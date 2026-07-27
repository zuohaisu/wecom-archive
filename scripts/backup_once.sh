#!/usr/bin/env bash
#
# backup_once.sh — RND-193 encrypted backup: PostgreSQL (pg_dump, custom
# format) + the LOCAL legacy media directory (tar). Every artifact this
# script writes is GPG-symmetric-encrypted before it touches disk under
# BACKUP_DIR — no plaintext dump or archive is ever persisted.
#
# Scope note: media backed by Qiniu Kodo (the current write backend — see
# docs/ops/media_storage_ops.md) already lives in Qiniu's own durable
# object storage and is intentionally NOT re-backed-up here. This script
# only covers the historical `local` storage_backend media directory
# (STORAGE_LOCAL_PATH), which is the one copy that exists nowhere else.
#
# A single local backup directory is NOT a complete disaster-recovery
# plan — see docs/operations/2c2g-runbook.md, "Backup scope and what this
# does NOT cover".
#
# ── Required env ────────────────────────────────────────────────────────
#   DATABASE_URL           PostgreSQL connection string (same one the app uses)
#   BACKUP_GPG_PASSPHRASE  Symmetric encryption passphrase. Never logged,
#                          never passed as a bare CLI argument (piped via
#                          --passphrase-fd instead, so it never appears in
#                          `ps`/process listings). If unset, this script
#                          refuses to run rather than writing an
#                          unencrypted backup.
#
# ── Optional env ────────────────────────────────────────────────────────
#   BACKUP_DIR             Output directory (default: /srv/apps/wecom-archive-365/shared/backups)
#   STORAGE_LOCAL_PATH     Local legacy media directory. If unset, empty,
#                          or missing, the media backup step is skipped
#                          (logged explicitly, not silently).
#   BACKUP_RETENTION_DAYS  Delete this script's own backups older than N
#                          days, AFTER a new backup succeeds (default: 7).
#                          Never touches anything outside BACKUP_DIR, and
#                          never runs at all if today's backup failed —
#                          a failed run never costs you an existing backup.
#   ALERT_WEBHOOK_URL      Passed straight through to notify.sh on failure.
#   BACKUP_STAGING_DIR     Where the plaintext dump/tar are staged before
#                          encryption (default: BACKUP_DIR/.tmp). Deliberately
#                          NOT /tmp — on this project's production host (and
#                          many systemd-managed Linux hosts) /tmp is tmpfs,
#                          i.e. RAM-backed, which would compete with the
#                          app/worker for memory on a 2GB box and hard-fail
#                          past tmpfs's size cap. Always cleaned up via trap,
#                          even on failure.
#
# Command overrides (for tests — never needed in production):
#   PG_DUMP_BIN, GPG_BIN, TAR_BIN, NOTIFY_BIN
#
# Exit codes: 0 success (media step may have been skipped — see log),
# 1 the DB backup failed (the one thing this script must not silently accept).
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BACKUP_DIR="${BACKUP_DIR:-/srv/apps/wecom-archive-365/shared/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
STORAGE_LOCAL_PATH="${STORAGE_LOCAL_PATH:-}"

PG_DUMP_BIN="${PG_DUMP_BIN:-pg_dump}"
GPG_BIN="${GPG_BIN:-gpg}"
TAR_BIN="${TAR_BIN:-tar}"
NOTIFY_BIN="${NOTIFY_BIN:-$SCRIPT_DIR/../ssl-renew/notify.sh}"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "[$(_ts)] $*"; }

# Same connection-string redaction pattern as deploy_server.sh's _redact —
# never let a captured stderr line leak the DB password.
_redact() {
	sed -E 's#://[^:/@[:space:]]+:[^@[:space:]]*@#://REDACTED@#g'
}

_alert_failure() {
	local stage="$1" detail="$2"
	log "[ERROR] $stage: $detail"
	if [ -x "$NOTIFY_BIN" ]; then
		CURRENT_STAGE="$stage" "$NOTIFY_BIN" ERROR "backup-once" "$stage: $detail" || true
	else
		log "[WARN] notify.sh not found/executable at $NOTIFY_BIN — alert logged only"
	fi
}

if [ -z "${DATABASE_URL:-}" ]; then
	log "[ERROR] DATABASE_URL is not set — refusing to run"
	exit 1
fi
if [ -z "${BACKUP_GPG_PASSPHRASE:-}" ]; then
	log "[ERROR] BACKUP_GPG_PASSPHRASE is not set — refusing to write an unencrypted backup"
	exit 1
fi

mkdir -p "$BACKUP_DIR"

# Staging lives under BACKUP_DIR, NOT $TMPDIR/tmp — deliberately not the
# host's default /tmp, which on this project's production box (and many
# systemd-managed Linux hosts) is tmpfs, i.e. backed by RAM. On a 2GB box,
# a plaintext DB dump + media tar staged there would compete with the
# app/worker for memory during the backup window, and would hard-fail if
# it ever exceeded tmpfs's size cap — neither of which can happen when
# staging goes to the same on-disk filesystem the final encrypted output
# already lives on.
BACKUP_STAGING_DIR="${BACKUP_STAGING_DIR:-$BACKUP_DIR/.tmp}"
mkdir -p "$BACKUP_STAGING_DIR"
TMPDIR_BACKUP="$(mktemp -d "$BACKUP_STAGING_DIR/backup-once.XXXXXX")"
trap 'rm -rf "$TMPDIR_BACKUP"' EXIT

TS="$(date -u +%Y%m%dT%H%M%SZ)"

# ── 1. PostgreSQL dump (custom format — compressed, pg_restore-able) ────
DB_DUMP_PLAIN="$TMPDIR_BACKUP/wecom_archive-db-$TS.dump"
if ! "$PG_DUMP_BIN" "$DATABASE_URL" -Fc -f "$DB_DUMP_PLAIN" 2> >(_redact >&2); then
	_alert_failure "pg_dump" "pg_dump exited non-zero — no backup written this run"
	exit 1
fi

DB_DUMP_ENC="$BACKUP_DIR/wecom_archive-db-$TS.dump.gpg"
if ! printf '%s' "$BACKUP_GPG_PASSPHRASE" |
	"$GPG_BIN" --batch --yes --passphrase-fd 0 --pinentry-mode loopback \
		--symmetric --cipher-algo AES256 \
		-o "$DB_DUMP_ENC" "$DB_DUMP_PLAIN" 2> >(_redact >&2); then
	_alert_failure "gpg-encrypt-db" "encryption of the DB dump failed — no backup written this run"
	rm -f "$DB_DUMP_ENC"
	exit 1
fi
log "[OK] DB backup encrypted: $DB_DUMP_ENC ($(du -h "$DB_DUMP_ENC" | cut -f1))"

# ── 2. Local legacy media (skip explicitly if not configured/present) ──
MEDIA_BACKED_UP=0
if [ -z "$STORAGE_LOCAL_PATH" ]; then
	log "[SKIP] media backup: STORAGE_LOCAL_PATH not set"
elif [ ! -d "$STORAGE_LOCAL_PATH" ]; then
	log "[SKIP] media backup: STORAGE_LOCAL_PATH ($STORAGE_LOCAL_PATH) does not exist"
elif [ -z "$(find "$STORAGE_LOCAL_PATH" -type f -print -quit 2>/dev/null)" ]; then
	log "[SKIP] media backup: STORAGE_LOCAL_PATH ($STORAGE_LOCAL_PATH) has no files"
else
	MEDIA_TAR_PLAIN="$TMPDIR_BACKUP/wecom_archive-media-$TS.tar.gz"
	media_parent="$(dirname "$STORAGE_LOCAL_PATH")"
	media_base="$(basename "$STORAGE_LOCAL_PATH")"
	if ! "$TAR_BIN" czf "$MEDIA_TAR_PLAIN" -C "$media_parent" "$media_base" 2> >(_redact >&2); then
		_alert_failure "tar-media" "archiving STORAGE_LOCAL_PATH failed — DB backup above is still valid, media step skipped this run"
	else
		MEDIA_TAR_ENC="$BACKUP_DIR/wecom_archive-media-$TS.tar.gz.gpg"
		if ! printf '%s' "$BACKUP_GPG_PASSPHRASE" |
			"$GPG_BIN" --batch --yes --passphrase-fd 0 --pinentry-mode loopback \
				--symmetric --cipher-algo AES256 \
				-o "$MEDIA_TAR_ENC" "$MEDIA_TAR_PLAIN" 2> >(_redact >&2); then
			_alert_failure "gpg-encrypt-media" "encryption of the media archive failed — DB backup above is still valid, media step skipped this run"
			rm -f "$MEDIA_TAR_ENC"
		else
			log "[OK] media backup encrypted: $MEDIA_TAR_ENC ($(du -h "$MEDIA_TAR_ENC" | cut -f1))"
			MEDIA_BACKED_UP=1
		fi
	fi
fi

# ── 3. Retention — only runs after a successful DB backup above ────────
deleted_count=0
while IFS= read -r -d '' old_file; do
	rm -f "$old_file"
	deleted_count=$((deleted_count + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -name 'wecom_archive-*.gpg' -mtime "+$BACKUP_RETENTION_DAYS" -print0 2>/dev/null)
log "[OK] retention: removed $deleted_count backup file(s) older than ${BACKUP_RETENTION_DAYS} day(s) from $BACKUP_DIR"

log "[OK] backup_once complete (db=1 media=$MEDIA_BACKED_UP)"
exit 0
