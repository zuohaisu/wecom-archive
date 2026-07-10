#!/usr/bin/env bats
# verify_https.sh: TLS/HTTPS verification against a local mock TLS server
# and openssl-generated fixture certificates. No real DNS/network is used.

load test_helper/common

setup() {
    common_setup
    gen_cert test.example.test "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"
    VERIFY="$SSL_RENEW_ROOT/verify_https.sh"
}
teardown() { common_teardown; }

# ── HTTPS 验证成功 ─────────────────────────────────────────────────────
@test "verify_https.sh succeeds end-to-end against a valid matching cert" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --insecure-http-check --timeout 5 --retries 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"all HTTPS checks passed"* ]]
}

# ── HTTPS 域名不匹配 ───────────────────────────────────────────────────
@test "verify_https.sh fails with a distinct code on domain mismatch" {
    gen_cert wrong.example.test "$TEST_TMPDIR/wrong.pem" "$TEST_TMPDIR/wrong.key"
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/wrong.pem" --key "$TEST_TMPDIR/wrong.key" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" right.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --timeout 5 --retries 1
    [ "$status" -eq 13 ]
    [[ "$output" == *"does not cover domain"* ]]
}

# ── 证书已过期 ─────────────────────────────────────────────────────────
@test "verify_https.sh fails with a distinct code on an expired certificate" {
    gen_expired_cert expired.example.test "$TEST_TMPDIR/exp.pem" "$TEST_TMPDIR/exp.key"
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/exp.pem" --key "$TEST_TMPDIR/exp.key" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" expired.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --timeout 5 --retries 1
    [ "$status" -eq 15 ]
    [[ "$output" == *"certificate has expired"* ]]
}

# ── 证书即将过期 ───────────────────────────────────────────────────────
@test "verify_https.sh fails with a distinct code when under the min-days threshold" {
    gen_soon_expiring_cert soon.example.test "$TEST_TMPDIR/soon.pem" "$TEST_TMPDIR/soon.key" 2
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/soon.pem" --key "$TEST_TMPDIR/soon.key" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" soon.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --min-days 15 --timeout 5 --retries 1
    [ "$status" -eq 16 ]
    [[ "$output" == *"expires within"* ]]
}

@test "verify_https.sh passes the min-days check when validity comfortably exceeds it" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check --min-days 15 --timeout 5 --retries 1
    [ "$status" -eq 0 ]
}

# ── 证书链验证成功/失败 ─────────────────────────────────────────────────
@test "verify_https.sh chain verification succeeds against a trusted CA bundle" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check --timeout 5 --retries 1
    [ "$status" -eq 0 ]
}

@test "verify_https.sh chain verification fails for a self-signed cert against the system store" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    run "$VERIFY" test.example.test --host 127.0.0.1 --port "$port" --skip-dns-check --timeout 5 --retries 1
    [ "$status" -eq 14 ]
    [[ "$output" == *"chain verification failed"* ]]
}

# ── DNS 解析失败 ───────────────────────────────────────────────────────
@test "verify_https.sh fails with a distinct code when DNS resolution fails" {
    run "$VERIFY" definitely-not-a-real-domain.invalid --timeout 3 --retries 1
    [ "$status" -eq 10 ]
    [[ "$output" == *"DNS resolution failed"* ]]
}

@test "--skip-dns-check bypasses the DNS resolution step" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check --timeout 5 --retries 1
    [ "$status" -eq 0 ]
}

# ── TLS 1.2 可用 ───────────────────────────────────────────────────────
@test "verify_https.sh confirms TLS 1.2 availability against a normal server" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check --timeout 5 --retries 1
    [[ "$output" == *"TLS 1.2 available"* ]]
}

@test "verify_https.sh fails with a distinct code when the server rejects TLS 1.2" {
    port=$(find_free_port)
    start_tls13_only_server "$port" "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem" >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check --timeout 5 --retries 1
    [ "$status" -eq 17 ]
    [[ "$output" == *"TLS 1.2 handshake failed"* ]]
}

# ── HTTP 响应可取得 / 401/403 视为成功 ──────────────────────────────────
@test "verify_https.sh treats a 200 HTTP response as reachable" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --insecure-http-check --timeout 5 --retries 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"HTTP reachable (status=200)"* ]]
}

@test "verify_https.sh treats HTTP 401 as proof of successful TLS + routing" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 401 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --insecure-http-check --timeout 5 --retries 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"status=401"* ]]
}

@test "verify_https.sh treats HTTP 403 as proof of successful TLS + routing" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 403 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --insecure-http-check --timeout 5 --retries 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"status=403"* ]]
}

@test "verify_https.sh fails with a distinct code on a 500 HTTP response" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 500 >/dev/null
    export VERIFY_HTTPS_CA_BUNDLE="$TEST_TMPDIR/cert.pem"
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --insecure-http-check --timeout 5 --retries 1
    [ "$status" -eq 18 ]
    [[ "$output" == *"HTTP status unusable"* ]]
}

# ── 证书指纹校验 ───────────────────────────────────────────────────────
@test "verify_https.sh fingerprint check passes for the matching cert" {
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check \
        --cert-file "$TEST_TMPDIR/cert.pem" --timeout 5 --retries 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"fingerprint matches"* ]]
}

@test "verify_https.sh fingerprint check fails with a distinct code on mismatch" {
    gen_cert other.example.test "$TEST_TMPDIR/other.pem" "$TEST_TMPDIR/other.key"
    port=$(find_free_port)
    start_mock_server "$port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" test.example.test \
        --host 127.0.0.1 --port "$port" --skip-dns-check --skip-http-check \
        --cert-file "$TEST_TMPDIR/other.pem" --timeout 5 --retries 1
    [ "$status" -eq 19 ]
    [[ "$output" == *"fingerprint mismatch"* ]]
}

# ── 超时 / 重试 / 连接失败 ──────────────────────────────────────────────
@test "verify_https.sh fails with a distinct code when the connection is refused" {
    port=$(find_free_port)
    run "$VERIFY" test.example.test --host 127.0.0.1 --port "$port" --skip-dns-check --timeout 2 --retries 1
    [ "$status" -eq 11 ]
    [[ "$output" == *"refused"* ]]
}

@test "verify_https.sh retries the configured number of times before giving up" {
    port=$(find_free_port)
    run "$VERIFY" test.example.test --host 127.0.0.1 --port "$port" --skip-dns-check --timeout 1 --retries 3 --retry-delay 1
    [ "$status" -eq 11 ]
    attempt_count=$(echo "$output" | grep -c "TLS connect attempt")
    [ "$attempt_count" -eq 3 ]
}

# ── 不同故障返回不同非零退出码 ──────────────────────────────────────────
@test "verify_https.sh exit codes are distinct across failure modes" {
    port=$(find_free_port)
    run "$VERIFY" test.example.test --host 127.0.0.1 --port "$port" --skip-dns-check --timeout 1 --retries 1
    connect_refused_status="$status"

    gen_expired_cert e2.example.test "$TEST_TMPDIR/e2.pem" "$TEST_TMPDIR/e2.key"
    port2=$(find_free_port)
    start_mock_server "$port2" --tls --cert "$TEST_TMPDIR/e2.pem" --key "$TEST_TMPDIR/e2.key" --status 200 >/dev/null
    export VERIFY_HTTPS_SKIP_CHAIN=1
    run "$VERIFY" e2.example.test --host 127.0.0.1 --port "$port2" --skip-dns-check --timeout 3 --retries 1
    expired_status="$status"
    unset VERIFY_HTTPS_SKIP_CHAIN

    run "$VERIFY" not-a-real-domain.invalid --timeout 2 --retries 1
    dns_status="$status"

    [ "$connect_refused_status" -ne "$expired_status" ]
    [ "$expired_status" -ne "$dns_status" ]
    [ "$connect_refused_status" -ne "$dns_status" ]
}

# ── 使用说明 / 参数错误 ─────────────────────────────────────────────────
@test "verify_https.sh with no domain argument exits with the usage error code" {
    run "$VERIFY"
    [ "$status" -eq 2 ]
}

@test "verify_https.sh rejects a malformed domain" {
    run "$VERIFY" "not_a_domain"
    [ "$status" -eq 2 ]
}
