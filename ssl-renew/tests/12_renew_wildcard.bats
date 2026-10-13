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

# use_stateful_helper succeeds for upload and every bind/verify, except the
# selected per-command failure domain. Env contract:
#   MOCK_CERT_ID                 certID returned by `upload` (default wildcard-certid)
#   MOCK_FAIL_BIND_DOMAIN        `bind` fails for this domain with MOCK_FAIL_BIND_MESSAGE
#   MOCK_FAIL_VERIFY_DOMAIN      `verify` fails for this domain with MOCK_FAIL_VERIFY_MESSAGE
#   MOCK_VERIFY_ACTUAL_CERT_ID   `verify` success returns this actual certId (default: expected)
use_stateful_helper() {
    cat >"$MOCK_BIN_DIR/qiniu_stateful_helper.py" <<'PYEOF'
import json
import os
import sys

args = sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


command = args[0] if args else ""

if command == "upload":
    print(json.dumps({"certID": os.environ.get("MOCK_CERT_ID", "wildcard-certid")}))
    sys.exit(0)

if command == "bind":
    fail_domain = os.environ.get("MOCK_FAIL_BIND_DOMAIN")
    if fail_domain and value("--domain") == fail_domain:
        print(json.dumps({"error": os.environ.get("MOCK_FAIL_BIND_MESSAGE", "bind rejected")}))
        sys.exit(1)
    print(json.dumps({}))
    sys.exit(0)

if command == "verify":
    fail_domain = os.environ.get("MOCK_FAIL_VERIFY_DOMAIN")
    if fail_domain and value("--domain") == fail_domain:
        print(json.dumps({"error": os.environ.get("MOCK_FAIL_VERIFY_MESSAGE", "verify rejected")}))
        sys.exit(1)
    expected = value("--expected-cert-id")
    actual = os.environ.get("MOCK_VERIFY_ACTUAL_CERT_ID", expected)
    print(json.dumps({"ok": True, "certId": actual, "matches": actual == expected}))
    sys.exit(0)

print(json.dumps({"error": "unexpected command"}))
sys.exit(1)
PYEOF
    export QINIU_HELPER_PYTHON="python3"
    export QINIU_HELPER_SCRIPT="$MOCK_BIN_DIR/qiniu_stateful_helper.py"
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
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success
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

@test "acme exit 2 with a matching fingerprint is a successful no-op after verifying Qiniu bindings" {
    fake_acme_sh 2
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already deployed"* ]]
    # GH-232: the no-op path must re-verify BOTH live Qiniu bindings.
    [ -f "$TEST_TMPDIR/helper.log" ]
    [ "$(grep -c '^verify ' "$TEST_TMPDIR/helper.log")" -eq 2 ]
    ! grep -q '^upload\|^bind' "$TEST_TMPDIR/helper.log"
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "matching fingerprint remains a successful no-op after verifying Qiniu bindings" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already deployed"* ]]
    [ -f "$TEST_TMPDIR/helper.log" ]
    [ "$(grep -c '^verify ' "$TEST_TMPDIR/helper.log")" -eq 2 ]
    ! grep -q '^upload\|^bind' "$TEST_TMPDIR/helper.log"
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

# ── GH-232: fingerprint match must verify live Qiniu bindings ─────────────

@test "fingerprint match without a recorded certID redeploys to verify Qiniu bindings" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    : >"$TEST_TMPDIR/acme_invocations.log"

    use_stateful_helper
    export MOCK_CERT_ID="wildcard-certid"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"no recorded certID — re-deploying"* ]]
    [[ "$output" == *"bound to $CDN_DOMAIN (CDN)"* ]]
    [[ "$output" == *"bound to $ORIGIN_DOMAIN (origin)"* ]]
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid")" = "wildcard-certid" ]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

@test "fingerprint match with a lost CDN binding redeploys and self-heals" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    export MOCK_FAIL_VERIFY_DOMAIN="$CDN_DOMAIN"
    export MOCK_FAIL_VERIFY_MESSAGE="certID mismatch — expected=wildcard-certid, actual=stale-certid"
    use_stateful_helper
    export MOCK_CERT_ID="wildcard-certid"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"$CDN_DOMAIN no longer binds certID=wildcard-certid"* ]]
    [[ "$output" == *"bound to $CDN_DOMAIN (CDN)"* ]]
    [[ "$output" == *"bound to $ORIGIN_DOMAIN (origin)"* ]]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

@test "fingerprint match with a lost origin binding redeploys and self-heals" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    export MOCK_FAIL_VERIFY_DOMAIN="$ORIGIN_DOMAIN"
    export MOCK_FAIL_VERIFY_MESSAGE="certID mismatch — expected=wildcard-certid, actual=stale-certid"
    use_stateful_helper
    export MOCK_CERT_ID="wildcard-certid"
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"$ORIGIN_DOMAIN no longer binds certID=wildcard-certid"* ]]
    [[ "$output" == *"bound to $ORIGIN_DOMAIN (origin)"* ]]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

@test "fingerprint match with API-invisible origin and console marker is a successful no-op" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.origin_console_fp"
    : >"$TEST_TMPDIR/acme_invocations.log"

    export MOCK_FAIL_VERIFY_DOMAIN="$ORIGIN_DOMAIN"
    export MOCK_FAIL_VERIFY_MESSAGE="Qiniu verify failed — HTTP 612: {\"code\":612,\"error\":\"no such domain\"}"
    use_stateful_helper
    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"HTTP 612"* ]]
    [[ "$output" == *"console binding confirmed"* ]]
    [[ "$output" == *"already deployed"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "fingerprint match with API-invisible origin and no console marker dies with runbook" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
    echo "wildcard-certid" >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid"
    : >"$TEST_TMPDIR/acme_invocations.log"

    export MOCK_FAIL_VERIFY_DOMAIN="$ORIGIN_DOMAIN"
    export MOCK_FAIL_VERIFY_MESSAGE="Qiniu verify failed — HTTP 612: {\"code\":612,\"error\":\"no such domain\"}"
    use_stateful_helper
    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_verify domain=crowntime.cn"* ]]
    [[ "$output" == *"not manageable via the Qiniu CDN API"* ]]
    [[ "$output" == *"配置 HTTPS"* ]]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
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
    export MOCK_FAIL_BIND_DOMAIN="$CDN_DOMAIN"
    export MOCK_FAIL_BIND_MESSAGE="CDN bind rejected"
    use_stateful_helper

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_bind_cdn domain=crowntime.cn message=CDN bind failed: CDN bind rejected"* ]]
    [[ "$output" != *"command not found"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid" ]
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

# ── Approved warning-only paths ─────────────────────────────────────────

@test "origin bind failure dies with console runbook and records no deployment state (GH-232 fail closed)" {
    fake_acme_sh 0
    fake_sudo 0
    export MOCK_FAIL_BIND_DOMAIN="$ORIGIN_DOMAIN"
    export MOCK_FAIL_BIND_MESSAGE="HTTP 612: no such domain"
    use_stateful_helper

    run "$WILDCARD"
    [ "$status" -eq 1 ]
    [[ "$output" == *"[ERROR] stage=qiniu_bind_origin domain=crowntime.cn message=Origin bind failed: HTTP 612: no such domain"* ]]
    # The journal message must be actionable on its own: full console runbook.
    [[ "$output" == *"配置 HTTPS"* ]]
    [[ "$output" == *"更换证书"* ]]
    [[ "$output" == *".origin_console_fp"* ]]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ ! -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "origin bind failure with recorded console binding continues and records state" {
    fake_acme_sh 0
    fake_sudo 0
    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$HOME/.acme.sh/acme.sh" >/dev/null || true
    local_fp=$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/$REAL_DOMAIN/.origin_console_fp"
    export MOCK_FAIL_BIND_DOMAIN="$ORIGIN_DOMAIN"
    export MOCK_FAIL_BIND_MESSAGE="HTTP 612: no such domain"
    use_stateful_helper

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"console binding recorded"* ]]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_certid" ]
    [ -f "$NGINX_CERT_DIR/fullchain.pem" ]
    grep -q 'sudo -n /usr/bin/systemctl reload nginx' "$TEST_TMPDIR/sudo_invocations.log"
}

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
