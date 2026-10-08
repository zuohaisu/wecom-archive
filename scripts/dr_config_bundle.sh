#!/usr/bin/env bash
#
# dr_config_bundle.sh — GH-105 disaster-recovery baseline: encrypted
# recovery-config bundle.
#
# scripts/backup_once.sh (RND-193) already covers PostgreSQL + local media —
# the DATA. It does NOT cover the recovery-critical CONFIG/KEY material a
# freshly provisioned host needs before that data means anything again:
# backend/.env (DB credentials, WECOM_ARCHIVE_SECRET, FIELD_ENCRYPTION_KEY —
# the root key that unlocks every kms_envelope-stored tenant private key in
# the database — third-party API/payment credentials, ...) and the on-disk
# RSA private key material under shared/keys/. None of that lives anywhere
# else: not in git, not in a provider console, not in the operator's
# password manager. Losing the Aliyun host without an off-host copy of this
# would mean the DB/media backup itself becomes undecryptable/unusable even
# though the ciphertext bytes are intact elsewhere.
#
# This script does NOT touch backup_once.sh's DB/media backup chain, does
# NOT reuse BACKUP_GPG_PASSPHRASE (independent secret, independent
# rotation), and does NOT configure Tencent/Mac off-host pull, retention,
# or a new systemd timer — see the DR "Recovery config bundle" section in
# the ops runbook (local ops records, moved out of the tree in GH-203)
# for the full picture and Ops handoff.
#
# ── Recovery-critical inventory (explicit — never a recursive shared/ tar)
#
#   REQUIRED (fail closed if missing/empty — bundle is refused):
#     backend/.env    — runtime secrets; not reconstructable from git,
#                        provider console, or the operator's password
#                        manager.
#     shared/keys/    — on-disk RSA private key material (e.g.
#                        private_key_v1.pem); WeCom cannot reissue a lost
#                        private key, only rotate to a new one, which is a
#                        production incident, not a recovery.
#
#   OPTIONAL (included if present, never fail-closed):
#     shared/private_keys/ — legacy key-path convention seen alongside
#                             shared/keys/ in the ops runbook (local ops records).
#     shared/backup.env    — BACKUP_GPG_PASSPHRASE. NOT required: the
#                             operator already keeps an independent copy in
#                             KeePassXC. Included anyway, ciphertext-only,
#                             as defense in depth against that copy being
#                             lost too.
#     shared/certs/         — TLS material. NOT required: the wildcard
#                              *.crowntime.cn cert is ACME-reissuable (see
#                              docs/operations/scheduled-workload-manifest.md).
#                              Included when present purely to shorten RTO.
#     shared/deploy_state/  — last_known_good_sha etc. NOT required: fully
#                              reconstructable from the current main SHA on
#                              a fresh checkout. Included when present for
#                              rollback-target continuity.
#
# ── Required env ────────────────────────────────────────────────────────
#   RECOVERY_PASSPHRASE   Symmetric encryption passphrase for this bundle.
#                         Deliberately INDEPENDENT of BACKUP_GPG_PASSPHRASE
#                         so the two can be rotated separately. Read from
#                         the environment ONLY — never a CLI argument,
#                         never logged, never echoed. Piped to gpg via
#                         --passphrase-fd 0, exactly like backup_once.sh
#                         does for BACKUP_GPG_PASSPHRASE.
#
# ── Optional env ────────────────────────────────────────────────────────
#   APP_ROOT              Production app root (default:
#                         /srv/apps/wecom-archive-365)
#   BACKEND_ENV_FILE      Path to backend/.env (default: $APP_ROOT/current/backend/.env)
#   SHARED_DIR            Path to shared/ (default: $APP_ROOT/shared)
#   BUNDLE_OUTPUT_DIR     Where the encrypted bundle + manifest are written
#                         (default: $SHARED_DIR/dr_bundles — deliberately a
#                         separate directory from backup_once.sh's
#                         shared/backups, so the two artifact sets, and
#                         their retention, never collide)
#   BUNDLE_STAGING_DIR    Where the plaintext bundle is staged before
#                         encryption (default: BUNDLE_OUTPUT_DIR/.tmp — on
#                         disk, not /tmp; same tmpfs-memory-pressure
#                         rationale as backup_once.sh on this project's 2GB
#                         production box). Always cleaned up via trap, even
#                         on failure.
#   BUNDLE_RETENTION_DAYS Delete this script's own bundles older than N
#                         days, AFTER a new bundle succeeds. Unset by
#                         default (no cleanup) — retention policy is an
#                         Ops decision (see #105 scope notes), not
#                         something this script hardcodes.
#   ALERT_WEBHOOK_URL     Passed straight through to notify.sh on failure.
#
# Command overrides (for tests — never needed in production):
#   GPG_BIN, TAR_BIN, SHA256SUM_BIN, NOTIFY_BIN
#
# Exit codes: 0 success, 1 missing required env / missing required
# inventory artifact / encryption failure — always fail closed, never a
# partial or silently-degraded bundle.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_ROOT="${APP_ROOT:-/srv/apps/wecom-archive-365}"
BACKEND_ENV_FILE="${BACKEND_ENV_FILE:-$APP_ROOT/current/backend/.env}"
SHARED_DIR="${SHARED_DIR:-$APP_ROOT/shared}"
BUNDLE_OUTPUT_DIR="${BUNDLE_OUTPUT_DIR:-$SHARED_DIR/dr_bundles}"
BUNDLE_RETENTION_DAYS="${BUNDLE_RETENTION_DAYS:-}"

GPG_BIN="${GPG_BIN:-gpg}"
TAR_BIN="${TAR_BIN:-tar}"
SHA256SUM_BIN="${SHA256SUM_BIN:-sha256sum}"
NOTIFY_BIN="${NOTIFY_BIN:-$SCRIPT_DIR/../ssl-renew/notify.sh}"

BUNDLE_SCHEMA_VERSION=1
BUNDLE_NAME_PREFIX="wecom_archive-recovery-config"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "[$(_ts)] $*"; }

_alert_failure() {
	local stage="$1" detail="$2"
	log "[ERROR] $stage: $detail"
	if [ -x "$NOTIFY_BIN" ]; then
		CURRENT_STAGE="$stage" "$NOTIFY_BIN" ERROR "dr-config-bundle" "$stage: $detail" || true
	else
		log "[WARN] notify.sh not found/executable at $NOTIFY_BIN — alert logged only"
	fi
}

# An item is "present" only if it carries real content — an empty
# directory or a zero-byte file is treated the same as missing, because
# either one is useless for recovery.
_artifact_present() {
	local path="$1"
	if [ -d "$path" ]; then
		[ -n "$(find "$path" -type f -print -quit 2>/dev/null)" ]
	elif [ -f "$path" ]; then
		[ -s "$path" ]
	else
		return 1
	fi
}

if [ -z "${RECOVERY_PASSPHRASE:-}" ]; then
	log "[ERROR] RECOVERY_PASSPHRASE is not set — refusing to run"
	exit 1
fi

umask 077
mkdir -p "$BUNDLE_OUTPUT_DIR"
BUNDLE_STAGING_DIR="${BUNDLE_STAGING_DIR:-$BUNDLE_OUTPUT_DIR/.tmp}"
mkdir -p "$BUNDLE_STAGING_DIR"

STAGE_DIR="$(mktemp -d "$BUNDLE_STAGING_DIR/dr-config-bundle.XXXXXX")"
chmod 700 "$STAGE_DIR"
trap 'rm -rf "$STAGE_DIR"' EXIT

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE_BASENAME="$BUNDLE_NAME_PREFIX-$TS"
COLLECT_DIR="$STAGE_DIR/$BUNDLE_BASENAME"
mkdir -p "$COLLECT_DIR"

# ── Explicit, ordered inventory — parallel arrays, not an assoc array,
# so iteration order (and therefore bundle contents) is deterministic
# regardless of bash's hashing.
INVENTORY_LABELS=(
	"backend/.env"
	"shared/keys"
	"shared/private_keys"
	"shared/backup.env"
	"shared/certs"
	"shared/deploy_state"
)
INVENTORY_SRC=(
	"$BACKEND_ENV_FILE"
	"$SHARED_DIR/keys"
	"$SHARED_DIR/private_keys"
	"$SHARED_DIR/backup.env"
	"$SHARED_DIR/certs"
	"$SHARED_DIR/deploy_state"
)
INVENTORY_REQUIRED=(1 1 0 0 0 0)

missing_required=0
manifest_items=()

for i in "${!INVENTORY_LABELS[@]}"; do
	label="${INVENTORY_LABELS[$i]}"
	src="${INVENTORY_SRC[$i]}"
	required="${INVENTORY_REQUIRED[$i]}"

	if ! _artifact_present "$src"; then
		if [ "$required" = "1" ]; then
			log "[ERROR] required recovery artifact missing: $label"
			missing_required=1
			manifest_items+=("{\"path\":\"$label\",\"required\":true,\"included\":false}")
		else
			log "[SKIP] optional recovery artifact not present: $label"
			manifest_items+=("{\"path\":\"$label\",\"required\":false,\"included\":false}")
		fi
		continue
	fi

	dest="$COLLECT_DIR/$label"
	mkdir -p "$(dirname "$dest")"
	if [ -d "$src" ]; then
		mkdir -p "$dest"
		if ! cp -a "$src"/. "$dest"/; then
			_alert_failure "collect" "failed to stage recovery artifact: $label"
			exit 1
		fi
	else
		if ! cp -a "$src" "$dest"; then
			_alert_failure "collect" "failed to stage recovery artifact: $label"
			exit 1
		fi
	fi
	log "[OK] collected: $label"
	manifest_items+=("{\"path\":\"$label\",\"required\":$([ "$required" = "1" ] && echo true || echo false),\"included\":true}")
done

if [ "$missing_required" = "1" ]; then
	_alert_failure "inventory" "one or more required recovery artifacts missing — no bundle written"
	exit 1
fi

# ── Stage → tar → encrypt → final .gpg → plaintext cleanup (via trap) ──
TAR_PLAIN="$STAGE_DIR/$BUNDLE_BASENAME.tar.gz"
if ! "$TAR_BIN" czf "$TAR_PLAIN" -C "$STAGE_DIR" "$BUNDLE_BASENAME"; then
	_alert_failure "tar-config" "archiving the staged recovery-config directory failed — no bundle written"
	exit 1
fi

BUNDLE_ENC="$BUNDLE_OUTPUT_DIR/$BUNDLE_BASENAME.tar.gz.gpg"
if ! printf '%s' "$RECOVERY_PASSPHRASE" |
	"$GPG_BIN" --batch --yes --passphrase-fd 0 --pinentry-mode loopback \
		--symmetric --cipher-algo AES256 \
		-o "$BUNDLE_ENC" "$TAR_PLAIN"; then
	_alert_failure "gpg-encrypt-config-bundle" "encryption of the recovery-config bundle failed — no bundle written"
	rm -f "$BUNDLE_ENC"
	exit 1
fi

BUNDLE_SIZE="$(wc -c <"$BUNDLE_ENC" | tr -d ' ')"
BUNDLE_SHA256="$("$SHA256SUM_BIN" "$BUNDLE_ENC" | awk '{print $1}')"

# ── Integrity evidence: checksum + manifest, both external to the
# ciphertext, neither containing a secret value or env value. ──────────
CHECKSUM_FILE="$BUNDLE_OUTPUT_DIR/$BUNDLE_BASENAME.tar.gz.gpg.sha256"
printf '%s  %s\n' "$BUNDLE_SHA256" "$BUNDLE_BASENAME.tar.gz.gpg" >"$CHECKSUM_FILE"

CREATED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
MANIFEST_FILE="$BUNDLE_OUTPUT_DIR/$BUNDLE_BASENAME.manifest.json"
{
	printf '{\n'
	printf '  "bundle_version": %s,\n' "$BUNDLE_SCHEMA_VERSION"
	printf '  "bundle_type": "recovery-config",\n'
	printf '  "artifact": "%s",\n' "$BUNDLE_BASENAME.tar.gz.gpg"
	printf '  "sha256": "%s",\n' "$BUNDLE_SHA256"
	printf '  "size_bytes": %s,\n' "$BUNDLE_SIZE"
	printf '  "created_at": "%s",\n' "$CREATED_AT"
	printf '  "inventory": [\n'
	for i in "${!manifest_items[@]}"; do
		sep=","
		[ "$i" -eq $((${#manifest_items[@]} - 1)) ] && sep=""
		printf '    %s%s\n' "${manifest_items[$i]}" "$sep"
	done
	printf '  ]\n'
	printf '}\n'
} >"$MANIFEST_FILE"

log "recovery_bundle status=completed artifact=$BUNDLE_BASENAME.tar.gz.gpg size=${BUNDLE_SIZE}B sha256=$BUNDLE_SHA256 bundle_version=$BUNDLE_SCHEMA_VERSION"

# ── Retention — opt-in only, never runs unless the operator sets a value,
# and only after this run's own bundle already exists on disk. ─────────
if [ -n "$BUNDLE_RETENTION_DAYS" ]; then
	deleted_count=0
	while IFS= read -r -d '' old_file; do
		rm -f "$old_file"
		deleted_count=$((deleted_count + 1))
	done < <(find "$BUNDLE_OUTPUT_DIR" -maxdepth 1 -name "$BUNDLE_NAME_PREFIX-*" -mtime "+$BUNDLE_RETENTION_DAYS" -print0 2>/dev/null)
	log "[OK] retention: removed $deleted_count recovery-config artifact(s) older than ${BUNDLE_RETENTION_DAYS} day(s) from $BUNDLE_OUTPUT_DIR"
fi

exit 0
