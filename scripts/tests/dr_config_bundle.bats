#!/usr/bin/env bats
# Tests for scripts/dr_config_bundle.sh (GH-105).
#
# gpg/notify.sh are mocks (test_helper/mock_gpg.sh, mock_notify.sh) wired
# in via the script's own *_BIN override variables — never a real GPG
# keyring or real webhook. tar/cp/sha256sum run for real, but only ever
# inside a per-test mktemp fixture tree (APP_ROOT), never a production
# path. Every "secret" used below is a fake fixture value — see AGENTS.md
# "Never commit" — none of it is a real credential.

load test_helper/gh105_common

setup() {
	gh105_common_setup
	export RECOVERY_PASSPHRASE="fake-recovery-pass-9f3c"
}

teardown() {
	gh105_common_teardown
}

seed_required() {
	echo "FIELD_ENCRYPTION_KEY=fake-fernet-key-do-not-use" >"$APP_ROOT/current/backend/.env"
	mkdir -p "$APP_ROOT/shared/keys"
	echo "-----BEGIN FAKE RSA PRIVATE KEY-----fake-----END FAKE RSA PRIVATE KEY-----" \
		>"$APP_ROOT/shared/keys/private_key_v1.pem"
}

run_bundle() {
	GPG_BIN="$MOCK_BIN_DIR/mock_gpg" \
		NOTIFY_BIN="$MOCK_BIN_DIR/mock_notify" \
		bash "$DR_CONFIG_BUNDLE_SCRIPT"
}

@test "refuses to run without RECOVERY_PASSPHRASE" {
	unset RECOVERY_PASSPHRASE
	seed_required
	run run_bundle
	[ "$status" -eq 1 ]
	assert_output_contains "RECOVERY_PASSPHRASE is not set"
	[ ! -d "$APP_ROOT/shared/dr_bundles" ] || [ -z "$(find "$APP_ROOT/shared/dr_bundles" -maxdepth 1 -name '*.gpg' 2>/dev/null)" ]
}

@test "required files present: SUCCESS, output is .gpg, checksum+manifest written" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	assert_output_contains "status=completed"

	artifact="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.tar.gz.gpg)"
	[ -f "$artifact" ]
	grep -q "MOCKGPG-ENCRYPTED" "$artifact"

	checksum_file="${artifact}.sha256"
	[ -f "$checksum_file" ]
	manifest_file="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.manifest.json)"
	[ -f "$manifest_file" ]
}

@test "required file missing (backend/.env absent): FAIL, no bundle written" {
	mkdir -p "$APP_ROOT/shared/keys"
	echo "fake-key" >"$APP_ROOT/shared/keys/private_key_v1.pem"
	# backend/.env intentionally not created
	run run_bundle
	[ "$status" -eq 1 ]
	assert_output_contains "required recovery artifact missing: backend/.env"
	[ -z "$(find "$APP_ROOT/shared/dr_bundles" -maxdepth 1 -name '*.gpg' 2>/dev/null)" ]
}

@test "required directory missing (shared/keys absent): FAIL, no bundle written" {
	echo "FIELD_ENCRYPTION_KEY=fake" >"$APP_ROOT/current/backend/.env"
	# shared/keys intentionally not created
	run run_bundle
	[ "$status" -eq 1 ]
	assert_output_contains "required recovery artifact missing: shared/keys"
	[ -z "$(find "$APP_ROOT/shared/dr_bundles" -maxdepth 1 -name '*.gpg' 2>/dev/null)" ]
}

@test "required directory present but empty: treated as missing, FAIL" {
	echo "FIELD_ENCRYPTION_KEY=fake" >"$APP_ROOT/current/backend/.env"
	mkdir -p "$APP_ROOT/shared/keys" # empty — no files inside
	run run_bundle
	[ "$status" -eq 1 ]
	assert_output_contains "required recovery artifact missing: shared/keys"
}

@test "optional artifacts missing: allowed, bundle still succeeds" {
	seed_required
	# shared/private_keys, shared/backup.env, shared/certs, shared/deploy_state
	# all intentionally absent.
	run run_bundle
	[ "$status" -eq 0 ]
	assert_output_contains "[SKIP] optional recovery artifact not present: shared/private_keys"
	assert_output_contains "[SKIP] optional recovery artifact not present: shared/backup.env"
	assert_output_contains "[SKIP] optional recovery artifact not present: shared/certs"
	assert_output_contains "[SKIP] optional recovery artifact not present: shared/deploy_state"
}

@test "optional artifacts present: included in the bundle and manifest" {
	seed_required
	echo "fake-legacy-key" >"$APP_ROOT/shared/private_keys_seed" # sanity: not the real dir name
	mkdir -p "$APP_ROOT/shared/private_keys" "$APP_ROOT/shared/certs" "$APP_ROOT/shared/deploy_state"
	echo "fake-legacy-key" >"$APP_ROOT/shared/private_keys/legacy.pem"
	echo "BACKUP_GPG_PASSPHRASE=fake-backup-pass" >"$APP_ROOT/shared/backup.env"
	echo "fake-cert-bytes" >"$APP_ROOT/shared/certs/fullchain.pem"
	echo "deadbeef" >"$APP_ROOT/shared/deploy_state/last_known_good_sha"

	run run_bundle
	[ "$status" -eq 0 ]
	assert_output_contains "[OK] collected: shared/private_keys"
	assert_output_contains "[OK] collected: shared/backup.env"
	assert_output_contains "[OK] collected: shared/certs"
	assert_output_contains "[OK] collected: shared/deploy_state"

	manifest_file="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.manifest.json)"
	grep -q '"path":"shared/private_keys","required":false,"included":true' "$manifest_file"
	grep -q '"path":"shared/backup.env","required":false,"included":true' "$manifest_file"
	grep -q '"path":"shared/certs","required":false,"included":true' "$manifest_file"
	grep -q '"path":"shared/deploy_state","required":false,"included":true' "$manifest_file"
}

@test "no plaintext staging directory is left behind after a successful run" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	staging_leftovers="$(find "$APP_ROOT/shared/dr_bundles/.tmp" -mindepth 1 2>/dev/null)"
	[ -z "$staging_leftovers" ]
}

@test "no plaintext staging directory is left behind after a failed run" {
	echo "FIELD_ENCRYPTION_KEY=fake" >"$APP_ROOT/current/backend/.env"
	# shared/keys missing -> fails closed
	run run_bundle
	[ "$status" -eq 1 ]
	staging_leftovers="$(find "$APP_ROOT/shared/dr_bundles/.tmp" -mindepth 1 2>/dev/null)"
	[ -z "$staging_leftovers" ]
}

@test "no plaintext .tar.gz artifact remains in the output directory" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	# shellcheck disable=SC2012
	plain_tar="$(find "$APP_ROOT/shared/dr_bundles" -maxdepth 1 -name '*.tar.gz' -not -name '*.gpg')"
	[ -z "$plain_tar" ]
}

@test "RECOVERY_PASSPHRASE never appears in command output" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	assert_output_not_contains "fake-recovery-pass-9f3c"
}

@test "RECOVERY_PASSPHRASE is piped via stdin, never present in the gpg argv log" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	! grep -q "fake-recovery-pass-9f3c" "$MOCK_LOG"
}

@test "RECOVERY_PASSPHRASE never appears in the manifest" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	manifest_file="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.manifest.json)"
	! grep -q "fake-recovery-pass-9f3c" "$manifest_file"
}

@test "secret .env content never appears in command output" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	assert_output_not_contains "fake-fernet-key-do-not-use"
}

@test "secret .env content never appears in the manifest" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	manifest_file="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.manifest.json)"
	! grep -q "fake-fernet-key-do-not-use" "$manifest_file"
}

@test "private key PEM content never appears in command output" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	assert_output_not_contains "FAKE RSA PRIVATE KEY"
}

@test "manifest inventory order is deterministic regardless of fixture creation order" {
	# Deliberately create optional items BEFORE required ones, in reverse
	# of the script's declared inventory order.
	mkdir -p "$APP_ROOT/shared/deploy_state" "$APP_ROOT/shared/certs"
	echo "deadbeef" >"$APP_ROOT/shared/deploy_state/last_known_good_sha"
	echo "fake-cert" >"$APP_ROOT/shared/certs/fullchain.pem"
	seed_required

	run run_bundle
	[ "$status" -eq 0 ]
	manifest_file="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.manifest.json)"
	order="$(grep -o '"path":"[^"]*"' "$manifest_file" | tr '\n' ',')"
	expected='"path":"backend/.env","path":"shared/keys","path":"shared/private_keys","path":"shared/backup.env","path":"shared/certs","path":"shared/deploy_state",'
	[ "$order" = "$expected" ]
}

@test "checksum file matches the actual sha256 of the encrypted artifact" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	artifact="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.tar.gz.gpg)"
	checksum_file="${artifact}.sha256"
	recorded_sha="$(awk '{print $1}' "$checksum_file")"
	actual_sha="$(sha256sum "$artifact" | awk '{print $1}')"
	[ "$recorded_sha" = "$actual_sha" ]
	(cd "$(dirname "$artifact")" && sha256sum -c "$(basename "$checksum_file")")
}

@test "manifest records bundle_version" {
	seed_required
	run run_bundle
	[ "$status" -eq 0 ]
	manifest_file="$(ls "$APP_ROOT"/shared/dr_bundles/wecom_archive-recovery-config-*.manifest.json)"
	grep -q '"bundle_version": 1' "$manifest_file"
	grep -q '"bundle_type": "recovery-config"' "$manifest_file"
}

@test "gpg encryption failure: no .gpg artifact left behind, exit 1" {
	seed_required
	export MOCK_GPG_MODE=fail
	run run_bundle
	[ "$status" -eq 1 ]
	assert_output_contains "encryption of the recovery-config bundle failed"
	[ -z "$(find "$APP_ROOT/shared/dr_bundles" -maxdepth 1 -name '*.gpg' 2>/dev/null)" ]
}

@test "gpg encryption failure triggers a notify.sh alert" {
	seed_required
	export MOCK_GPG_MODE=fail
	run run_bundle
	[ "$status" -eq 1 ]
	grep -q "NOTIFY ERROR|dr-config-bundle|gpg-encrypt-config-bundle" "$MOCK_LOG"
}

@test "missing required artifact triggers a notify.sh alert" {
	echo "FIELD_ENCRYPTION_KEY=fake" >"$APP_ROOT/current/backend/.env"
	run run_bundle
	[ "$status" -eq 1 ]
	grep -q "NOTIFY ERROR|dr-config-bundle|inventory" "$MOCK_LOG"
}

@test "retention is opt-in only: unset BUNDLE_RETENTION_DAYS never deletes prior bundles" {
	seed_required
	mkdir -p "$APP_ROOT/shared/dr_bundles"
	old="$APP_ROOT/shared/dr_bundles/wecom_archive-recovery-config-20260101T000000Z.tar.gz.gpg"
	echo old >"$old"
	touch -t 202601010000 "$old"

	run run_bundle
	[ "$status" -eq 0 ]
	[ -f "$old" ]
	assert_output_not_contains "retention:"
}

@test "retention, when explicitly enabled, removes only this script's own old artifacts" {
	seed_required
	mkdir -p "$APP_ROOT/shared/dr_bundles"
	old="$APP_ROOT/shared/dr_bundles/wecom_archive-recovery-config-20260101T000000Z.tar.gz.gpg"
	unrelated="$APP_ROOT/shared/dr_bundles/some-other-file.txt"
	echo old >"$old"
	echo unrelated >"$unrelated"
	touch -t 202601010000 "$old" "$unrelated"

	run_bundle_with_retention() {
		GPG_BIN="$MOCK_BIN_DIR/mock_gpg" \
			NOTIFY_BIN="$MOCK_BIN_DIR/mock_notify" \
			BUNDLE_RETENTION_DAYS=7 \
			bash "$DR_CONFIG_BUNDLE_SCRIPT"
	}
	run run_bundle_with_retention
	[ "$status" -eq 0 ]
	[ ! -f "$old" ]
	assert_output_contains "retention: removed 1 recovery-config artifact(s)"
}
