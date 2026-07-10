#!/usr/bin/env bats
# Full mock integration: certificate available → upload → certID → bind →
# HTTPS verification → success. Plus three independent failure paths:
# Qiniu upload failure, Qiniu bind failure, HTTPS verification failure.
#
# Qiniu calls go through the REAL qiniu_helper.py (official SDK signing +
# real HTTP client) pointed at a local mock_server.py via QINIU_API_HOST —
# not a stubbed shell script — so this is a genuine end-to-end proof that
# request construction, signing, and response parsing all work together.
# The HTTPS-verification leg talks to a real local mock TLS server via
# verify_https.sh. No production service is ever contacted.

load test_helper/common

setup() {
    common_setup
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    mkdir -p "$HOME/.acme.sh"
    RENEW="$SSL_RENEW_ROOT/renew.sh"
    DOMAIN="integration.example.test"
    gen_cert "$DOMAIN" "$TEST_TMPDIR/cert.pem" "$TEST_TMPDIR/key.pem"

    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$TEST_TMPDIR/acme_invocations.log"
mkdir -p "$HOME/.acme.sh/$DOMAIN"
cp "$TEST_TMPDIR/cert.pem" "$HOME/.acme.sh/$DOMAIN/fullchain.cer"
cp "$TEST_TMPDIR/key.pem" "$HOME/.acme.sh/$DOMAIN/$DOMAIN.key"
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
}
teardown() { common_teardown; }

# ── Full success chain ────────────────────────────────────────────────
@test "integration: cert available -> upload -> certID -> bind -> https verify -> success" {
    tls_port=$(find_free_port)
    start_mock_server "$tls_port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null

    qiniu_port=$(find_free_port)
    start_mock_server "$qiniu_port" --body '{"certId":"integration-certid","certID":"integration-certid"}' \
        --log-file "$TEST_TMPDIR/qiniu.log" >/dev/null

    # This test calls the qiniu.sh functions directly (bypassing renew.sh
    # and its acme.sh invocation), so seed the "certificate available"
    # precondition explicitly.
    mkdir -p "$HOME/.acme.sh/$DOMAIN"
    cp "$TEST_TMPDIR/cert.pem" "$HOME/.acme.sh/$DOMAIN/fullchain.cer"
    cp "$TEST_TMPDIR/key.pem" "$HOME/.acme.sh/$DOMAIN/$DOMAIN.key"

    export QINIU_API_HOST="http://127.0.0.1:$qiniu_port"

    run bash -c "
        source '$SSL_RENEW_ROOT/lib/common.sh'
        source '$SSL_RENEW_ROOT/lib/qiniu.sh'
        export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
        export HOME='$HOME'
        export QINIU_API_HOST='$QINIU_API_HOST'
        certID=\$(deploy_to_qiniu '$DOMAIN' '$HOME/.acme.sh/$DOMAIN') || exit 1
        echo \"[TEST] certID=\$certID\"
        bind_cert_to_domain '$DOMAIN' \"\$certID\" || exit 2
        echo '[TEST] bind ok'
        verify_certID_on_domain '$DOMAIN' \"\$certID\" || exit 3
        echo '[TEST] api verify ok'
        VERIFY_HTTPS_CA_BUNDLE='$TEST_TMPDIR/cert.pem' '$SSL_RENEW_ROOT/verify_https.sh' '$DOMAIN' \
            --host 127.0.0.1 --port '$tls_port' --skip-dns-check --insecure-http-check \
            --timeout 5 --retries 1 || exit 4
        echo '[TEST] https verify ok'
    "
    [ "$status" -eq 0 ]
    [[ "$output" == *"certID=integration-certid"* ]]
    [[ "$output" == *"bind ok"* ]]
    [[ "$output" == *"api verify ok"* ]]
    [[ "$output" == *"https verify ok"* ]]
    [[ "$output" == *"all HTTPS checks passed"* ]]

    # Prove the real signing path actually ran: three real HTTP requests
    # (POST upload, PUT bind, GET verify) reached the mock server, with
    # the certificate/private key content present in the POST body (the
    # request the mock received) — i.e. this is not a stub.
    [ "$(wc -l <"$TEST_TMPDIR/qiniu.log" | tr -d ' ')" = "3" ]
    grep -q "^POST .*BEGIN PRIVATE KEY" "$TEST_TMPDIR/qiniu.log"
    grep -q "^PUT " "$TEST_TMPDIR/qiniu.log"
    grep -q "^GET " "$TEST_TMPDIR/qiniu.log"
}

# renew.sh's own end-to-end run through the mocked Qiniu chain (real
# helper, real signing, real local HTTP server). renew.sh always verifies
# HTTPS against the real $DOMAIN with no --host override, so with a
# non-resolving test domain the HTTPS leg here fails at the DNS step —
# that's the same non-fatal path exercised deliberately by the "HTTPS
# verification failure" test below. The genuinely successful HTTPS leg is
# covered end-to-end by the mock-server-backed test above.
@test "integration: renew.sh end-to-end reaches deployment complete" {
    qiniu_port=$(find_free_port)
    start_mock_server "$qiniu_port" --body '{"certId":"integration-certid","certID":"integration-certid"}' >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$qiniu_port"
    export TLS_VERIFY_TIMEOUT=1 TLS_VERIFY_RETRIES=1

    run "$RENEW" "$DOMAIN"
    [ "$status" -eq 0 ]
    [[ "$output" == *"deployment complete"* ]]
    [ -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
}

# ── Failure path 1: Qiniu upload failure ──────────────────────────────
@test "integration failure path: Qiniu upload failure stops before bind/verify" {
    qiniu_port=$(find_free_port)
    start_mock_server "$qiniu_port" --status 500 --body '{"error":"upload rejected"}' \
        --log-file "$TEST_TMPDIR/qiniu.log" >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$qiniu_port"

    run "$RENEW" "$DOMAIN"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Qiniu upload failed"* ]]
    [ ! -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
    # Only the upload POST should have been attempted — no bind (PUT).
    [ "$(wc -l <"$TEST_TMPDIR/qiniu.log" | tr -d ' ')" = "1" ]
    grep -q "^POST " "$TEST_TMPDIR/qiniu.log"
}

# ── Failure path 2: Qiniu bind failure ─────────────────────────────────
@test "integration failure path: Qiniu bind failure stops before HTTPS verify" {
    tls_port=$(find_free_port)
    start_mock_server "$tls_port" --tls --cert "$TEST_TMPDIR/cert.pem" --key "$TEST_TMPDIR/key.pem" --status 200 >/dev/null

    qiniu_port=$(find_free_port)
    # POST (upload) succeeds; PUT (bind) fails with a Qiniu business error.
    start_mock_server "$qiniu_port" \
        --post-body '{"certID":"integration-certid"}' \
        --put-status 200 --put-body '{"code":6100,"error":"domain not bound"}' \
        --log-file "$TEST_TMPDIR/qiniu.log" >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$qiniu_port"
    export TLS_VERIFY_TIMEOUT=1 TLS_VERIFY_RETRIES=1

    run "$RENEW" "$DOMAIN"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Qiniu bind failed"* ]]
    [ ! -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
    [ "$(wc -l <"$TEST_TMPDIR/qiniu.log" | tr -d ' ')" = "2" ]
}

# ── Failure path 3: HTTPS verification failure (non-fatal by design) ──
@test "integration failure path: HTTPS verification failure warns but preserves the deployment" {
    # $DOMAIN is an RFC 2606 reserved, guaranteed-non-resolving name, so
    # renew.sh's internal HTTPS verification (against the real $DOMAIN,
    # with no --host override available at this layer) fails at the DNS
    # step. Per the documented state machine that is NOT fatal: the
    # API-confirmed deployment stands and is re-checked on the next run.
    qiniu_port=$(find_free_port)
    start_mock_server "$qiniu_port" --body '{"certId":"integration-certid","certID":"integration-certid"}' >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$qiniu_port"
    export TLS_VERIFY_TIMEOUT=1 TLS_VERIFY_RETRIES=1
    export CERT_DIR="$HOME/.acme.sh/$DOMAIN"

    run "$RENEW" "$DOMAIN"
    [ "$status" -eq 0 ]
    [[ "$output" == *"HTTPS verification not yet passing"* ]]
    [[ "$output" == *"certID=integration-certid confirmed via API"* ]]
    [ -f "$HOME/.acme.sh/$DOMAIN/.deployed_fp" ]
    [ ! -f "$HOME/.acme.sh/$DOMAIN/.tls_verified" ]
    [ -f "$HOME/.acme.sh/$DOMAIN/.tls_mismatch_days" ]
}
