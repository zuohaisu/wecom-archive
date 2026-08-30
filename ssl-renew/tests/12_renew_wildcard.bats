#!/usr/bin/env bats
# renew-wildcard.sh (GH-104 Follow-up B, corrected twice) — captured
# production wildcard *.crowntime.cn renewal pipeline: acme.sh (DNS-01,
# wildcard SAN, exit 2 = not due yet but STILL falls through to the
# fingerprint check, never an early exit) -> local fingerprint
# short-circuit -> Qiniu upload (captured via `out=$(cmd) && st=$? ||
# st=$?`, NOT a bare assignment, so a failure is caught by the intended
# `if ... die` check instead of silently killing the script one line
# earlier under `set -e`) -> CDN bind -> origin bind -> fingerprint/TLS-
# marker bookkeeping (BEFORE the nginx step, so a downstream nginx
# failure never discards a Qiniu deployment that already succeeded) ->
# nginx cert copy, structured as `if cp && cp; then ... if sudo reload;
# then ... else warn; fi; else warn; fi` (an `&&`/`if` context, which is
# exempt from `set -e`) -> `sudo -n /usr/bin/systemctl reload nginx`.
#
# IMPORTANT, faithfully preserved production defect: this script calls
# `warn`/`die` on every failure path, but — unlike renew.sh, which defines
# its own local warn()/die() — neither is defined anywhere this script
# sources. An undefined bash function/command is "command not found"
# (exit 127); because `set -e` is active for the whole body past the
# acme.sh call, that 127 is NOT swallowed — it immediately halts the
# script right there. The intended custom message (e.g. "CDN domain bind
# failed...") is never printed (it's an argument to a command that never
# ran), nothing after the failing statement executes (no .deployed_fp
# write, no nginx deployment, no TLS marker cleanup), and the process exits
# 127. Verified empirically against every failure branch before writing
# these tests. Do NOT "fix" the underlying script to make these pass
# differently — see docs/operations/wildcard-ssl-renewal.md and the
# recommended follow-up hardening issue. These tests lock the OBSERVED
# (defective) behavior so a future change cannot silently alter it without
# a test failure making that explicit.
#
# acme.sh and `sudo` are both fake shell scripts (never a real ACME CA,
# DNS mutation, or nginx process); Qiniu calls go through
# use_mock_qiniu_helper (never a real Qiniu API). No test ever touches a
# real certificate, DNS record, or production host.

load test_helper/common

WILDCARD="$SSL_RENEW_ROOT/renew-wildcard.sh"
REAL_DOMAIN="crowntime.cn"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"

setup() {
    common_setup
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    mkdir -p "$HOME/.acme.sh"
    # The corrected script does NOT mkdir -p its nginx destination itself
    # (matching the captured production script) -- the fixture must
    # pre-create it, exactly like an Ops-provisioned host already would.
    NGINX_CERT_DIR="$TEST_TMPDIR/nginx-certs"
    mkdir -p "$NGINX_CERT_DIR"
    export NGINX_CERT_DIR
    MOCK_BIN_DIR="$TEST_TMPDIR/mockbin"
    mkdir -p "$MOCK_BIN_DIR"
    PATH="$MOCK_BIN_DIR:$ORIGINAL_PATH"
    export PATH
}
teardown() { common_teardown; }

# fake_acme_sh <exit_code> — always writes a fresh fullchain.cer/<domain>.key
# pair into $HOME/.acme.sh/$REAL_DOMAIN before exiting, matching acme.sh's own
# behavior on both a real renewal (exit 0) and a "not due yet" skip (exit
# 2, where the existing files are simply left in place — acme.sh does not
# delete them either way).
fake_acme_sh() {
    local exit_code="${1:-0}"
    gen_cert "$REAL_DOMAIN" "$TEST_TMPDIR/fullchain.cer" "$TEST_TMPDIR/privkey.key"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TEST_TMPDIR/acme_invocations.log"
mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
cp "$TEST_TMPDIR/fullchain.cer" "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer"
cp "$TEST_TMPDIR/privkey.key" "$HOME/.acme.sh/$REAL_DOMAIN/$REAL_DOMAIN.key"
exit $exit_code
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
}

# fake_sudo <exit_code> — logs its full argv so tests can assert on the
# exact command shape (sudo -n /usr/bin/systemctl reload nginx).
fake_sudo() {
    local exit_code="${1:-0}"
    cat >"$MOCK_BIN_DIR/sudo" <<EOF
#!/usr/bin/env bash
echo "sudo \$*" >>"$TEST_TMPDIR/sudo_invocations.log"
exit $exit_code
EOF
    chmod +x "$MOCK_BIN_DIR/sudo"
}

# ── Domain constants are hardcoded, not env-driven ──────────────────────

@test "hardcoded domain constants are used even when DOMAIN/CDN_DOMAIN/ORIGIN_DOMAIN env vars are set to something else" {
    export DOMAIN="not-the-real-domain.example"
    export CDN_DOMAIN="not-cdn.example"
    export ORIGIN_DOMAIN="not-origin.example"
    fake_acme_sh 0
    fake_sudo 0
    # Seed an already-matching fingerprint so the run is a no-op -- this
    # test only cares what domain the acme.sh invocation used, not the
    # rest of the deploy pipeline.
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    : >"$TEST_TMPDIR/acme_invocations.log"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    grep -q -- "-d crowntime.cn -d \*.crowntime.cn" "$TEST_TMPDIR/acme_invocations.log"
    [[ "$output" != *"not-the-real-domain.example"* ]]
}

# ── ACME exit-code contract ───────────────────────────────────────────────

@test "acme.sh exit 2 (not due yet) still falls through to the fingerprint check and deploys if the fingerprint differs" {
    fake_acme_sh 2
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid" MOCK_QINIU_ACTUAL_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"not due yet"* ]]
    # NOT a no-op: no prior .deployed_fp exists, so exit=2 must still lead
    # to a real deploy -- this is the corrected behavior (exit=2 is not an
    # early return, unlike the earlier, incorrect capture).
    [[ "$output" == *"bound to CDN domain $CDN_DOMAIN"* ]]
    [[ "$output" == *"nginx cert deployed + reloaded"* ]]
    grep -q "sudo -n /usr/bin/systemctl reload nginx" "$TEST_TMPDIR/sudo_invocations.log"
}

@test "acme.sh exit 2 with a fingerprint that already matches deployed_fp is a no-op" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already deployed"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "acme.sh hard failure (any exit other than 0 or 2) halts immediately: undefined die() -> exit 127, no downstream calls" {
    fake_acme_sh 1
    fake_sudo 0
    use_mock_qiniu_helper success

    run "$WILDCARD"
    [ "$status" -eq 127 ]
    [[ "$output" == *"command not found"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── Fingerprint short-circuit (acme exit 0 case) ────────────────────────

@test "a fingerprint that already matches deployed_fp skips upload/bind/nginx entirely" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already deployed"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── Full deploy path ───────────────────────────────────────────────────────

@test "full deploy: upload -> CDN bind -> origin bind -> nginx copy -> sudo -n /usr/bin/systemctl reload nginx" {
    fake_acme_sh 0
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid" MOCK_QINIU_ACTUAL_CERT_ID="wildcard-certid"
    # Pre-seed TLS markers to prove they get cleared on a real re-deploy.
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    touch "$HOME/.acme.sh/$REAL_DOMAIN/.tls_verified"
    echo 3 >"$HOME/.acme.sh/$REAL_DOMAIN/.tls_mismatch_days"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"bound to CDN domain $CDN_DOMAIN"* ]]
    [[ "$output" == *"bound to origin domain $ORIGIN_DOMAIN"* ]]
    [[ "$output" == *"nginx cert deployed + reloaded"* ]]

    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$NGINX_CERT_DIR/privkey.pem" ]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    # Exact production command shape: absolute path, non-interactive.
    grep -q "sudo -n /usr/bin/systemctl reload nginx" "$TEST_TMPDIR/sudo_invocations.log"

    # TLS markers are cleared on every successful re-deploy.
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.tls_verified" ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.tls_mismatch_days" ]

    # File permissions: the deployed copies must not be world-readable.
    # GNU stat's `-c` must come first: GNU `stat -f` means "filesystem
    # status" (a different report entirely) and silently succeeds with
    # unrelated output instead of failing, so a `-f`-first fallback chain
    # never reaches `-c` on Linux. `-c` is unrecognized by BSD/macOS stat
    # and fails cleanly there, correctly falling through to `-f`. Same
    # order as scripts/nonprod_deployment_lib.sh's existing helper.
    perm=$(stat -c '%a' "$NGINX_CERT_DIR/privkey.pem" 2>/dev/null || stat -f '%Lp' "$NGINX_CERT_DIR/privkey.pem")
    [ "$perm" = "640" ]
}

# ── Known, deliberately-preserved production defect: warn/die are
# undefined, and `set -e` is active past the acme.sh call, so any failure
# path HALTS THE SCRIPT IMMEDIATELY at exit 127 -- it does not continue,
# and does not print its own intended message. Verified empirically
# against the real script before being written here. Do not "fix" the
# script to make these pass differently -- see the file header above and
# docs/operations/wildcard-ssl-renewal.md.

@test "KNOWN DEFECT: a CDN bind failure halts the script at exit 127 -- no fingerprint recorded, no nginx deploy" {
    fake_acme_sh 0
    fake_sudo 0

    cat >"$MOCK_BIN_DIR/qiniu_cdn_fail_helper.py" <<'PYEOF'
import sys, json
args = sys.argv[1:]
cmd = args[0] if args else ""
if cmd == "upload":
    print(json.dumps({"certID": "wildcard-certid"}))
    sys.exit(0)
if cmd == "bind":
    print(json.dumps({"error": "CDN bind rejected"}))
    sys.exit(1)
print(json.dumps({"error": "unexpected command"}))
sys.exit(1)
PYEOF
    export QINIU_HELPER_PYTHON="python3"
    export QINIU_HELPER_SCRIPT="$MOCK_BIN_DIR/qiniu_cdn_fail_helper.py"

    run "$WILDCARD"
    [ "$status" -eq 127 ]
    [[ "$output" == *"command not found"* ]]
    # The intended custom message is an argument to a command that never
    # ran, so it is never printed.
    [[ "$output" != *"CDN domain bind failed"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "KNOWN DEFECT: an origin bind failure ALSO halts the script at exit 127, even though the CDN bind already succeeded -- nginx never gets the already-bound certificate" {
    fake_acme_sh 0
    fake_sudo 0

    cat >"$MOCK_BIN_DIR/qiniu_wildcard_helper.py" <<'PYEOF'
import sys, json
args = sys.argv[1:]
cmd = args[0] if args else ""
def _get(flag):
    return args[args.index(flag) + 1] if flag in args else None
if cmd == "upload":
    print(json.dumps({"certID": "wildcard-certid"}))
    sys.exit(0)
if cmd == "bind":
    if _get("--domain") == "media-origin.crowntime.cn":
        print(json.dumps({"error": "origin bind rejected"}))
        sys.exit(1)
    print(json.dumps({}))
    sys.exit(0)
print(json.dumps({"error": "unexpected command"}))
sys.exit(1)
PYEOF
    export QINIU_HELPER_PYTHON="python3"
    export QINIU_HELPER_SCRIPT="$MOCK_BIN_DIR/qiniu_wildcard_helper.py"

    run "$WILDCARD"
    [ "$status" -eq 127 ]
    [[ "$output" == *"command not found"* ]]
    [[ "$output" == *"bound to CDN domain $CDN_DOMAIN"* ]]
    # The CDN bind genuinely succeeded, but the script died before ever
    # reaching the nginx copy step or recording the fingerprint.
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "KNOWN DEFECT: a missing nginx cert directory still halts at exit 127 (warn undefined), but the fingerprint IS already recorded by then" {
    fake_acme_sh 0
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid" MOCK_QINIU_ACTUAL_CERT_ID="wildcard-certid"
    export NGINX_CERT_DIR="$TEST_TMPDIR/does-not-exist"

    run "$WILDCARD"
    [ "$status" -eq 127 ]
    [[ "$output" == *"command not found"* ]]
    # Correction #2: the fingerprint/TLS-marker bookkeeping now happens
    # right after the Qiniu binds, BEFORE the nginx block -- so a
    # downstream nginx failure (here: destination directory missing) no
    # longer discards the record of a Qiniu deployment that DID succeed.
    # A follow-up run will correctly short-circuit as "already deployed"
    # instead of redundantly re-uploading to Qiniu.
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
}

@test "CORRECTION: a Qiniu upload failure is caught by the intended die() check, not silently killed earlier by set -e" {
    fake_acme_sh 0
    fake_sudo 0

    cat >"$MOCK_BIN_DIR/qiniu_upload_fail_helper.py" <<'PYEOF'
import sys, json
sys.exit(1)
PYEOF
    export QINIU_HELPER_PYTHON="python3"
    export QINIU_HELPER_SCRIPT="$MOCK_BIN_DIR/qiniu_upload_fail_helper.py"

    run "$WILDCARD"
    # Still 127 (die is undefined -- the KNOWN DEFECT is unchanged), but
    # the key regression this locks: `upload_output=$(...) && upload_status=$?
    # || upload_status=$?` correctly exempts the assignment from `set -e`,
    # so execution actually reaches `if [ "$upload_status" -ne 0 ]; then
    # die ...`. Before this correction, a bare `upload_output=$(...)`
    # assignment under `set -e` would have killed the script AT THAT LINE
    # instead, with no "command not found" and never attempting die() at
    # all -- i.e. the exact same 127 you'd see here is not proof by
    # itself; the "command not found" text is.
    [ "$status" -eq 127 ]
    [[ "$output" == *"command not found"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
}

@test "CORRECTION: nginx reload failure (cp succeeded) also hits the undefined warn(), but the fingerprint and cert files are already in place" {
    fake_acme_sh 0
    fake_sudo 1
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid" MOCK_QINIU_ACTUAL_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 127 ]
    [[ "$output" == *"command not found"* ]]
    # The nginx cp/chmod step is isolated from the reload step (an
    # `if cp && cp; then ... if sudo reload; then ... else warn; fi;
    # else warn; fi` structure) -- a reload-only failure must not have
    # prevented the certificate files from already landing on disk.
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$NGINX_CERT_DIR/privkey.pem" ]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    grep -q "sudo -n /usr/bin/systemctl reload nginx" "$TEST_TMPDIR/sudo_invocations.log"
}

@test "no warn()/die() function is defined by this script, and notify.sh is never sourced or invoked" {
    run grep -nE '^(warn|die)\s*\(\)' "$WILDCARD"
    [ "$status" -ne 0 ]
    run grep -n "notify.sh" "$WILDCARD"
    [ "$status" -ne 0 ]
}

# ── Secret safety ──────────────────────────────────────────────────────────

@test "QINIU_ACCESS_KEY/QINIU_SECRET_KEY values never appear in stdout/stderr on success or failure" {
    fake_acme_sh 0
    fake_sudo 0
    use_mock_qiniu_helper http_error 401 "$TEST_TMPDIR/helper.log"

    run "$WILDCARD"
    [[ "$output" != *"testak"* ]]
    [[ "$output" != *"testsk"* ]]
}

@test "the script never sets -x and never echoes a QINIU_* variable literally" {
    run grep -nE '(^|[^#])set[[:space:]]+-[a-zA-Z]*x|echo[[:space:]]+"?\$(QINIU_ACCESS_KEY|QINIU_SECRET_KEY)"?' "$WILDCARD"
    [ "$status" -ne 0 ]
}
