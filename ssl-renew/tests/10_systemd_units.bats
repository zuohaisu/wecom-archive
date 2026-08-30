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
    WILDCARD_SERVICE="$SSL_RENEW_ROOT/../deploy/systemd/qiniu-ssl-renew-wildcard.service"
    WILDCARD_TIMER="$SSL_RENEW_ROOT/../deploy/systemd/qiniu-ssl-renew-wildcard.timer"
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


# ── GH-104 Follow-up B: captured wildcard *.crowntime.cn units ──────────
# These lock the production-proven cadence/relationship this capture must
# never silently drift from (see docs/operations/wildcard-ssl-renewal.md).

@test "shipped wildcard .service and .timer pass the static structural check" {
    run "$CHECKER" "$WILDCARD_SERVICE" "$WILDCARD_TIMER"
    [ "$status" -eq 0 ]
}

@test "qiniu-ssl-renew-wildcard.service points ExecStart at the repo-managed wildcard script" {
    run grep '^ExecStart=' "$WILDCARD_SERVICE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"/ssl-renew/renew-wildcard.sh"* ]]
    [ -f "$SSL_RENEW_ROOT/renew-wildcard.sh" ]
    [ -x "$SSL_RENEW_ROOT/renew-wildcard.sh" ]
}

@test "qiniu-ssl-renew-wildcard.service runs as wecomarchive:wecomarchive, not root" {
    grep -q '^User=wecomarchive$' "$WILDCARD_SERVICE"
    grep -q '^Group=wecomarchive$' "$WILDCARD_SERVICE"
}

@test "qiniu-ssl-renew-wildcard.service EnvironmentFile is the wildcard config, not the per-domain template's %i" {
    run grep '^EnvironmentFile=' "$WILDCARD_SERVICE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"/etc/qiniu-ssl-renew/media.crowntime.cn.env"* ]]
    [[ "$output" != *"%i"* ]]
}

@test "qiniu-ssl-renew-wildcard.service sets a TimeoutStartSec well above the default 90s" {
    run grep -E '^TimeoutStartSec=([0-9]+)' "$WILDCARD_SERVICE"
    [ "$status" -eq 0 ]
    value=$(echo "$output" | sed -E 's/TimeoutStartSec=([0-9]+).*/\1/')
    [ "$value" -ge 300 ]
}

@test "qiniu-ssl-renew-wildcard.service does NOT add hardening beyond the captured production unit" {
    # GH-104 Follow-up B is explicit: do not add NoNewPrivileges=,
    # ProtectSystem=, or CapabilityBoundingSet= -- the production unit
    # this was captured from does not have them. Adding them here would
    # silently diverge repo/server equivalence.
    ! grep -qE '^(NoNewPrivileges|ProtectSystem|CapabilityBoundingSet)=' "$WILDCARD_SERVICE"
}

@test "qiniu-ssl-renew-wildcard.timer locks the captured production cadence" {
    grep -q '^OnCalendar=\*-\*-\* 00:15:00$' "$WILDCARD_TIMER"
    grep -q '^Persistent=true$' "$WILDCARD_TIMER"
    grep -q '^RandomizedDelaySec=900$' "$WILDCARD_TIMER"
    grep -q '^Unit=qiniu-ssl-renew-wildcard.service$' "$WILDCARD_TIMER"
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
