#!/usr/bin/env bats
# Tests for scripts/disk_usage_check.sh (RND-193).
#
# df/psql/notify.sh are all mocks (test_helper/mock_df.sh, mock_psql.sh,
# mock_notify.sh) wired in via the script's own *_BIN override variables —
# never a real disk threshold, real database, or real webhook.

load test_helper/rnd193_common

setup() { rnd193_common_setup; }
teardown() { rnd193_common_teardown; }

run_check() {
	DF_BIN="$MOCK_BIN_DIR/mock_df" \
		PSQL_BIN="$MOCK_BIN_DIR/mock_psql" \
		NOTIFY_BIN="$MOCK_BIN_DIR/mock_notify" \
		MEMINFO_FILE="$MEMINFO_OK" \
		bash "$DISK_USAGE_SCRIPT"
}

@test "all checks healthy: exits 0, no alert sent" {
	unset DATABASE_URL
	run run_check
	[ "$status" -eq 0 ]
	assert_output_contains "[OK] disk"
	assert_output_contains "[OK] inode"
	assert_output_contains "[OK] memory"
	assert_output_contains "[OK] swap"
	assert_output_contains "[SKIP] pg_connections: DATABASE_URL not set"
	! grep -q "^NOTIFY" "$MOCK_LOG"
}

@test "disk breach triggers WARN, alert, and exit 1" {
	unset DATABASE_URL
	export MOCK_DF_DISK_PCT=95
	run run_check
	[ "$status" -eq 1 ]
	assert_output_contains "[WARN] disk: 95%"
	grep -q "NOTIFY WARN|disk-usage-check|" "$MOCK_LOG"
	grep -q "disk 95%" "$MOCK_LOG"
	grep -q "|disk$" "$MOCK_LOG" # CURRENT_STAGE carries the breached check name, not just "WARN"
}

@test "inode breach triggers WARN and exit 1" {
	unset DATABASE_URL
	export MOCK_DF_INODE_PCT=99
	run run_check
	[ "$status" -eq 1 ]
	assert_output_contains "[WARN] inode: 99%"
}

@test "memory pressure breach triggers WARN and exit 1" {
	unset DATABASE_URL
	cat >"$TEST_TMPDIR/meminfo_mem_warn" <<'EOF'
MemTotal:        1900000 kB
MemAvailable:      50000 kB
SwapTotal:       4000000 kB
SwapFree:        3900000 kB
EOF
	MEMINFO_FILE="$TEST_TMPDIR/meminfo_mem_warn" \
		DF_BIN="$MOCK_BIN_DIR/mock_df" NOTIFY_BIN="$MOCK_BIN_DIR/mock_notify" \
		run bash "$DISK_USAGE_SCRIPT"
	[ "$status" -eq 1 ]
	assert_output_contains "[WARN] memory:"
}

@test "swap breach triggers WARN and exit 1" {
	unset DATABASE_URL
	cat >"$TEST_TMPDIR/meminfo_swap_warn" <<'EOF'
MemTotal:        1900000 kB
MemAvailable:    1300000 kB
SwapTotal:       4000000 kB
SwapFree:         100000 kB
EOF
	MEMINFO_FILE="$TEST_TMPDIR/meminfo_swap_warn" \
		DF_BIN="$MOCK_BIN_DIR/mock_df" NOTIFY_BIN="$MOCK_BIN_DIR/mock_notify" \
		run bash "$DISK_USAGE_SCRIPT"
	[ "$status" -eq 1 ]
	assert_output_contains "[WARN] swap:"
}

@test "PostgreSQL connection breach triggers WARN and exit 1" {
	export DATABASE_URL="postgresql://user:pw@localhost/db"
	export MOCK_PSQL_CONNS=95
	export MOCK_PSQL_MAX_CONN=100
	run run_check
	[ "$status" -eq 1 ]
	assert_output_contains "[WARN] pg_connections: 95%"
}

@test "PostgreSQL unreachable reports DEGRADED, not WARN, and does not itself force exit 1" {
	export DATABASE_URL="postgresql://user:pw@localhost/db"
	export MOCK_PSQL_MODE=unreachable
	run run_check
	[ "$status" -eq 0 ]
	assert_output_contains "[DEGRADED] pg_connections:"
	assert_output_not_contains "[WARN] pg_connections"
}

@test "healthy PostgreSQL connection count passes" {
	export DATABASE_URL="postgresql://user:pw@localhost/db"
	export MOCK_PSQL_CONNS=7
	export MOCK_PSQL_MAX_CONN=100
	run run_check
	[ "$status" -eq 0 ]
	assert_output_contains "[OK] pg_connections: 7%"
}

@test "thresholds are env-overridable" {
	unset DATABASE_URL
	export DISK_WARN_PCT=20
	run run_check
	[ "$status" -eq 1 ]
	assert_output_contains "[WARN] disk: 31% >= 20% threshold"
}

@test "missing notify.sh degrades to log-only without crashing" {
	unset DATABASE_URL
	export MOCK_DF_DISK_PCT=95
	DF_BIN="$MOCK_BIN_DIR/mock_df" \
		NOTIFY_BIN="$TEST_TMPDIR/does-not-exist.sh" \
		MEMINFO_FILE="$MEMINFO_OK" \
		run bash "$DISK_USAGE_SCRIPT"
	[ "$status" -eq 1 ]
	assert_output_contains "notify.sh not found/executable"
}
