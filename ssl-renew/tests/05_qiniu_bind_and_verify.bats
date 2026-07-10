#!/usr/bin/env bats
# lib/qiniu.sh: bind_cert_to_domain() and verify_certID_on_domain() —
# request construction, success, business error codes, HTTP 401/403,
# against a mock qiniu_helper.py (see tests/test_qiniu_helper.py for the
# real-SDK signing parity coverage).

load test_helper/common

setup() {
    common_setup
    source "$SSL_RENEW_ROOT/lib/common.sh"
    source "$SSL_RENEW_ROOT/lib/qiniu.sh"
    export QINIU_ACCESS_KEY=testak QINIU_SECRET_KEY=testsk
}
teardown() { common_teardown; }

# ── 七牛绑定请求构造 ───────────────────────────────────────────────────
@test "bind_cert_to_domain invokes qiniu_helper.py bind with domain + cert-id" {
    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    run bind_cert_to_domain media.crowntime.cn certid-123
    [ "$status" -eq 0 ]
    grep -q -- "bind" "$TEST_TMPDIR/helper.log"
    grep -q -- "--domain media.crowntime.cn" "$TEST_TMPDIR/helper.log"
    grep -q -- "--cert-id certid-123" "$TEST_TMPDIR/helper.log"
}

# ── 七牛绑定成功响应 ───────────────────────────────────────────────────
@test "bind_cert_to_domain succeeds on a normal 200 response" {
    use_mock_qiniu_helper success
    run bind_cert_to_domain media.crowntime.cn certid-123
    [ "$status" -eq 0 ]
}

# ── 七牛绑定失败 / 业务错误码 ────────────────────────────────────────────
@test "bind_cert_to_domain fails on a Qiniu business error" {
    use_mock_qiniu_helper http_error 500
    export MOCK_QINIU_ERROR="Qiniu bind failed — business error code=6100: domain not found"
    run bind_cert_to_domain media.crowntime.cn certid-123
    [ "$status" -eq 1 ]
    [[ "$output" == *"business error code=6100"* ]]
}

# ── HTTP 401/403 ─────────────────────────────────────────────────────────
@test "bind_cert_to_domain treats HTTP 401 as an auth failure" {
    use_mock_qiniu_helper http_error 401
    run bind_cert_to_domain media.crowntime.cn certid-123
    [ "$status" -eq 1 ]
    [[ "$output" == *"HTTP 401"* ]]
}

@test "bind_cert_to_domain treats HTTP 403 as an auth failure" {
    use_mock_qiniu_helper http_error 403
    run bind_cert_to_domain media.crowntime.cn certid-123
    [ "$status" -eq 1 ]
    [[ "$output" == *"HTTP 403"* ]]
}

@test "bind_cert_to_domain in dry-run mode never invokes the helper" {
    export DRY_RUN=1
    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    run bind_cert_to_domain media.crowntime.cn certid-123
    [ "$status" -eq 0 ]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
}

# ── verify_certID_on_domain ───────────────────────────────────────────
@test "verify_certID_on_domain succeeds when the bound certId matches" {
    use_mock_qiniu_helper success
    export MOCK_QINIU_ACTUAL_CERT_ID="certid-123"
    run verify_certID_on_domain media.crowntime.cn certid-123
    [ "$status" -eq 0 ]
    [[ "$output" == *"API certID=certid-123 confirmed"* ]]
}

@test "verify_certID_on_domain fails on certId mismatch" {
    use_mock_qiniu_helper success
    export MOCK_QINIU_ACTUAL_CERT_ID="some-other-certid"
    run verify_certID_on_domain media.crowntime.cn certid-123
    [ "$status" -eq 1 ]
    [[ "$output" == *"certID mismatch"* ]]
    [[ "$output" == *"expected=certid-123"* ]]
    [[ "$output" == *"actual=some-other-certid"* ]]
}

@test "verify_certID_on_domain treats HTTP 401/403 as auth failure, not a mismatch" {
    use_mock_qiniu_helper http_error 403
    run verify_certID_on_domain media.crowntime.cn certid-123
    [ "$status" -eq 1 ]
    [[ "$output" == *"HTTP 403"* ]]
    [[ "$output" != *"mismatch"* ]]
}

@test "verify_certID_on_domain in dry-run mode never invokes the helper" {
    export DRY_RUN=1
    use_mock_qiniu_helper success 200 "$TEST_TMPDIR/helper.log"
    run verify_certID_on_domain media.crowntime.cn certid-123
    [ "$status" -eq 0 ]
    [ ! -f "$TEST_TMPDIR/helper.log" ]
}
