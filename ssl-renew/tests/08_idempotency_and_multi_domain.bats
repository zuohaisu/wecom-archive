#!/usr/bin/env bats
# 幂等执行, 多域名配置, 当前有效证书保护 — renew.sh state-machine behavior.
#
# NOTE: these tests never use a real production hostname —
# only RFC 2606 reserved *.example.test names, which are guaranteed to
# never resolve to a real service. TLS_VERIFY_TIMEOUT/RETRIES are kept
# tiny wherever the full deploy path runs a (fast-failing) HTTPS check.

load test_helper/common

setup() {
    common_setup
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    export TLS_VERIFY_TIMEOUT=1 TLS_VERIFY_RETRIES=1
    mkdir -p "$HOME/.acme.sh"
    RENEW="$SSL_RENEW_ROOT/renew.sh"
}
teardown() { common_teardown; }

fake_acme_sh() {
    local domain="$1" src_cert="$2" src_key="${3:-$TEST_TMPDIR/key.pem}"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$TEST_TMPDIR/acme_invocations.log"
mkdir -p "$HOME/.acme.sh/$domain"
cp "$src_cert" "$HOME/.acme.sh/$domain/fullchain.cer"
cp "$src_key" "$HOME/.acme.sh/$domain/$domain.key"
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
}

# ── 当前有效证书保护 ───────────────────────────────────────────────────
@test "a fully-deployed, TLS-verified cert is never re-uploaded or re-bound" {
    gen_cert renew-a.example.test "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"
    fake_acme_sh renew-a.example.test "$TEST_TMPDIR/cert.pem"
    mkdir -p "$HOME/.acme.sh/renew-a.example.test"
    cp "$TEST_TMPDIR/cert.pem" "$HOME/.acme.sh/renew-a.example.test/fullchain.cer"
    local_fp=$(sha256sum "$HOME/.acme.sh/renew-a.example.test/fullchain.cer" | awk '{print $1}')
    echo "$local_fp" >"$HOME/.acme.sh/renew-a.example.test/.deployed_fp"
    touch "$HOME/.acme.sh/renew-a.example.test/.tls_verified"

    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    run "$RENEW" renew-a.example.test
    [ "$status" -eq 0 ]
    [[ "$output" == *"fully deployed + TLS verified"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
}

# ── 幂等执行 ───────────────────────────────────────────────────────────
@test "running renew.sh twice in a row is a no-op the second time" {
    gen_cert renew-b.example.test "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"
    fake_acme_sh renew-b.example.test "$TEST_TMPDIR/cert.pem"
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="certid-123" MOCK_QINIU_ACTUAL_CERT_ID="certid-123"

    run "$RENEW" renew-b.example.test
    [ "$status" -eq 0 ]

    run "$RENEW" renew-b.example.test
    [ "$status" -eq 0 ]
    [[ "$output" == *"fully deployed"* || "$output" == *"nothing to do"* || "$output" == *"not yet verified"* ]]
}

@test "a concurrent renew.sh run for the same domain exits cleanly instead of racing" {
    gen_cert renew-c.example.test "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"
    fake_acme_sh renew-c.example.test "$TEST_TMPDIR/cert.pem"
    mkdir -p "$HOME/.acme.sh/renew-c.example.test"

    # Simulate a stale lock held by a still-running process (this shell).
    echo $$ >"$HOME/.acme.sh/renew-c.example.test/.renew.lock"

    if command -v flock >/dev/null 2>&1; then
        skip "flock is available; the PID-lock fallback path isn't exercised on this host"
    fi

    run "$RENEW" renew-c.example.test
    [ "$status" -eq 0 ]
    [[ "$output" == *"already in progress"* ]]
}

# ── 多域名配置 ─────────────────────────────────────────────────────────
@test "two domains keep fully independent state and never conflict" {
    gen_cert multi-a.example.test "$TEST_TMPDIR/a.pem" "$TEST_TMPDIR/a.key"
    gen_cert multi-b.example.test "$TEST_TMPDIR/b.pem" "$TEST_TMPDIR/b.key"

    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
domain="\${*: -1}"
echo "\$@" >> "$TEST_TMPDIR/acme_invocations.log"
mkdir -p "\$HOME/.acme.sh/\$domain"
case "\$domain" in
  multi-a.example.test)
    cp "$TEST_TMPDIR/a.pem" "\$HOME/.acme.sh/\$domain/fullchain.cer"
    cp "$TEST_TMPDIR/a.key" "\$HOME/.acme.sh/\$domain/\$domain.key"
    ;;
  multi-b.example.test)
    cp "$TEST_TMPDIR/b.pem" "\$HOME/.acme.sh/\$domain/fullchain.cer"
    cp "$TEST_TMPDIR/b.key" "\$HOME/.acme.sh/\$domain/\$domain.key"
    ;;
esac
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"

    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="certid-a" MOCK_QINIU_ACTUAL_CERT_ID="certid-a"
    run "$SSL_RENEW_ROOT/renew.sh" multi-a.example.test
    [ "$status" -eq 0 ]

    export MOCK_QINIU_CERT_ID="certid-b" MOCK_QINIU_ACTUAL_CERT_ID="certid-b"
    run "$SSL_RENEW_ROOT/renew.sh" multi-b.example.test
    [ "$status" -eq 0 ]

    [ -f "$HOME/.acme.sh/multi-a.example.test/.deployed_fp" ]
    [ -f "$HOME/.acme.sh/multi-b.example.test/.deployed_fp" ]
    fp_a=$(cat "$HOME/.acme.sh/multi-a.example.test/.deployed_fp")
    fp_b=$(cat "$HOME/.acme.sh/multi-b.example.test/.deployed_fp")
    [ "$fp_a" != "$fp_b" ]
}

@test "DOMAIN env var (systemd EnvironmentFile style) and CLI arg must agree" {
    gen_cert renew-d.example.test "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"
    fake_acme_sh renew-d.example.test "$TEST_TMPDIR/cert.pem"
    export DOMAIN=other-domain.example.test
    run "$SSL_RENEW_ROOT/renew.sh" renew-d.example.test
    [ "$status" -eq 2 ]
    [[ "$output" == *"does not match"* ]]
}

@test "DOMAIN env var alone (no CLI arg) is honored, as systemd's EnvironmentFile= supplies it" {
    gen_cert renew-e.example.test "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"
    fake_acme_sh renew-e.example.test "$TEST_TMPDIR/cert.pem"
    export DOMAIN=renew-e.example.test
    export DRY_RUN=1
    run "$SSL_RENEW_ROOT/renew.sh"
    [ "$status" -eq 0 ]
}

@test "per-domain CERT_DIR/STATE_DIR overrides keep two instances from colliding even under one HOME" {
    # renew.sh always calls acme.sh --renew first (regardless of deploy
    # state) — a no-op fake is enough since fullchain.cer is pre-seeded
    # directly into the overridden CERT_DIR below.
    cat >"$HOME/.acme.sh/acme.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"

    gen_cert multi-c.example.test "$TEST_TMPDIR/a.pem" "$TEST_TMPDIR/a.key"
    mkdir -p "$TEST_TMPDIR/state-a"
    cp "$TEST_TMPDIR/a.pem" "$TEST_TMPDIR/state-a/fullchain.cer"
    cp "$TEST_TMPDIR/a.key" "$TEST_TMPDIR/state-a/multi-c.example.test.key"
    fp=$(sha256sum "$TEST_TMPDIR/state-a/fullchain.cer" | awk '{print $1}')
    echo "$fp" >"$TEST_TMPDIR/state-a/.deployed_fp"
    touch "$TEST_TMPDIR/state-a/.tls_verified"

    use_mock_curl success 200 '{"certID":"should-not-be-called"}' "$TEST_TMPDIR/curl.log"
    export CERT_DIR="$TEST_TMPDIR/state-a"
    export STATE_DIR="$TEST_TMPDIR/state-a"
    run "$SSL_RENEW_ROOT/renew.sh" multi-c.example.test
    [ "$status" -eq 0 ]
    [ ! -f "$TEST_TMPDIR/curl.log" ]
}
