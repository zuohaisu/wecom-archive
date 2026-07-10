#!/usr/bin/env bats
# Structural checks on the shipped systemd unit files (best-effort static
# check on macOS; the authoritative check is `systemd-analyze verify`,
# run via ssl-renew/Dockerfile or the Makefile ssl-verify-systemd target).

load test_helper/common

setup() {
    common_setup
    CHECKER="$SSL_RENEW_ROOT/tests/systemd_static_check.sh"
    SERVICE="$SSL_RENEW_ROOT/../deploy/systemd/qiniu-ssl-renew@.service"
    TIMER="$SSL_RENEW_ROOT/../deploy/systemd/qiniu-ssl-renew@.timer"
}
teardown() { common_teardown; }

@test "shipped .service and .timer pass the static structural check" {
    run "$CHECKER" "$SERVICE" "$TIMER"
    [ "$status" -eq 0 ]
}

@test "qiniu-ssl-renew@.service sets a TimeoutStartSec well above the TLS verify worst case" {
    run grep -E '^TimeoutStartSec=([0-9]+)' "$SERVICE"
    [ "$status" -eq 0 ]
    value=$(echo "$output" | sed -E 's/TimeoutStartSec=([0-9]+).*/\1/')
    # worst case: acme.sh DNS propagation + verify_https.sh backoff
    # (5 retries at up to a few seconds each) comfortably exceeds the old
    # implicit 90s default; require a generous margin.
    [ "$value" -ge 300 ]
}

@test "qiniu-ssl-renew@.service does not run as root" {
    run grep -E '^User=' "$SERVICE"
    [ "$status" -eq 0 ]
    [[ "$output" != *"User=root"* ]]
}

@test "qiniu-ssl-renew@.service sets UMask, Group, and RestrictAddressFamilies" {
    grep -q '^UMask=' "$SERVICE"
    grep -q '^Group=' "$SERVICE"
    grep -q '^RestrictAddressFamilies=' "$SERVICE"
}

@test "qiniu-ssl-renew@.service EnvironmentFile is per-instance (uses %i)" {
    run grep '^EnvironmentFile=' "$SERVICE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"%i"* ]]
}

@test "systemd_static_check.sh fails on a unit file missing a required directive" {
    bad="$TEST_TMPDIR/bad.service"
    cat >"$bad" <<'EOF'
[Unit]
Description=broken

[Service]
Type=oneshot
EOF
    run "$CHECKER" "$bad"
    [ "$status" -eq 1 ]
    [[ "$output" == *"ExecStart"* ]]
}

@test "systemd_static_check.sh rejects a malformed (non KEY=VALUE) line" {
    bad="$TEST_TMPDIR/bad2.service"
    cat >"$bad" <<'EOF'
[Unit]
Description=broken

[Service]
Type=oneshot
ExecStart=/bin/true
TimeoutStartSec=600
User=nobody
this is not a valid directive line
EOF
    run "$CHECKER" "$bad"
    [ "$status" -eq 1 ]
    [[ "$output" == *"malformed line"* ]]
}
