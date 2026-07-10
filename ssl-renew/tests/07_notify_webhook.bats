#!/usr/bin/env bats
# notify.sh: generic webhook alerting against a local mock HTTP server.
# Covers success, non-2xx, timeout, connection failure, and log-only degrade.

load test_helper/common

setup() {
    common_setup
    NOTIFY="$SSL_RENEW_ROOT/notify.sh"
}
teardown() { common_teardown; }

# ── 告警触发 ───────────────────────────────────────────────────────────
@test "notify.sh always logs the alert locally regardless of webhook state" {
    run "$NOTIFY" ERROR media.crowntime.cn "certID mismatch"
    [[ "$output" == *"[NOTIFY] [ERROR] [media.crowntime.cn] certID mismatch"* ]]
}

@test "notify.sh POSTs a JSON payload with domain/hostname/failed_stage/timestamp/error_summary" {
    port=$(find_free_port)
    start_mock_server "$port" --status 200 --log-file "$TEST_TMPDIR/req.json" >/dev/null
    export ALERT_WEBHOOK_URL="http://127.0.0.1:$port/hook"
    export ALERT_WEBHOOK_TIMEOUT=5
    export CURRENT_STAGE="qiniu_upload"
    run "$NOTIFY" ERROR media.crowntime.cn "upload failed"
    [ "$status" -eq 0 ]
    [ -f "$TEST_TMPDIR/req.json" ]
    payload=$(cat "$TEST_TMPDIR/req.json")
    [[ "$payload" == *'"domain": "media.crowntime.cn"'* ]]
    [[ "$payload" == *'"hostname":'* ]]
    [[ "$payload" == *'"failed_stage": "qiniu_upload"'* ]]
    [[ "$payload" == *'"timestamp":'* ]]
    [[ "$payload" == *'"error_summary": "upload failed"'* ]]
}

@test "notify.sh reports webhook success" {
    port=$(find_free_port)
    start_mock_server "$port" --status 200 >/dev/null
    export ALERT_WEBHOOK_URL="http://127.0.0.1:$port/hook" ALERT_WEBHOOK_TIMEOUT=5
    run "$NOTIFY" WARN media.crowntime.cn "propagation delay"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[OK] webhook delivered (HTTP 200)"* ]]
}

# ── 告警 Webhook 非 2xx ────────────────────────────────────────────────
@test "notify.sh handles a non-2xx webhook response without changing its own exit code" {
    port=$(find_free_port)
    start_mock_server "$port" --status 500 >/dev/null
    export ALERT_WEBHOOK_URL="http://127.0.0.1:$port/hook" ALERT_WEBHOOK_TIMEOUT=5
    run "$NOTIFY" WARN media.crowntime.cn "propagation delay"
    [ "$status" -eq 0 ]
    [[ "$output" == *"non-2xx: HTTP 500"* ]]
}

# ── 告警超时 ───────────────────────────────────────────────────────────
@test "notify.sh handles a webhook timeout without changing its own exit code" {
    port=$(find_free_port)
    start_mock_server "$port" --status 200 --delay 5 >/dev/null
    export ALERT_WEBHOOK_URL="http://127.0.0.1:$port/hook" ALERT_WEBHOOK_TIMEOUT=1
    start=$(date +%s)
    run "$NOTIFY" WARN media.crowntime.cn "slow endpoint"
    end=$(date +%s)
    [ "$status" -eq 0 ]
    [[ "$output" == *"timed out"* ]]
    elapsed=$((end - start))
    [ "$elapsed" -lt 4 ]
}

@test "notify.sh handles a webhook connection failure without changing its own exit code" {
    port=$(find_free_port)
    export ALERT_WEBHOOK_URL="http://127.0.0.1:$port/hook" ALERT_WEBHOOK_TIMEOUT=3
    run "$NOTIFY" WARN media.crowntime.cn "unreachable webhook"
    [ "$status" -eq 0 ]
    [[ "$output" == *"connection error"* || "$output" == *"failed"* ]]
}

# ── Webhook 未配置 → 明确降级 ───────────────────────────────────────────
@test "notify.sh degrades to a clearly-labeled log-only mode when ALERT_WEBHOOK_URL is unset" {
    unset ALERT_WEBHOOK_URL
    run "$NOTIFY" INFO media.crowntime.cn "no webhook here"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[DEGRADED]"* ]]
    [[ "$output" == *"not configured"* ]]
}

# ── Secret 脱敏 (notify.sh 特有) ────────────────────────────────────────
@test "notify.sh never logs or forwards secret material in the message" {
    port=$(find_free_port)
    start_mock_server "$port" --status 200 --log-file "$TEST_TMPDIR/req.json" >/dev/null
    export ALERT_WEBHOOK_URL="http://127.0.0.1:$port/hook" ALERT_WEBHOOK_TIMEOUT=5
    run "$NOTIFY" ERROR media.crowntime.cn "upload failed QINIU_SECRET_KEY=leaked-secret-xyz"
    [[ "$output" != *"leaked-secret-xyz"* ]]
    payload=$(cat "$TEST_TMPDIR/req.json")
    [[ "$payload" != *"leaked-secret-xyz"* ]]
}

@test "notify.sh does not print ALERT_WEBHOOK_URL itself in any log line" {
    export ALERT_WEBHOOK_URL="http://127.0.0.1:1/super-secret-path-token-abc"
    run "$NOTIFY" WARN media.crowntime.cn "test"
    [[ "$output" != *"super-secret-path-token-abc"* ]]
}
