#!/usr/bin/env bats
# --staging / ACME_STAGING=1: uses the Let's Encrypt staging CA and never
# touches production Qiniu deployment.

load test_helper/common

setup() {
    common_setup
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    # Fake acme.sh that records its invocation and "issues" a cert.
    mkdir -p "$HOME/.acme.sh"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$TEST_TMPDIR/acme_invocations.log"
mkdir -p "$HOME/.acme.sh/media.crowntime.cn"
cp "$TEST_TMPDIR/fake-fullchain.cer" "$HOME/.acme.sh/media.crowntime.cn/fullchain.cer" 2>/dev/null || true
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"
    gen_cert media.crowntime.cn "$TEST_TMPDIR/fake-fullchain.cer" "$TEST_TMPDIR/fake.key"
}
teardown() { common_teardown; }

@test "--staging passes --staging through to acme.sh" {
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --staging
    [ "$status" -eq 0 ]
    grep -q -- "--staging" "$TEST_TMPDIR/acme_invocations.log"
}

@test "ACME_STAGING=1 env var form is equivalent to --staging" {
    ACME_STAGING=1 run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn
    [ "$status" -eq 0 ]
    grep -q -- "--staging" "$TEST_TMPDIR/acme_invocations.log"
}

@test "staging mode never calls Qiniu (no upload/bind/verify)" {
    use_mock_curl success 200 '{"certID":"should-not-happen"}' "$TEST_TMPDIR/curl.log"
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --staging
    [ "$status" -eq 0 ]
    [ ! -f "$TEST_TMPDIR/curl.log" ]
}

@test "staging mode does not write .deployed_fp / .tls_verified" {
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --staging
    [ "$status" -eq 0 ]
    [ ! -f "$HOME/.acme.sh/media.crowntime.cn/.deployed_fp" ]
    [ ! -f "$HOME/.acme.sh/media.crowntime.cn/.tls_verified" ]
}

@test "production mode (no flags) does not pass --staging to acme.sh" {
    # Production mode proceeds past acme.sh into the Qiniu deploy path —
    # curl MUST be mocked here so this test never reaches the real Qiniu API.
    use_mock_curl success 200 '{"certID":"prod-test-certid"}'
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn
    [ -f "$TEST_TMPDIR/acme_invocations.log" ]
    run grep -- "--staging" "$TEST_TMPDIR/acme_invocations.log"
    [ "$status" -ne 0 ]
}

@test "staging output clearly states deployment was skipped" {
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --staging
    [[ "$output" == *"skipping Qiniu production deployment"* ]]
}
