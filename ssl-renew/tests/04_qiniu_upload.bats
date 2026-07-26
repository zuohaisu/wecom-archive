#!/usr/bin/env bats
# lib/qiniu.sh: deploy_to_qiniu() — request construction, success, business
# failure, HTTP 401/403, and network timeout.
#
# Signing/transport correctness is proven by tests/test_qiniu_helper.py
# (golden parity against the official SDK) and the real-helper integration
# tests; this file exercises lib/qiniu.sh's own argv construction, JSON
# response parsing, and error propagation against a mock qiniu_helper.py.

load test_helper/common

setup() {
    common_setup
    source "$SSL_RENEW_ROOT/lib/common.sh"
    source "$SSL_RENEW_ROOT/lib/qiniu.sh"
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
    mkdir -p "$TEST_TMPDIR/cert"
    gen_cert media.example.com \
        "$TEST_TMPDIR/cert/fullchain.cer" \
        "$TEST_TMPDIR/cert/media.example.com.key"
}
teardown() { common_teardown; }

# ── 七牛上传请求构造 ───────────────────────────────────────────────────
@test "deploy_to_qiniu invokes qiniu_helper.py upload with domain + file paths (no secret content)" {
    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    export MOCK_QINIU_CERT_ID="certid-123"
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 0 ]
    grep -q -- "upload" "$TEST_TMPDIR/helper.log"
    grep -q -- "--domain media.example.com" "$TEST_TMPDIR/helper.log"
    grep -q -- "--cert-file $TEST_TMPDIR/cert/fullchain.cer" "$TEST_TMPDIR/helper.log"
    grep -q -- "--key-file $TEST_TMPDIR/cert/media.example.com.key" "$TEST_TMPDIR/helper.log"
}

@test "deploy_to_qiniu fails clearly when fullchain.cer is missing (never invokes the helper)" {
    rm -f "$TEST_TMPDIR/cert/fullchain.cer"
    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 1 ]
    [[ "$output" == *"fullchain.cer not found"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
}

# ── 七牛上传成功响应 ───────────────────────────────────────────────────
@test "deploy_to_qiniu returns the certID on a successful response" {
    use_mock_qiniu_helper success
    export MOCK_QINIU_CERT_ID="certid-abc-987"
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 0 ]
    [ "$output" = "certid-abc-987" ]
}

# ── 七牛上传失败 ───────────────────────────────────────────────────────
@test "deploy_to_qiniu fails when the helper reports a server error" {
    use_mock_qiniu_helper http_error 500
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Qiniu upload failed"* ]]
}

# ── HTTP 401/403 ─────────────────────────────────────────────────────────
@test "deploy_to_qiniu treats HTTP 401 as an auth failure, not a generic error" {
    use_mock_qiniu_helper http_error 401
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 1 ]
    [[ "$output" == *"HTTP 401"* ]]
    [[ "$output" == *"check AK/SK"* ]]
}

@test "deploy_to_qiniu treats HTTP 403 as an auth failure" {
    use_mock_qiniu_helper http_error 403
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 1 ]
    [[ "$output" == *"HTTP 403"* ]]
}

# ── 网络超时 ───────────────────────────────────────────────────────────
@test "deploy_to_qiniu surfaces a network timeout as a clean failure" {
    use_mock_qiniu_helper network_error
    export MOCK_QINIU_ERROR="Qiniu upload request failed (network error / timeout): ReadTimeout"
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 1 ]
    [[ "$output" == *"timeout"* ]]
}

@test "deploy_to_qiniu surfaces a connection failure as a clean failure" {
    use_mock_qiniu_helper network_error
    export MOCK_QINIU_ERROR="Qiniu upload request failed (network error / timeout): ConnectionError"
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 1 ]
    [[ "$output" == *"network error"* ]]
}

# ── dry-run ────────────────────────────────────────────────────────────
@test "deploy_to_qiniu in dry-run mode never invokes the helper" {
    export DRY_RUN=1
    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    run deploy_to_qiniu media.example.com "$TEST_TMPDIR/cert"
    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY-RUN-CERTID"* ]]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
}
