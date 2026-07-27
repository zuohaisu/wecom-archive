#!/usr/bin/env bash
#=============================================================================
# Shared bats test helpers for disk_usage_check.bats and backup_once.bats
# (RND-193). Mirrors deploy_server.bats's approach (see
# test_helper/common.bash): every external command the scripts under test
# shell out to (df, psql, pg_dump, gpg, notify.sh) is a mock script wired in
# via the scripts' own *_BIN override variables — never a real database,
# real GPG keyring, or real webhook endpoint. tar is used for real (a local,
# side-effect-free archive operation with no external system boundary),
# operating only inside a per-test mktemp fixture tree.
#=============================================================================

SCRIPTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISK_USAGE_SCRIPT="$SCRIPTS_ROOT/disk_usage_check.sh"
BACKUP_SCRIPT="$SCRIPTS_ROOT/backup_once.sh"

rnd193_common_setup() {
	TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/rnd193-test.XXXXXX")"
	export TEST_TMPDIR

	MOCK_BIN_DIR="$TEST_TMPDIR/mockbin"
	mkdir -p "$MOCK_BIN_DIR"
	cp "$TEST_HELPER_DIR/mock_df.sh" "$MOCK_BIN_DIR/mock_df"
	cp "$TEST_HELPER_DIR/mock_psql.sh" "$MOCK_BIN_DIR/mock_psql"
	cp "$TEST_HELPER_DIR/mock_pg_dump.sh" "$MOCK_BIN_DIR/mock_pg_dump"
	cp "$TEST_HELPER_DIR/mock_gpg.sh" "$MOCK_BIN_DIR/mock_gpg"
	cp "$TEST_HELPER_DIR/mock_notify.sh" "$MOCK_BIN_DIR/mock_notify"
	chmod +x "$MOCK_BIN_DIR"/*

	MOCK_LOG="$TEST_TMPDIR/mock_calls.log"
	: >"$MOCK_LOG"
	export MOCK_DF_LOG="$MOCK_LOG"
	export MOCK_PSQL_LOG="$MOCK_LOG"
	export MOCK_PG_DUMP_LOG="$MOCK_LOG"
	export MOCK_GPG_LOG="$MOCK_LOG"
	export MOCK_NOTIFY_LOG="$MOCK_LOG"

	# Defaults: everything healthy/successful unless a test overrides it.
	export MOCK_DF_DISK_PCT=31
	export MOCK_DF_INODE_PCT=4
	export MOCK_PSQL_MODE=ok
	export MOCK_PSQL_CONNS=7
	export MOCK_PSQL_MAX_CONN=100
	export MOCK_PG_DUMP_MODE=ok
	export MOCK_GPG_MODE=ok

	MEMINFO_OK="$TEST_TMPDIR/meminfo_ok"
	cat >"$MEMINFO_OK" <<'EOF'
MemTotal:        1900000 kB
MemAvailable:    1300000 kB
SwapTotal:       4000000 kB
SwapFree:        3900000 kB
EOF
	export MEMINFO_OK
}

rnd193_common_teardown() {
	[ -n "${TEST_TMPDIR:-}" ] && rm -rf "$TEST_TMPDIR"
}

assert_output_contains() {
	if [[ "$output" != *"$1"* ]]; then
		echo "expected output to contain: $1" >&2
		echo "--- actual output ---" >&2
		echo "$output" >&2
		return 1
	fi
}

assert_output_not_contains() {
	if [[ "$output" == *"$1"* ]]; then
		echo "expected output to NOT contain: $1" >&2
		echo "--- actual output ---" >&2
		echo "$output" >&2
		return 1
	fi
}
