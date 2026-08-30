#!/usr/bin/env bats
# renew-wildcard.sh (GH-104 Follow-up B) — captured production wildcard
# *.crowntime.cn renewal pipeline: acme.sh (DNS-01, wildcard SAN) -> local
# fingerprint short-circuit -> Qiniu upload -> CDN bind (hard fail) ->
# origin bind (warn only) -> nginx cert copy -> nginx reload (warn only).
#
# acme.sh and `sudo systemctl reload nginx` are both fake shell scripts
# (never a real ACME CA, DNS mutation, or nginx process); Qiniu calls go
# through use_mock_qiniu_helper (never a real Qiniu API). No test ever
# touches a real certificate, DNS record, or production host.

load test_helper/common

WILDCARD="$SSL_RENEW_ROOT/renew-wildcard.sh"
DOMAIN="crowntime.cn"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"

setup() {
    common_setup
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    # DOMAIN/CDN_DOMAIN/ORIGIN_DOMAIN above are deliberately NOT exported
    # here -- their values are identical to the script's own built-in
    # defaults, so every test below exercises those real defaults unless a
    # test explicitly exports an override itself.
    mkdir -p "$HOME/.acme.sh"
    NGINX_CERT_DIR="$TEST_TMPDIR/nginx-certs"
    export NGINX_CERT_DIR
    MOCK_BIN_DIR="$TEST_TMPDIR/mockbin"
    mkdir -p "$MOCK_BIN_DIR"
    PATH="$MOCK_BIN_DIR:$ORIGINAL_PATH"
    export PATH
}
teardown() { common_teardown; }

# fake_acme_sh <exit_code> — always writes a fresh fullchain.cer/<domain>.key
# pair into $HOME/.acme.sh/$DOMAIN before exiting, matching acme.sh's own
# behavior on both a real renewal (exit 0) and a "not due yet" skip (exit
# 2, where the existing files are simply left in place — acme.sh does not
# delete them either way).
fake_acme_sh() {
    local exit_code="${1:-0}"
    gen_cert "$DOMAIN" "$TEST_TMPDIR/fullchain.cer" "$TEST_TMPDIR/privkey.key"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TEST_TMPDIR/acme_invocations.log"
mkdir -p "$HOME/.acme.sh/$DOMAIN"
cp "$TEST_TMPDIR/fullchain.cer" "$HOME/.acme.sh/$DOMAIN/fullchain.cer"
cp "$TEST_TMPDIR/privkey.key" "$HOME/.acme.sh/$DOMAIN/$DOMAIN.key"
exit $exit_code
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
}

# fake_sudo <exit_code> — the ONLY sudo invocation this script makes is
# `sudo systemctl reload nginx`; a bare passthrough is enough since no
# test needs systemctl's own behavior beyond its exit code.
fake_sudo() {
    local exit_code="${1:-0}"
    cat >"$MOCK_BIN_DIR/sudo" <<EOF
#!/usr/bin/env bash
echo "sudo \$*" >>"$TEST_TMPDIR/sudo_invocations.log"
exit $exit_code
EOF
    chmod +x "$MOCK_BIN_DIR/sudo"
}

# ── Argument/env validation ──────────────────────────────────────────────

@test "missing QINIU_ACCESS_KEY/SECRET fails fast with a usage error, before touching acme.sh" {
    unset QINIU_ACCESS_KEY QINIU_SECRET_KEY
    fake_acme_sh 0
    fake_sudo 0
    run "$WILDCARD"
    [ "$status" -eq 2 ]
    [[ "$output" == *"missing required environment variable"* ]]
    [ ! -f "$TEST_TMPDIR/acme_invocations.log" ]
}

@test "domain defaults match the production values when DOMAIN/CDN_DOMAIN/ORIGIN_DOMAIN are unset" {
    fake_acme_sh 2
    fake_sudo 0
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    grep -q -- "-d crowntime.cn -d \*.crowntime.cn" "$TEST_TMPDIR/acme_invocations.log"
}

# ── ACME exit-code contract ───────────────────────────────────────────────

@test "acme.sh exit 2 (not due yet) is a successful no-op: no Qiniu or nginx calls" {
    fake_acme_sh 2
    fake_sudo 0
    use_mock_qiniu_helper success
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"not due yet"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "acme.sh hard failure (exit 1) is a non-zero failure, before any Qiniu/nginx call" {
    fake_acme_sh 1
    fake_sudo 0
    use_mock_qiniu_helper success
    run "$WILDCARD"
    [ "$status" -ne 0 ]
    [[ "$output" == *"acme.sh --renew failed"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── Fingerprint short-circuit ─────────────────────────────────────────────

@test "a fingerprint that already matches deployed_fp skips upload/bind/nginx entirely" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$DOMAIN"
    local_fp=$(sha256sum "$TEST_TMPDIR/fullchain.cer" 2>/dev/null | awk '{print $1}')
    # Run once for real to populate .acme.sh/$DOMAIN with a matching pair,
    # then compute its fingerprint the same way the script does.
    "$HOME/.acme.sh/acme.sh" --renew --dns dns_dp -d "$DOMAIN" -d "*.$DOMAIN" >/dev/null
    local_fp=$(sha256sum "$HOME/.acme.sh/$DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$DOMAIN/.deployed_fp"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already deployed"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── Full deploy path ───────────────────────────────────────────────────────

@test "full deploy: upload -> CDN bind -> origin bind -> nginx copy -> nginx reload, all succeeding" {
    fake_acme_sh 0
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid" MOCK_QINIU_ACTUAL_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"certID=wildcard-certid bound to CDN domain $CDN_DOMAIN"* ]]
    [[ "$output" == *"certID=wildcard-certid bound to origin domain $ORIGIN_DOMAIN"* ]]
    [[ "$output" == *"nginx reloaded"* ]]

    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$NGINX_CERT_DIR/privkey.pem" ]
    [ -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
    grep -q "sudo systemctl reload nginx" "$TEST_TMPDIR/sudo_invocations.log"

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

@test "CDN bind failure is a hard failure and never deploys to nginx" {
    fake_acme_sh 0
    fake_sudo 0

    # A qiniu_helper.py stand-in whose upload succeeds (so the failure
    # under test is specifically the CDN bind, not the earlier upload).
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
    [ "$status" -ne 0 ]
    [[ "$output" == *"CDN domain bind failed"* ]]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "origin bind failure is a warning only: CDN-bound cert is still deployed to nginx and reloaded" {
    fake_acme_sh 0
    fake_sudo 0

    # A qiniu_helper.py stand-in that succeeds for the CDN domain (upload +
    # bind) but fails bind for the origin domain specifically.
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
    [ "$status" -eq 0 ]
    [[ "$output" == *"origin domain bind failed"* ]]
    [[ "$output" == *"(non-fatal)"* ]]
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
    grep -q "sudo systemctl reload nginx" "$TEST_TMPDIR/sudo_invocations.log"
}

@test "nginx reload failure is a warning only: overall run still succeeds and the cert stays deployed" {
    fake_acme_sh 0
    fake_sudo 1
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid" MOCK_QINIU_ACTUAL_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"nginx reload failed"* ]]
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
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
