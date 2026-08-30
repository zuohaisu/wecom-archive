#!/usr/bin/env bats
# Tests for scripts/dr_pull.sh (GH-105).
#
# rsync/sha256sum run for real, but only ever against per-test mktemp
# fixture trees (never a real host/ssh endpoint, never production
# storage). "PULL_SOURCE" is always a local directory path in these
# tests — the same rsync invocation works identically against a remote
# user@host:/path/ spec in production.

SCRIPTS_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
DR_PULL_SCRIPT="$SCRIPTS_ROOT/dr_pull.sh"

setup() {
	TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/gh105-pull-test.XXXXXX")"
	SRC_DIR="$TEST_TMPDIR/src"
	DEST_DIR="$TEST_TMPDIR/dest"
	mkdir -p "$SRC_DIR" "$DEST_DIR"
}

teardown() {
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

# Seeds a valid artifact + matching checksum (+ optional manifest) in SRC_DIR.
seed_valid_artifact() {
	local name="$1" content="${2:-ciphertext-bytes}"
	printf '%s' "$content" >"$SRC_DIR/$name"
	(cd "$SRC_DIR" && sha256sum "$name") >"$SRC_DIR/$name.sha256"
}

run_pull() {
	PULL_SOURCE="$SRC_DIR/" PULL_DEST_DIR="$DEST_DIR" bash "$DR_PULL_SCRIPT"
}

@test "refuses to run without PULL_SOURCE" {
	unset PULL_SOURCE
	PULL_DEST_DIR="$DEST_DIR"
	run bash "$DR_PULL_SCRIPT"
	[ "$status" -eq 1 ]
	assert_output_contains "PULL_SOURCE is not set"
}

@test "refuses to run without PULL_DEST_DIR" {
	export PULL_SOURCE="$SRC_DIR/"
	unset PULL_DEST_DIR
	run bash "$DR_PULL_SCRIPT"
	[ "$status" -eq 1 ]
	assert_output_contains "PULL_DEST_DIR is not set"
}

@test "successful transfer: artifact verified and promoted into PULL_DEST_DIR" {
	seed_valid_artifact "bundle-a.tar.gz.gpg"
	run run_pull
	[ "$status" -eq 0 ]
	assert_output_contains "verified and promoted: bundle-a.tar.gz.gpg"
	[ -f "$DEST_DIR/bundle-a.tar.gz.gpg" ]
	[ -f "$DEST_DIR/bundle-a.tar.gz.gpg.sha256" ]
}

@test "manifest.json, when present alongside the artifact, is promoted too" {
	seed_valid_artifact "bundle-a.tar.gz.gpg"
	echo '{"bundle_version":1}' >"$SRC_DIR/bundle-a.manifest.json"
	run run_pull
	[ "$status" -eq 0 ]
	[ -f "$DEST_DIR/bundle-a.manifest.json" ]
}

@test "checksum mismatch: artifact NOT promoted, exits non-zero" {
	seed_valid_artifact "bundle-a.tar.gz.gpg" "original-content"
	# Corrupt the source bytes after the checksum was computed — simulates
	# a bad/tampered transfer.
	printf 'CORRUPTED' >"$SRC_DIR/bundle-a.tar.gz.gpg"

	run run_pull
	[ "$status" -eq 1 ]
	assert_output_contains "checksum verification FAILED for bundle-a.tar.gz.gpg"
	[ ! -f "$DEST_DIR/bundle-a.tar.gz.gpg" ]
}

@test "checksum mismatch never overwrites a previous known-good copy" {
	seed_valid_artifact "bundle-a.tar.gz.gpg" "good-content-v1"
	run run_pull
	[ "$status" -eq 0 ]
	good_sha="$(sha256sum "$DEST_DIR/bundle-a.tar.gz.gpg" | awk '{print $1}')"

	# New "transfer": checksum file still references the old good content,
	# but the actual bytes on the wire are corrupted.
	printf 'CORRUPTED-ON-THE-WIRE' >"$SRC_DIR/bundle-a.tar.gz.gpg"

	run run_pull
	[ "$status" -eq 1 ]
	current_sha="$(sha256sum "$DEST_DIR/bundle-a.tar.gz.gpg" | awk '{print $1}')"
	[ "$good_sha" = "$current_sha" ]
}

@test "missing .sha256 companion: artifact NOT promoted (cannot verify), exits non-zero" {
	printf 'ciphertext-bytes' >"$SRC_DIR/bundle-a.tar.gz.gpg"
	# No .sha256 file written.
	run run_pull
	[ "$status" -eq 1 ]
	assert_output_contains "checksum file missing for bundle-a.tar.gz.gpg"
	[ ! -f "$DEST_DIR/bundle-a.tar.gz.gpg" ]
}

@test "producer's .tmp staging directory is never synced or promoted" {
	seed_valid_artifact "bundle-a.tar.gz.gpg"
	mkdir -p "$SRC_DIR/.tmp"
	printf 'plaintext-mid-encryption' >"$SRC_DIR/.tmp/dr-config-bundle.XXXXXX"

	run run_pull
	[ "$status" -eq 0 ]
	[ ! -d "$DEST_DIR/.tmp" ]
	[ -z "$(find "$DEST_DIR" -name '*XXXXXX*' 2>/dev/null)" ]
}

@test "idempotent rerun: already-verified artifact is skipped, not re-promoted as an error" {
	seed_valid_artifact "bundle-a.tar.gz.gpg"
	run run_pull
	[ "$status" -eq 0 ]

	run run_pull
	[ "$status" -eq 0 ]
	assert_output_contains "already verified and present: bundle-a.tar.gz.gpg"
	assert_output_contains "promoted=0 skipped=1 failed=0"
}

@test "multiple artifacts: one good, one corrupted — good one still promoted" {
	seed_valid_artifact "bundle-good.tar.gz.gpg" "good-bytes"
	seed_valid_artifact "bundle-bad.tar.gz.gpg" "original-bad-bytes"
	printf 'CORRUPTED' >"$SRC_DIR/bundle-bad.tar.gz.gpg"

	run run_pull
	[ "$status" -eq 1 ]
	[ -f "$DEST_DIR/bundle-good.tar.gz.gpg" ]
	[ ! -f "$DEST_DIR/bundle-bad.tar.gz.gpg" ]
	assert_output_contains "verified and promoted: bundle-good.tar.gz.gpg"
	assert_output_contains "checksum verification FAILED for bundle-bad.tar.gz.gpg"
}

@test "rsync failure (unreachable source): exits non-zero, nothing promoted" {
	run bash -c "PULL_SOURCE='$TEST_TMPDIR/does-not-exist/' PULL_DEST_DIR='$DEST_DIR' bash '$DR_PULL_SCRIPT'"
	[ "$status" -eq 1 ]
	[ -z "$(find "$DEST_DIR" -maxdepth 1 -name '*.gpg' 2>/dev/null)" ]
}
