#!/usr/bin/env bats
# GH-231 regression coverage: Qiniu origin/CDN binding reconciliation on the
# "fingerprint already deployed" pass. All ACME, Qiniu, sudo, and nginx
# interactions are mocks; no test contacts a real provider, changes DNS,
# reloads a local nginx, or uses production credentials.

load test_helper/common

WILDCARD_SOURCE="$SSL_RENEW_ROOT/renew-wildcard.sh"
REAL_DOMAIN="crowntime.cn"
CDN_DOMAIN="media.crowntime.cn"
ORIGIN_DOMAIN="media-origin.crowntime.cn"
STALE_FP="6a5ef31fcb904e2c369b43fddeadbeefdeadbeefdeadbeefdeadbeefdeadbe"

setup() {
    common_setup
    # Defensive: a developer/CI shell that leaks MOCK_QINIU_* exports must
    # not silently flip reconciliation decisions in these tests. Same
    # philosophy as common_setup's proxy unset.
    unset MOCK_QINIU_MODE MOCK_QINIU_STATUS MOCK_QINIU_CERT_ID \
        MOCK_QINIU_ACTUAL_CERT_ID MOCK_QINIU_ERROR MOCK_QINIU_LOG \
        MOCK_QINIU_BOUND_CERT_FP MOCK_QINIU_BOUND_CERT_ID \
        MOCK_QINIU_NOT_AFTER MOCK_QINIU_CERT_DETAIL_MISSING \
        MOCK_QINIU_FAIL_BIND_DOMAIN
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

    export ORIGIN_DOMAIN
    export QINIU_BUCKET="365-wecom-media"
}

teardown() { common_teardown; }

# fake_acme_sh_skipped installs an acme.sh that reports "not due yet"
# (exit 2) while leaving the existing certificate pair in place — the daily
# production case in which the GH-231 bug kept the origin binding stale.
fake_acme_sh_skipped() {
    gen_cert "$REAL_DOMAIN" "$TEST_TMPDIR/fullchain.cer" "$TEST_TMPDIR/privkey.key"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TEST_TMPDIR/acme_invocations.log"
exit 2
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"

    mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
    "$REAL_CP" "$TEST_TMPDIR/fullchain.cer" "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer"
    "$REAL_CP" "$TEST_TMPDIR/privkey.key" "$HOME/.acme.sh/$REAL_DOMAIN/$REAL_DOMAIN.key"

    # Simulate a completed earlier deploy: fingerprint state matches, TLS
    # state clean. .deployed_fp hashes the fullchain FILE (script semantics).
    sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}' \
        >"$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp"
}

# leaf_fp prints the sha256 of the leaf certificate's DER — the value the
# script compares against the fingerprint reported by `query`.
leaf_fp() {
    openssl x509 -in "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" -outform DER | sha256sum | awk '{print $1}'
}

# unix_now_plus <days> prints a unix timestamp <days> from now (GNU/BSD date).
unix_now_plus() {
    date -u -v+"$1"d +%s 2>/dev/null || date -u -d "+$1 days" +%s
}

fake_sudo() {
    local exit_code="${1:-0}"
    cat >"$MOCK_BIN_DIR/sudo" <<EOF
#!/usr/bin/env bash
echo "sudo \$*" >>"$TEST_TMPDIR/sudo_invocations.log"
exit $exit_code
EOF
    chmod +x "$MOCK_BIN_DIR/sudo"
}

helper_calls() {
    [ -f "$TEST_TMPDIR/helper.log" ] && cat "$TEST_TMPDIR/helper.log" || true
}

# ── The four GH-231 Fix-Requirements-6 cases ─────────────────────────────

@test "acme skipped + origin bound to stale cert reconciles origin only (CDN untouched)" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    # CDN already serves the current wildcard cert; origin still points at
    # the July certificate — the exact production incident.
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$STALE_FP"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"not due yet"* ]]
    [[ "$output" == *"decision=stale-cert bound_fp=${STALE_FP:0:12}"* ]]
    [[ "$output" == *"bucket=365-wecom-media"* ]]
    [[ "$output" == *"reconciliation needed for: $ORIGIN_DOMAIN"* ]]
    [[ "$output" == *"[OK] reconciled: bound to $ORIGIN_DOMAIN"* ]]
    [[ "$output" == *"cert already deployed"* ]]

    calls=$(helper_calls)
    [[ "$calls" == *"query --domain $CDN_DOMAIN"* ]]
    [[ "$calls" == *"query --domain $ORIGIN_DOMAIN"* ]]
    [[ "$calls" == *"upload --domain $REAL_DOMAIN"* ]]
    [[ "$calls" == *"bind --domain $ORIGIN_DOMAIN"* ]]
    ! grep -q "bind --domain $CDN_DOMAIN" "$TEST_TMPDIR/helper.log"

    # Reconciliation must not touch nginx, local state, or the TLS flags:
    # the local certificate did not change, only a remote binding did.
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp")" = "$(sha256sum "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer" | awk '{print $1}')" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
}

@test "origin bound to current cert is a read-only no-op" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$local_fp"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"decision=in-sync"* ]]
    [[ "$output" == *"all Qiniu bindings in sync"* ]]
    [[ "$output" == *"cert already deployed — nothing to do"* ]]

    calls=$(helper_calls)
    [[ "$calls" == *"query --domain $CDN_DOMAIN"* ]]
    [[ "$calls" == *"query --domain $ORIGIN_DOMAIN"* ]]
    ! grep -q "upload" "$TEST_TMPDIR/helper.log"
    ! grep -q "bind" "$TEST_TMPDIR/helper.log"
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "ORIGIN_DOMAIN unset keeps the legacy zero-API-call no-op" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    unset ORIGIN_DOMAIN QINIU_BUCKET

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already deployed"* ]]
    [[ "$output" != *"binding check"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "dry-run with a stale origin prints the plan and performs zero write calls" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$STALE_FP"
    exported_fp=$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp")

    DRY_RUN=1 run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[DRY-RUN]"* ]]
    [[ "$output" == *"would run"* ]]
    [[ "$output" == *"reconciliation needed for: $ORIGIN_DOMAIN"* ]]
    [[ "$output" == *"reconciliation plan complete — zero external writes performed"* ]]

    # Read-only queries happened; the upload/bind steps never reached the
    # helper (lib/qiniu.sh dry-run guards) — zero external writes.
    calls=$(helper_calls)
    [[ "$calls" == *"query --domain $ORIGIN_DOMAIN"* ]]
    ! grep -q "upload" "$TEST_TMPDIR/helper.log"
    ! grep -q "bind" "$TEST_TMPDIR/helper.log"
    [ "$(cat "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp")" = "$exported_fp" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
    [ ! -f "$NGINX_CERT_DIR/fullchain.pem" ]
}

# ── Decision-rule details ────────────────────────────────────────────────

@test "near-expiry bound cert is reconciled even when the fingerprint matches" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$local_fp"
    export MOCK_QINIU_NOT_AFTER="$(unix_now_plus 5)"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"decision=near-expiry"* ]]
    [[ "$output" == *"days_left=5"* ]]
    [[ "$output" == *"reconciliation needed for: $CDN_DOMAIN $ORIGIN_DOMAIN"* ]]
    grep -q "bind --domain $CDN_DOMAIN" "$TEST_TMPDIR/helper.log"
    grep -q "bind --domain $ORIGIN_DOMAIN" "$TEST_TMPDIR/helper.log"
}

@test "ORIGIN_CERT_MIN_VALID_DAYS widens the near-expiry window both ways" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$local_fp"
    export MOCK_QINIU_NOT_AFTER="$(unix_now_plus 20)"

    # 20 days left is outside the widened 30-day window → reconcile.
    ORIGIN_CERT_MIN_VALID_DAYS=30 run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"decision=near-expiry"* ]]
    grep -q "bind --domain $ORIGIN_DOMAIN" "$TEST_TMPDIR/helper.log"

    # 20 days left is outside the widened 30-day window → reconcile; with
    # the tightened 2-day window 20 days is comfortably inside → in-sync.
    rm -f "$TEST_TMPDIR/helper.log"
    ORIGIN_CERT_MIN_VALID_DAYS=2 run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"all Qiniu bindings in sync"* ]]
    ! grep -q "upload" "$TEST_TMPDIR/helper.log"
}

@test "ORIGIN_CERT_MIN_VALID_DAYS non-numeric value warns and falls back to 14" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$local_fp"
    export MOCK_QINIU_NOT_AFTER="$(unix_now_plus 5)"

    ORIGIN_CERT_MIN_VALID_DAYS=soon run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARN]"* ]]
    [[ "$output" == *"ORIGIN_CERT_MIN_VALID_DAYS='soon' is not a non-negative integer"* ]]
    # Fallback default 14 ⇒ the 5-day cert is reconciled.
    [[ "$output" == *"decision=near-expiry"* ]]
}

@test "absent origin binding (no httpsconf) is reconciled" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    # CDN in sync; origin has no HTTPS configuration at all.
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"decision=no-binding"* ]]
    [[ "$output" == *"reconciliation needed for: $ORIGIN_DOMAIN"* ]]
    grep -q "bind --domain $ORIGIN_DOMAIN" "$TEST_TMPDIR/helper.log"
}

@test "bound certID no longer resolving (detail 404) is reconciled" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    local_fp=$(leaf_fp)
    export MOCK_QINIU_BOUND_CERT_FP="$CDN_DOMAIN=$local_fp,$ORIGIN_DOMAIN=$local_fp"
    export MOCK_QINIU_CERT_DETAIL_MISSING=1

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"decision=bound-cert-missing"* ]]
    [[ "$output" == *"reconciliation needed for: $CDN_DOMAIN $ORIGIN_DOMAIN"* ]]
}

@test "binding query failure warns and performs no writes" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper http_error 500 "$TEST_TMPDIR/helper.log"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARN] stage=binding_reconcile domain=crowntime.cn message=binding query failed for $CDN_DOMAIN"* ]]
    [[ "$output" != *"reconciliation needed"* ]]
    calls=$(helper_calls)
    [[ "$calls" == *"query --domain $CDN_DOMAIN"* ]]
    ! grep -q "upload" "$TEST_TMPDIR/helper.log"
    ! grep -q "bind" "$TEST_TMPDIR/helper.log"
}

@test "reconcile bind failure warns, preserves exit 0, and still reconciles the healthy domain" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_BOUND_CERT_FP="$STALE_FP"
    export MOCK_QINIU_FAIL_BIND_DOMAIN="$ORIGIN_DOMAIN"

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[WARN] stage=binding_reconcile domain=crowntime.cn message=reconcile bind failed for $ORIGIN_DOMAIN"* ]]
    [[ "$output" == *"[OK] reconciled: bound to $CDN_DOMAIN"* ]]
    [[ "$output" == *"cert already deployed"* ]]
}

# ── Mode contracts and the deploy path ───────────────────────────────────

@test "staging mode skips reconciliation entirely" {
    fake_acme_sh_skipped
    fake_sudo 0
    use_mock_qiniu_helper success "" "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_BOUND_CERT_FP="$STALE_FP"

    ACME_STAGING=1 run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"staging mode — skipping binding reconciliation"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
    [ ! -f "$TEST_TMPDIR/sudo_invocations.log" ]
}

@test "deploy path without ORIGIN_DOMAIN logs the origin-bind skip" {
    gen_cert "$REAL_DOMAIN" "$TEST_TMPDIR/fullchain.cer" "$TEST_TMPDIR/privkey.key"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
mkdir -p "$HOME/.acme.sh/$REAL_DOMAIN"
"$REAL_CP" "$TEST_TMPDIR/fullchain.cer" "$HOME/.acme.sh/$REAL_DOMAIN/fullchain.cer"
"$REAL_CP" "$TEST_TMPDIR/privkey.key" "$HOME/.acme.sh/$REAL_DOMAIN/$REAL_DOMAIN.key"
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
    fake_sudo 0
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="wildcard-certid"
    unset ORIGIN_DOMAIN

    run "$WILDCARD"
    [ "$status" -eq 0 ]
    [[ "$output" == *"bound to $CDN_DOMAIN (CDN)"* ]]
    [[ "$output" == *"ORIGIN_DOMAIN not configured — skipping origin bind"* ]]
    [ -f "$HOME/.acme.sh/$REAL_DOMAIN/.deployed_fp" ]
}
