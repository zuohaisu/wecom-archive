#!/usr/bin/env bats
# dry-run mode: no network side effects, predictable exit code, no secrets
# leaked, and README-documented invocation forms both work.

load test_helper/common

setup() {
    common_setup
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    mkdir -p "$HOME/.acme.sh/media.crowntime.cn"
    gen_cert media.crowntime.cn \
        "$HOME/.acme.sh/media.crowntime.cn/fullchain.cer" \
        "$HOME/.acme.sh/media.crowntime.cn/media.crowntime.cn.key"
}
teardown() { common_teardown; }

@test "renew.sh --dry-run exits 0 and prints a plan" {
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY-RUN"* ]]
    [[ "$output" == *"dry-run plan complete"* ]]
}

@test "DRY_RUN=1 env var form is equivalent to --dry-run" {
    DRY_RUN=1 run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn
    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY-RUN"* ]]
}

@test "dry-run never invokes curl (no Qiniu upload/bind/verify)" {
    use_mock_curl success 200 '{"certID":"should-not-be-called"}' "$TEST_TMPDIR/curl.log"
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --dry-run
    [ "$status" -eq 0 ]
    [ ! -f "$TEST_TMPDIR/curl.log" ]
}

@test "dry-run does not write any deployment state files" {
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --dry-run
    [ "$status" -eq 0 ]
    [ ! -f "$HOME/.acme.sh/media.crowntime.cn/.deployed_fp" ]
    [ ! -f "$HOME/.acme.sh/media.crowntime.cn/.tls_verified" ]
}

@test "dry-run does not require real Qiniu credentials" {
    unset QINIU_ACCESS_KEY QINIU_SECRET_KEY
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --dry-run
    [ "$status" -eq 0 ]
}

@test "dry-run output contains no secret material" {
    export QINIU_SECRET_KEY="super-secret-value-12345"
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" != *"super-secret-value-12345"* ]]
}

@test "dry-run rejects bad usage with a non-zero, non-network exit code" {
    run "$SSL_RENEW_ROOT/renew.sh" --dry-run
    [ "$status" -eq 2 ]
}

@test "notify.sh honors DRY_RUN and never calls the webhook" {
    DRY_RUN=1 ALERT_WEBHOOK_URL="http://127.0.0.1:1/hook" run "$SSL_RENEW_ROOT/notify.sh" ERROR media.crowntime.cn "boom"
    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY-RUN"* ]]
}
