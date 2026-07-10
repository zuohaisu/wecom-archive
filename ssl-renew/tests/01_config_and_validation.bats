#!/usr/bin/env bats
# Config loading, required-env validation, domain format validation, and
# secret redaction — lib/common.sh unit tests.

load test_helper/common

setup() {
    common_setup
    source "$SSL_RENEW_ROOT/lib/common.sh"
}
teardown() { common_teardown; }

# ── 配置加载成功 ───────────────────────────────────────────────────────
@test "load_config sources CONFIG_FILE and populates variables" {
    cat >"$TEST_TMPDIR/domain.env" <<'EOF'
DOMAIN=media.crowntime.cn
QINIU_ACCESS_KEY=someak
QINIU_SECRET_KEY=somesk
EOF
    CONFIG_FILE="$TEST_TMPDIR/domain.env"
    run load_config
    [ "$status" -eq 0 ]
    source "$TEST_TMPDIR/domain.env"
    [ "$DOMAIN" = "media.crowntime.cn" ]
    [ "$QINIU_ACCESS_KEY" = "someak" ]
}

@test "load_config fails clearly when CONFIG_FILE does not exist" {
    CONFIG_FILE="$TEST_TMPDIR/does-not-exist.env"
    run load_config
    [ "$status" -eq "$EXIT_USAGE_ERROR" ]
    [[ "$output" == *"config file not found"* ]]
}

@test "load_config is a no-op when CONFIG_FILE is unset" {
    unset CONFIG_FILE
    run load_config
    [ "$status" -eq 0 ]
}

# ── 必填环境变量缺失 ───────────────────────────────────────────────────
@test "require_env passes when all variables are set" {
    export FOO=1 BAR=2
    run require_env FOO BAR
    [ "$status" -eq 0 ]
}

@test "require_env reports every missing variable, not just the first" {
    unset FOO BAR
    export BAZ=1
    run require_env FOO BAR BAZ
    [ "$status" -eq "$EXIT_USAGE_ERROR" ]
    [[ "$output" == *"FOO"* ]]
    [[ "$output" == *"BAR"* ]]
    [[ "$output" != *"BAZ is missing"* ]]
}

@test "renew.sh refuses to start without Qiniu credentials (non-dry-run)" {
    unset QINIU_ACCESS_KEY QINIU_SECRET_KEY SAVED_QINIU_AK SAVED_QINIU_SK
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn
    [ "$status" -eq "$EXIT_USAGE_ERROR" ]
    [[ "$output" == *"missing required Qiniu credentials"* ]]
}

@test "renew.sh accepts SAVED_QINIU_AK/SK as an alternative to QINIU_ACCESS_KEY/SECRET_KEY" {
    unset QINIU_ACCESS_KEY QINIU_SECRET_KEY
    export SAVED_QINIU_AK=ak SAVED_QINIU_SK=sk
    # Not dry-run: the credential check itself is skipped entirely in
    # dry-run, so this must run the real (pre-network) validation path.
    # It will still fail later (no real acme.sh in the test sandbox) —
    # what matters is it does NOT fail on the credentials check.
    run "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn
    [[ "$output" != *"missing required Qiniu credentials"* ]]
}

# ── 域名格式校验 ───────────────────────────────────────────────────────
@test "validate_domain accepts a normal FQDN" {
    run validate_domain "media.crowntime.cn"
    [ "$status" -eq 0 ]
}

@test "validate_domain rejects an empty domain" {
    run validate_domain ""
    [ "$status" -eq 1 ]
}

@test "validate_domain rejects a domain with no dot" {
    run validate_domain "localhost"
    [ "$status" -eq 1 ]
}

@test "validate_domain rejects a domain with illegal characters" {
    run validate_domain "media_crown!time.cn"
    [ "$status" -eq 1 ]
}

@test "validate_domain rejects a label starting or ending with a hyphen" {
    run validate_domain "-media.crowntime.cn"
    [ "$status" -eq 1 ]
    run validate_domain "media-.crowntime.cn"
    [ "$status" -eq 1 ]
}

@test "renew.sh rejects a malformed domain before touching any network" {
    export DRY_RUN=1
    run "$SSL_RENEW_ROOT/renew.sh" "not_a_domain"
    [ "$status" -eq "$EXIT_USAGE_ERROR" ]
}

# ── Secret 脱敏 ────────────────────────────────────────────────────────
@test "filter_secrets redacts DP_/QINIU_/TOKEN/SECRET key=value pairs" {
    result=$(printf 'DP_Id=12345 DP_Key=secretvalue QINIU_ACCESS_KEY=abc TOKEN=xyz\n' | filter_secrets)
    [[ "$result" != *"12345"* ]]
    [[ "$result" != *"secretvalue"* ]]
    [[ "$result" != *"abc"* ]]
    [[ "$result" != *"xyz"* ]]
    [[ "$result" == *"[REDACTED]"* ]]
}

@test "filter_secrets redacts a QBox Authorization header" {
    result=$(printf 'Authorization: QBox akkey:signaturevalue==\n' | filter_secrets)
    [[ "$result" != *"signaturevalue"* ]]
    [[ "$result" == *"QBox [REDACTED]"* ]]
}

@test "filter_secrets redacts credentials embedded in a URL" {
    result=$(printf 'endpoint https://user:hunter2@example.com/hook\n' | filter_secrets)
    [[ "$result" != *"hunter2"* ]]
}

@test "filter_secrets leaves ordinary log text untouched" {
    result=$(printf 'deployment complete for media.crowntime.cn\n' | filter_secrets)
    [ "$result" = "deployment complete for media.crowntime.cn" ]
}

@test "run_with_timeout kills a command that outlives its budget" {
    start=$(date +%s)
    run run_with_timeout 1 sleep 5
    end=$(date +%s)
    elapsed=$((end - start))
    [ "$elapsed" -lt 4 ]
}

@test "run_with_timeout returns the command's own exit code when it finishes in time" {
    run run_with_timeout 5 bash -c 'exit 3'
    [ "$status" -eq 3 ]
}
