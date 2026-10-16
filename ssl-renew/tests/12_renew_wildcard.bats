#!/usr/bin/env bats
# GH-123 regression coverage for wildcard *.crowntime.cn renewal. All ACME,
# Qiniu, sudo, and nginx interactions are mocks; no test contacts a real
# provider, changes DNS, reloads a local nginx, or uses production credentials.

load test_helper/common

WILDCARD_SOURCE="$SSL_RENEW_ROOT/renew-wildcard.sh"
REAL_DOMAIN="crowntime.cn"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"

setup() {
    common_setup
    # The installed script deliberately uses the production absolute helper
    # path. Redirect only that bootstrap path in this disposable test copy.
    WILDCARD="$TEST_TMPDIR/renew-wildcard.sh"
    sed 's|^SCRIPT_DIR="/srv/apps/wecom-archive-365/current/ssl-renew"$|SCRIPT_DIR="'"$SSL_RENEW_ROOT"'"|' \
        "$WILDCARD_SOURCE" >"$WILDCARD"
    chmod +x "$WILDCARD"

    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    mkdir -p "$HOME/.acme.sh"
    NGINX_CERT_DIR="$TEST_TMPDIR/nginx-certs"
    mkdir -p "$NGINX_CERT_DIR"
    export NGINX_CERT_DIR

    MOCK_BIN_DIR="$TEST_TMPDIR/mockbin"
    mkdir -p "$MOCK_BIN_DIR"
    export REAL_CP="$(command -v cp)"
    PATH="$MOCK_BIN_DIR:$ORIGINAL_PATH"
    export PATH
}
teardown() { common_teardown; }

# fake_acme_sh <exit_code> writes a synthetic wildcard certificate/key pair.
# Exit 2 intentionally still leaves the existing pair in place, as acme.sh
# does on a not-due run.
fake_acme_sh() {
    local exit_code="${1:-0}"
    gen_cert "$REAL_DOMAIN" "$TEST_TMPDIR/fullchain.cer" "$TEST_TMPDIR/privkey.key"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TEST_TMPDIR/acme_invocations.log"
mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
"\$REAL_CP" "$TEST_TMPDIR/fullchain.cer" "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer"
"\$REAL_CP" "$TEST_TMPDIR/privkey.key" "$HOME/.acme.sh/$REAL_DOMAIN/$REAL_DOMAIN.key"
exit $exit_code
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
}

fake_acme_without_certificate() {
    local exit_code="${1:-0}"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TEST_TMPDIR/acme_invocations.log"
exit $exit_code
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
}

# fake_sudo <exit_code> logs the exact attempted reload command.
fake_sudo() {
    local exit_code="${1:-0}"
    cat >"$MOCK_BIN_DIR/sudo" <<EOF
#!/usr/bin/env bash
echo "sudo \$*" >>"$TEST_TMPDIR/sudo_invocations.log"
exit $exit_code
EOF
    chmod +x "$MOCK_BIN_DIR/sudo"
}

# use_bind_failure_helper <domain> <message> succeeds for upload and every
# other bind/verify, but returns a synthetic bind error for the selected
# domain. MOCK_FAIL_VERIFY_DOMAIN (optional) additionally fails `verify`
# for that domain (HTTP 612-style), modelling a source domain the CDN API
# cannot see at all.
use_bind_failure_helper() {
    export MOCK_FAIL_BIND_DOMAIN="$1"
    export MOCK_FAIL_BIND_MESSAGE="$2"
    cat >"$MOCK_BIN_DIR/qiniu_bind_helper.py" <<'PYEOF'
import json
import os
import sys

args = sys.argv[1:]
command = args[0] if args else ""

if os.environ.get("MOCK_QINIU_LOG"):
    with open(os.environ["MOCK_QINIU_LOG"], "a") as f:
        f.write(" ".join(args) + "\n")


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


if command == "upload":
    print(json.dumps({"certID": "wildcard-certid"}))
    sys.exit(0)
if command == "bind":
    if value("--domain") == os.environ["MOCK_FAIL_BIND_DOMAIN"]:
        print(json.dumps({"error": os.environ["MOCK_FAIL_BIND_MESSAGE"]}))
        sys.exit(1)
    print(json.dumps({}))
    sys.exit(0)
if command == "verify":
    if value("--domain") == os.environ.get("MOCK_FAIL_VERIFY_DOMAIN"):
        print(json.dumps({"error": os.environ["MOCK_FAIL_BIND_MESSAGE"]}))
        sys.exit(1)
    print(json.dumps({"ok": True, "certId": value("--expected-cert-id"), "matches": True}))
    sys.exit(0)
if command == "tls-verify":
    if os.environ.get("MOCK_QINIU_TLS_ERROR"):
        print(json.dumps({"ok": False, "stage": "tls_verify", "error": os.environ["MOCK_QINIU_TLS_ERROR"]}))
        sys.exit(1)
    matches = os.environ.get("MOCK_QINIU_TLS_MATCH", "true") == "true"
    print(json.dumps({"ok": True, "matches": matches, "local_sha256": "a" * 64, "remote_sha256": "a" * 64 if matches else "b" * 64}))
    sys.exit(0)
print(json.dumps({"error": "unexpected command"}))
sys.exit(1)
PYEOF
    export QINIU_HELPER_PYTHON="python3"
    export QINIU_HELPER_SCRIPT="$MOCK_BIN_DIR/qiniu_bind_helper.py"
}

fake_nginx_copy_failure() {
    cat >"$MOCK_BIN_DIR/cp" <<'EOF'
#!/usr/bin/env bash
case "$*" in
*"$NGINX_CERT_DIR"*)
    echo "mock nginx certificate copy denied" >&2
    exit 1
    ;;
esac
exec "$REAL_CP" "$@"
EOF
    chmod +x "$MOCK_BIN_DIR/cp"
}

# ── GH-232 helpers: deployed-state files + console-binding marker ───────

# write_deployed_state <certid> records fp+certid files matching the cert
# currently in $HOME/.acme.sh/$REAL_DOMAIN (i.e. run fake_acme_sh first).
write_deployed_state() {
    local certid="$1" fp
    fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "$certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
}

# write_origin_console_marker records the current cert's fingerprint in
# .origin_console_fp, simulating an operator who has console-bound the
# current cert to the origin domain per the GH-232 runbook.
write_origin_console_marker() {
    sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}' \
        >"$HOME/.acme.sh/$REAL_DOMAIN/.origin_console_fp"
}

# ── Canonical reporting and alert integration ───────────────────────────

@test "shared warn/die emit one structured redacted stderr diagnostic" {
    warn_stdout="$TEST_TMPDIR/warn.stdout"
    warn_stderr="$TEST_TMPDIR/warn.stderr"
    bash -c '
        source "$1"
        CURRENT_STAGE=origin_bind
        SSL_RENEW_ALERT_DOMAIN=crowntime.cn
        warn "QINIU_SECRET_KEY=wildcard-secret-fixture ALERT_WEBHOOK_URL=https://user:webhook-secret@example.test/hook"
    ' bash "$SSL_RENEW_ROOT/lib/common.sh" >"$warn_stdout" 2>"$warn_stderr"
    [ "$?" -eq 0 ]
    [ ! -s "$warn_stdout" ]
    grep -q '\[WARN\] stage=origin_bind domain=crowntime.cn' "$warn_stderr"
    grep -q '\[REDACTED\]' "$warn_stderr"
    ! grep -q 'wildcard-secret-fixture\|webhook-secret' "$warn_stderr"

    die_stdout="$TEST_TMPDIR/die.stdout"
    die_stderr="$TEST_TMPDIR/die.stderr"
    set +e
    bash -c '
        source "$1"
        CURRENT_STAGE=acme_renew
        SSL_RENEW_ALERT_DOMAIN=crowntime.cn
        die "QINIU_SECRET_KEY=wildcard-secret-fixture"
    ' bash "$SSL_RENEW_ROOT/lib/common.sh" >"$die_stdout" 2>"$die_stderr"
    die_status=$?
    set -e
    [ "$die_status" -eq 1 ]
    [ ! -s "$die_stdout" ]
    grep -q '\[ERROR\] stage=acme_renew domain=crowntime.cn' "$die_stderr"
    ! grep -q 'wildcard-secret-fixture' "$die_stderr"
}

@test "wildcard service uses the canonical OnFailure alert hook without direct notification" {
    service="$SSL_RENEW_ROOT/../deploy/systemd/qiniu-ssl-renew-wildcard.service"
    alert_service="$SSL_RENEW_ROOT/../deploy/systemd/wecom-job-failure-alert@.service"

    grep -q '^OnFailure=wecom-job-failure-alert@%n.service$' "$service"
    grep -q 'ssl-renew/notify.sh ERROR %i' "$alert_service"
    run grep -n 'notify.sh' "$WILDCARD_SOURCE"
    [ "$status" -ne 0 ]
    # The warning-only tests below assert final status 0. Systemd invokes
    # OnFailure only for a failed service, so those paths do not alert.
}

@test "wildcard source consumes the shared canonical warn/die implementation" {
    run grep -nE '^(warn|die)[[:space:]]*\(\)' "$WILDCARD_SOURCE"
    [ "$status" -ne 0 ]
    run bash -c 'source "$1"; declare -F warn die' bash "$SSL_RENEW_ROOT/lib/common.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"warn"* ]]
    [[ "$output" == *"die"* ]]
}

# ── Preserved ACME and fingerprint success semantics ────────────────────

@test "hardcoded wildcard domains ignore convention-only DOMAIN environment values" {
    export DOMAIN="not-the-real-domain.example"
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    : >"$TEST_TMPDIR/acme_invocations.log"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    grep -q -- "-d crowntime.cn -d \*.crowntime.cn" "$TEST_TMPDIR/acme_invocations.log"
    [[ "$output" != *"not-the-real-domain.example"* ]]
}

@test "acme exit 2 continues to fingerprint and deployment when it differs" {
    fake_acme_sh 2
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"not due yet"* ]]
    [[ "$output" == *"bound to $CDN_DOMAIN (CDN)"* ]]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

@test "acme exit 2 with a matching fingerprint is a successful no-op after re-verification" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"cert already deployed — nothing to do"* ]]
    # GH-232: the early-exit path now re-verifies BOTH live Qiniu bindings
    # (CDN + origin) against the recorded certID instead of blindly
    # trusting the fingerprint file.
    [ "$(grep -c '^verify ' "$TEST_TMPDIR/helper.log")" -eq 2 ]
    ! grep -q '^upload \|^bind ' "$TEST_TMPDIR/helper.log"
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "matching fingerprint remains a successful no-op after re-verification" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"cert already deployed — nothing to do"* ]]
    [ "$(grep -c '^verify ' "$TEST_TMPDIR/helper.log")" -eq 2 ]
    ! grep -q '^upload \|^bind ' "$TEST_TMPDIR/helper.log"
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── GH-235: CDN live TLS evidence gates idempotent exit ─────────────────

@test "CDN API HTTP 404 plus matching live TLS leaf is a no-op with no upload or bind" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    write_origin_console_marker
    use_mock_qiniu_helper http_error 404 "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_HTTP_ERROR_COMMAND=verify
    export MOCK_QINIU_TLS_MATCH=true

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"HTTP 404"* ]]
    [[ "$output" == *"live CDN TLS leaf matches"* ]]
    [[ "$output" == *"cert already deployed — nothing to do"* ]]
    grep -q "^verify --domain $CDN_DOMAIN " "$TEST_TMPDIR/helper.log"
    [ "$(grep -c '^verify ' "$TEST_TMPDIR/helper.log")" -eq 1 ]
    grep -q "^tls-verify --domain $CDN_DOMAIN " "$TEST_TMPDIR/helper.log"
    ! grep -q '^upload \|^bind ' "$TEST_TMPDIR/helper.log"
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$NGINX_CERT_DIR/privkey.pem" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "live CDN TLS leaf mismatch triggers the intended deployment recovery" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    write_origin_console_marker
    use_mock_qiniu_helper http_error 404 "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_HTTP_ERROR_COMMAND=verify
    export MOCK_QINIU_TLS_MATCH=false
    export MOCK_QINIU_CERT_ID="repaired-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"live CDN TLS leaf does not match"* ]]
    [[ "$output" == *"continuing with full deployment"* ]]
    grep -q '^upload ' "$TEST_TMPDIR/helper.log"
    grep -q "^bind --domain $CDN_DOMAIN " "$TEST_TMPDIR/helper.log"
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "repaired-certid" ]
}

@test "live CDN TLS probe failure alerts and does not assume success or rebind" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    write_origin_console_marker
    use_mock_qiniu_helper http_error 404 "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_TLS_ERROR="TLS connection timed out"

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_live_tls_verify"* ]]
    [[ "$output" == *"TLS connection timed out"* ]]
    ! grep -q '^upload \|^bind ' "$TEST_TMPDIR/helper.log"
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── GH-232: early-exit re-verification failure branches self-heal ───────

@test "fingerprint match with a CDN certID drift triggers full redeployment" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_CERT_ID="wildcard-certid"
    export MOCK_QINIU_VERIFY_MISMATCH_DOMAIN="$CDN_DOMAIN"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"CDN API reports a certID mismatch"* ]]
    [[ "$output" == *"continuing with full deployment"* ]]
    [[ "$output" == *"bound to $CDN_DOMAIN (CDN)"* ]]
    [[ "$output" == *"bound to $ORIGIN_DOMAIN (origin)"* ]]
    grep -q '^upload ' "$TEST_TMPDIR/helper.log"
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "wildcard-certid" ]
}

@test "fingerprint match without a recorded certID triggers full redeployment" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    # Legacy state: fp written by an older script version, no certID file.
    sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}' >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid" ]

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"no recorded certID"* ]]
    [[ "$output" == *"continuing with full deployment"* ]]
    grep -q '^upload ' "$TEST_TMPDIR/helper.log"
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "wildcard-certid" ]
}

@test "fingerprint match with an unverifiable origin binding and no console marker redeploys" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.origin_console_fp" ]

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_CERT_ID="wildcard-certid"
    export MOCK_QINIU_VERIFY_MISMATCH_DOMAIN="$ORIGIN_DOMAIN"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"origin binding verify failed (no matching console marker)"* ]]
    [[ "$output" == *"continuing with full deployment"* ]]
    grep -q '^upload ' "$TEST_TMPDIR/helper.log"
}

@test "fingerprint match with a matching console marker skips the origin API verify" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_deployed_state "wildcard-certid"
    write_origin_console_marker

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    # The origin verify would 612 if it were attempted — the matching
    # marker must excuse it and keep the run a verified no-op.
    export MOCK_QINIU_VERIFY_MISMATCH_DOMAIN="$ORIGIN_DOMAIN"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"origin console-binding marker matches"* ]]
    [[ "$output" == *"cert already deployed — nothing to do"* ]]
    # Only the CDN verify ran.
    [ "$(grep -c '^verify ' "$TEST_TMPDIR/helper.log")" -eq 1 ]
    grep -q "^verify --domain $CDN_DOMAIN " "$TEST_TMPDIR/helper.log"
    ! grep -q '^upload \|^bind ' "$TEST_TMPDIR/helper.log"
}

# ── Hard failures ───────────────────────────────────────────────────────

@test "acme hard failure exits deterministic 1 without downstream calls" {
    fake_acme_sh 1
    fake_sudo 0
    use_mock_qiniu_helper success

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=acme_renew domain=crowntime.cn message=acme.sh --renew failed (exit=1)"* ]]
    [[ "$output" != *"command not found"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "missing fullchain.cer exits deterministic 1" {
    fake_acme_without_certificate 0
    fake_sudo 0

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=local_fingerprint domain=crowntime.cn message=fullchain.cer not found:"* ]]
    [[ "$output" != *"command not found"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "Qiniu upload failure exits non-zero and filters secret-shaped helper output" {
    fake_acme_sh 0
    fake_sudo 0
    use_mock_qiniu_helper http_error 401
    export MOCK_QINIU_ERROR='Qiniu upload rejected: QINIU_SECRET_KEY=wildcard-secret-fixture ALERT_WEBHOOK_URL=https://user:webhook-secret@example.test/hook'

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_upload domain=crowntime.cn"* ]]
    [[ "$output" != *"wildcard-secret-fixture"* ]]
    [[ "$output" != *"webhook-secret"* ]]
    [[ "$output" != *"testak"* ]]
    [[ "$output" != *"testsk"* ]]
    [[ "$output" == *"[REDACTED]"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "CDN bind failure exits non-zero without recording fingerprint or deploying nginx" {
    fake_acme_sh 0
    fake_sudo 0
    use_bind_failure_helper "$CDN_DOMAIN" "CDN bind rejected"

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_bind_cdn domain=crowntime.cn message=CDN bind failed: CDN bind rejected"* ]]
    [[ "$output" != *"command not found"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "missing mandatory Qiniu runtime exits deterministic 1" {
    fake_acme_sh 0
    fake_sudo 0
    export QINIU_HELPER_PYTHON="$MOCK_BIN_DIR/missing-python"

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_upload domain=crowntime.cn message=required Qiniu helper runtime dependency is unavailable"* ]]
    [[ "$output" != *"command not found"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
}

# ── Hard failure: origin bind (GH-232 fail-closed + console marker) ─────

@test "origin bind failure without a console marker dies with runbook guidance and records nothing" {
    fake_acme_sh 0
    fake_sudo 0
    use_bind_failure_helper "$ORIGIN_DOMAIN" 'HTTP 612: {"code":612,"error":"no such domain"}'
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.origin_console_fp" ]

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_bind_origin domain=crowntime.cn"* ]]
    [[ "$output" == *"certID=wildcard-certid is NOT bound"* ]]
    [[ "$output" == *"612"* ]]
    [[ "$output" == *"docs/operations/qiniu-origin-domain-https-runbook.md"* ]]
    [[ "$output" == *".origin_console_fp"* ]]
    [[ "$output" != *"bound to $ORIGIN_DOMAIN (origin)"* ]]
    [[ "$output" != *"command not found"* ]]
    # An origin hard failure records nothing and never reaches nginx.
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "a stale console marker does not excuse an origin bind failure for a new cert" {
    fake_acme_sh 0
    fake_sudo 0
    use_bind_failure_helper "$ORIGIN_DOMAIN" 'HTTP 612: {"code":612,"error":"no such domain"}'
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    echo "stale-fingerprint-from-a-previous-cert" >"$HOME/.acme.sh/$REAL_DOMAIN/.origin_console_fp"

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_bind_origin domain=crowntime.cn"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
}

@test "a matching console marker excuses the expected origin 612 and completes the deployment" {
    fake_acme_sh 0
    fake_sudo 0
    # Operator has already console-bound the CURRENT cert (the 612 response
    # is the expected API limitation, not drift).
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    use_bind_failure_helper "$ORIGIN_DOMAIN" 'HTTP 612: {"code":612,"error":"no such domain"}'
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    write_origin_console_marker
    : >"$TEST_TMPDIR/acme_invocations.log"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARN] stage=qiniu_bind_origin domain=crowntime.cn"* ]]
    [[ "$output" == *".origin_console_fp matches the current cert"* ]]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "wildcard-certid" ]
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

@test "runs converge after the console bind: alert, marker, then a verified no-op" {
    fake_acme_sh 0
    fake_sudo 0
    use_bind_failure_helper "$ORIGIN_DOMAIN" 'HTTP 612: {"code":612,"error":"no such domain"}'

    # Run 1: no marker — hard failure, nothing recorded, alert fires.
    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]

    # Operator console-binds the current cert and records the marker.
    write_origin_console_marker

    # Run 2: full redeploy, origin 612 excused by the marker, state recorded.
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "wildcard-certid" ]

    # Run 3: fp match + CDN verify + matching marker — a verified no-op.
    export MOCK_QINIU_LOG="$TEST_TMPDIR/helper.log"
    export MOCK_FAIL_VERIFY_DOMAIN="$ORIGIN_DOMAIN"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"cert already deployed — nothing to do"* ]]
    ! grep -q '^upload \|^bind ' "$TEST_TMPDIR/helper.log"
}

# ── Approved warning-only paths ─────────────────────────────────────────

@test "nginx certificate copy failure warns and preserves final success" {
    fake_acme_sh 0
    fake_sudo 0
    fake_nginx_copy_failure
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARN] stage=nginx_deploy domain=crowntime.cn message=nginx cert copy failed — files NOT updated"* ]]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "nginx reload failure warns after copies and preserves final success" {
    fake_acme_sh 0
    fake_sudo 1
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARN] stage=nginx_deploy domain=crowntime.cn message=nginx reload failed (sudo) — cert files updated, manual reload needed"* ]]
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$NGINX_CERT_DIR/privkey.pem" ]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

# ── Fully successful deploy regression ──────────────────────────────────

@test "full deploy keeps upload, both binds, nginx copy, and non-interactive reload semantics" {
    fake_acme_sh 0
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid"
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    touch "$HOME/.acme.sh/$REAL_DOMAIN/.tls_verified"
    echo 3 >"$HOME/.acme.sh/$REAL_DOMAIN/.tls_mismatch_days"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"bound to $CDN_DOMAIN (CDN)"* ]]
    [[ "$output" == *"bound to $ORIGIN_DOMAIN (origin)"* ]]
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ -f "$NGINX_CERT_DIR/privkey.pem" ]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "wildcard-certid" ]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.tls_verified" ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.tls_mismatch_days" ]

    perm=$(stat -c '%a' "$NGINX_CERT_DIR/privkey.pem" 2>/dev/null || stat -f '%Lp' "$NGINX_CERT_DIR/privkey.pem")
    [ "$perm" = "640" ]
}
