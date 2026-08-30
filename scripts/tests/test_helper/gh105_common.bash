#!/usr/bin/env bash
#=============================================================================
# Shared bats test helpers for dr_config_bundle.bats (GH-105). Mirrors
# rnd193_common.bash's approach: gpg/notify.sh are mocks wired in via the
# script's own *_BIN override variables — never a real GPG keyring or real
# webhook endpoint. tar/cp/sha256sum run for real (local, side-effect-free
# operations with no external system boundary), operating only inside a
# per-test mktemp fixture tree, never touching production paths.
#=============================================================================

SCRIPTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DR_CONFIG_BUNDLE_SCRIPT="$SCRIPTS_ROOT/dr_config_bundle.sh"

gh105_common_setup() {
	TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/gh105-test.XXXXXX")"
	export TEST_TMPDIR

	MOCK_BIN_DIR="$TEST_TMPDIR/mockbin"
	mkdir -p "$MOCK_BIN_DIR"
	cp "$TEST_HELPER_DIR/mock_gpg.sh" "$MOCK_BIN_DIR/mock_gpg"
	cp "$TEST_HELPER_DIR/mock_notify.sh" "$MOCK_BIN_DIR/mock_notify"
	chmod +x "$MOCK_BIN_DIR"/*

	MOCK_LOG="$TEST_TMPDIR/mock_calls.log"
	: >"$MOCK_LOG"
	export MOCK_GPG_LOG="$MOCK_LOG"
	export MOCK_NOTIFY_LOG="$MOCK_LOG"
	export MOCK_GPG_MODE=ok

	# Fake production tree: APP_ROOT/current/backend/.env + APP_ROOT/shared/*
	APP_ROOT="$TEST_TMPDIR/app"
	mkdir -p "$APP_ROOT/current/backend" "$APP_ROOT/shared"
	export APP_ROOT
}

gh105_common_teardown() {
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
