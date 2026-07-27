#!/usr/bin/env bats
# Tests for scripts/backup_once.sh (RND-193).
#
# pg_dump/gpg/notify.sh are all mocks (test_helper/mock_pg_dump.sh,
# mock_gpg.sh, mock_notify.sh) wired in via the script's own *_BIN override
# variables — never a real database, real GPG keyring, or real webhook.
# tar runs for real, but only ever inside a per-test mktemp fixture tree,
# never touching production media.

load test_helper/rnd193_common

setup() {
	rnd193_common_setup
	BACKUP_DIR="$TEST_TMPDIR/backups"
	MEDIA_DIR="$TEST_TMPDIR/media"
	mkdir -p "$BACKUP_DIR" "$MEDIA_DIR"
	echo "fake photo bytes" >"$MEDIA_DIR/a.jpg"
	export BACKUP_DIR MEDIA_DIR
	export DATABASE_URL="postgresql://user:supersecretpw@localhost/db"
	export BACKUP_GPG_PASSPHRASE="testpass123"
}

run_backup() {
	PG_DUMP_BIN="$MOCK_BIN_DIR/mock_pg_dump" \
		GPG_BIN="$MOCK_BIN_DIR/mock_gpg" \
		NOTIFY_BIN="$MOCK_BIN_DIR/mock_notify" \
		bash "$BACKUP_SCRIPT"
}

@test "refuses to run without DATABASE_URL" {
	unset DATABASE_URL
	run run_backup
	[ "$status" -eq 1 ]
	assert_output_contains "DATABASE_URL is not set"
	[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name 'wecom_archive-*' 2>/dev/null)" ]
}

@test "refuses to run without BACKUP_GPG_PASSPHRASE (never writes an unencrypted backup)" {
	unset BACKUP_GPG_PASSPHRASE
	run run_backup
	[ "$status" -eq 1 ]
	assert_output_contains "BACKUP_GPG_PASSPHRASE is not set"
	[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name 'wecom_archive-*' 2>/dev/null)" ]
}

@test "pg_dump failure: no backup file written, alert sent, exit 1" {
	export MOCK_PG_DUMP_MODE=fail
	export STORAGE_LOCAL_PATH="$MEDIA_DIR"
	run run_backup
	[ "$status" -eq 1 ]
	assert_output_contains "pg_dump exited non-zero"
	[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name 'wecom_archive-*' 2>/dev/null)" ]
	grep -q "NOTIFY ERROR|backup-once|pg_dump" "$MOCK_LOG"
	grep -q "|pg_dump$" "$MOCK_LOG" # CURRENT_STAGE carries the failed stage name, not just "ERROR"
}

@test "gpg encryption failure on DB dump: no .gpg left behind, exit 1" {
	export MOCK_GPG_MODE=fail
	run run_backup
	[ "$status" -eq 1 ]
	assert_output_contains "encryption of the DB dump failed"
	[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name 'wecom_archive-*' 2>/dev/null)" ]
}

@test "successful run with media: both artifacts encrypted, decryptable, retention logged" {
	export STORAGE_LOCAL_PATH="$MEDIA_DIR"
	run run_backup
	[ "$status" -eq 0 ]
	assert_output_contains "[OK] DB backup encrypted"
	assert_output_contains "[OK] media backup encrypted"
	assert_output_contains "backup_once complete (db=1 media=1)"

	db_file="$(ls "$BACKUP_DIR"/wecom_archive-db-*.dump.gpg)"
	[ -f "$db_file" ]
	grep -q "MOCKGPG-ENCRYPTED" "$db_file"
	grep -q "FAKE PG DUMP CONTENT" "$db_file"

	media_file="$(ls "$BACKUP_DIR"/wecom_archive-media-*.tar.gz.gpg)"
	[ -f "$media_file" ]
	grep -q "MOCKGPG-ENCRYPTED" "$media_file"
}

@test "no plaintext dump or archive is ever left in BACKUP_DIR" {
	export STORAGE_LOCAL_PATH="$MEDIA_DIR"
	run run_backup
	[ "$status" -eq 0 ]
	# shellcheck disable=SC2012
	unencrypted="$(ls "$BACKUP_DIR" | grep -v '\.gpg$' || true)"
	[ -z "$unencrypted" ]
}

@test "media backup is skipped explicitly when STORAGE_LOCAL_PATH is unset" {
	unset STORAGE_LOCAL_PATH
	run run_backup
	[ "$status" -eq 0 ]
	assert_output_contains "[SKIP] media backup: STORAGE_LOCAL_PATH not set"
	assert_output_contains "backup_once complete (db=1 media=0)"
	[ -z "$(ls "$BACKUP_DIR"/wecom_archive-media-* 2>/dev/null)" ]
}

@test "media backup is skipped explicitly when the directory is empty" {
	empty_dir="$TEST_TMPDIR/empty_media"
	mkdir -p "$empty_dir"
	export STORAGE_LOCAL_PATH="$empty_dir"
	run run_backup
	[ "$status" -eq 0 ]
	assert_output_contains "has no files"
}

@test "retention deletes only this script's backups older than the threshold, after a successful run" {
	export STORAGE_LOCAL_PATH="$MEDIA_DIR"
	export BACKUP_RETENTION_DAYS=7

	old_db="$BACKUP_DIR/wecom_archive-db-20260101T000000Z.dump.gpg"
	old_media="$BACKUP_DIR/wecom_archive-media-20260101T000000Z.tar.gz.gpg"
	unrelated="$BACKUP_DIR/pre-rnd-111-2026-06-28-234547.sql"
	echo old >"$old_db"
	echo old >"$old_media"
	echo unrelated >"$unrelated"
	# Backdate well past the retention window.
	touch -t 202601010000 "$old_db" "$old_media"

	run run_backup
	[ "$status" -eq 0 ]
	assert_output_contains "retention: removed 2 backup file(s)"
	[ ! -f "$old_db" ]
	[ ! -f "$old_media" ]
	[ -f "$unrelated" ] # never touches files outside its own naming pattern
}

@test "a failed run never triggers retention deletion" {
	export MOCK_PG_DUMP_MODE=fail
	old_db="$BACKUP_DIR/wecom_archive-db-20260101T000000Z.dump.gpg"
	echo old >"$old_db"
	touch -t 202601010000 "$old_db"

	run run_backup
	[ "$status" -eq 1 ]
	[ -f "$old_db" ]
	assert_output_not_contains "retention:"
}

@test "DB password never appears in output" {
	export STORAGE_LOCAL_PATH="$MEDIA_DIR"
	run run_backup
	[ "$status" -eq 0 ]
	assert_output_not_contains "supersecretpw"
}

@test "GPG passphrase is piped via stdin, never passed as a CLI argument" {
	export STORAGE_LOCAL_PATH="$MEDIA_DIR"
	run run_backup
	[ "$status" -eq 0 ]
	assert_output_not_contains "testpass123"
	! grep -q "testpass123" "$MOCK_LOG"
}
