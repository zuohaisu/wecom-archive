#!/usr/bin/env bash
#
# dr_pull.sh — GH-105 disaster-recovery baseline: destination-side pull +
# verify for scripts/dr_config_bundle.sh's recovery-config bundles.
#
# Scope decision (see #105 ticket notes, "dr_pull.sh 是否需要实现"): a plain
# `rsync` + `sha256sum -c` one-liner would be enough for a SINGLE
# destination, but this needs to run identically on two independent,
# unrelated destinations (Tencent Cloud Singapore, and a local Mac) with
# the same non-negotiable invariant — partial transfer != valid backup —
# and "verify before it can ever become the new known-good copy, never
# delete the previous known-good copy on a bad transfer" is exactly the
# kind of logic that is easy to get subtly wrong twice. Hence one small,
# tested, destination-side helper instead of duplicated ad-hoc cron
# one-liners.
#
# This is intentionally NOT a general backup-pull tool: it only pulls and
# verifies scripts/dr_config_bundle.sh's <name>.tar.gz.gpg +
# <name>.tar.gz.gpg.sha256 artifact pairs (the only artifacts this repo
# currently ships an external checksum for). scripts/backup_once.sh's
# DB/media *.gpg artifacts do not currently carry a checksum file, so this
# script deliberately does not touch them — see backup_once.sh's own
# header for why that script is not being refactored as part of #105.
# Pulling those is a plain `rsync`/`ssh` operation Ops can run directly
# (see docs/operations/2c2g-runbook.md).
#
# ── Design ──────────────────────────────────────────────────────────────
#   remote artifact → local staging (rsync) → sha256 verify →
#   atomic rename into the verified tree
#
#   - Runs ON THE DESTINATION (Tencent host or the Mac), pulling FROM
#     production. Never the reverse — production never pushes.
#   - A checksum mismatch (or a missing/corrupt .sha256 companion) FAILS
#     that one artifact and leaves the previous known-good copy in
#     PULL_DEST_DIR untouched. This script never deletes anything already
#     inside PULL_DEST_DIR.
#   - Idempotent: re-running with nothing new to pull, or re-pulling an
#     artifact that is already verified and present, is a no-op for that
#     artifact (logged as SKIP, not an error).
#
# ── Required env ────────────────────────────────────────────────────────
#   PULL_SOURCE      rsync source spec for the producer's bundle
#                    directory, e.g.
#                    "wecomarchive@ali-xy-qw:/srv/apps/wecom-archive-365/shared/dr_bundles/"
#                    (trailing slash matters — rsync copies CONTENTS of
#                    this directory, not the directory itself). May also
#                    be a local path for a destination that mounts
#                    production storage directly.
#   PULL_DEST_DIR    Local "verified" tree — only ever contains artifacts
#                    that passed checksum verification.
#
# ── Optional env ────────────────────────────────────────────────────────
#   PULL_STAGING_DIR Local staging area for in-flight transfers (default:
#                    PULL_DEST_DIR/.incoming). A partial/interrupted
#                    transfer here simply fails its checksum check and is
#                    never promoted — it can never masquerade as a valid
#                    backup.
#   RSYNC_OPTS       Extra rsync flags (e.g. "-e 'ssh -i /path/to/key'"),
#                    appended after the built-in safe defaults. Passed
#                    through a bash array, never eval'd.
#
# Command overrides (for tests — never needed in production):
#   RSYNC_BIN, SHA256SUM_BIN
#
# Exit codes: 0 all discovered artifacts verified/promoted (or already
# up to date); 1 at least one artifact failed checksum verification or
# could not be verified (missing/corrupt .sha256) — the run still
# attempts every other artifact before reporting failure, and never
# overwrites a previous known-good file.
#
set -uo pipefail

RSYNC_BIN="${RSYNC_BIN:-rsync}"
SHA256SUM_BIN="${SHA256SUM_BIN:-sha256sum}"
IFS=' ' read -r -a RSYNC_OPTS <<<"${RSYNC_OPTS:-}"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "[$(_ts)] $*"; }

if [ -z "${PULL_SOURCE:-}" ]; then
	log "[ERROR] PULL_SOURCE is not set — refusing to run"
	exit 1
fi
if [ -z "${PULL_DEST_DIR:-}" ]; then
	log "[ERROR] PULL_DEST_DIR is not set — refusing to run"
	exit 1
fi

PULL_STAGING_DIR="${PULL_STAGING_DIR:-$PULL_DEST_DIR/.incoming}"

mkdir -p "$PULL_DEST_DIR" "$PULL_STAGING_DIR"

# Never sync the producer's own plaintext-staging scratch directory —
# scripts/dr_config_bundle.sh's BUNDLE_OUTPUT_DIR/.tmp can legitimately
# contain a bundle mid-encryption at the moment this runs.
if ! "$RSYNC_BIN" -a --exclude='.tmp' --exclude='.tmp/**' \
	"${RSYNC_OPTS[@]}" \
	"$PULL_SOURCE" "$PULL_STAGING_DIR/"; then
	log "[ERROR] rsync from PULL_SOURCE failed — no artifact promoted this run"
	exit 1
fi

failures=0
promoted=0
skipped=0

shopt -s nullglob
for artifact in "$PULL_STAGING_DIR"/*.tar.gz.gpg; do
	name="$(basename "$artifact")"
	checksum_file="$artifact.sha256"
	dest_artifact="$PULL_DEST_DIR/$name"

	if [ -f "$dest_artifact" ]; then
		existing_sha="$("$SHA256SUM_BIN" "$dest_artifact" | awk '{print $1}')"
		staged_sha="$("$SHA256SUM_BIN" "$artifact" | awk '{print $1}')"
		if [ "$existing_sha" = "$staged_sha" ]; then
			log "[SKIP] already verified and present: $name"
			skipped=$((skipped + 1))
			continue
		fi
	fi

	if [ ! -f "$checksum_file" ]; then
		log "[ERROR] checksum file missing for $name — not promoted, previous known-good (if any) retained"
		failures=$((failures + 1))
		continue
	fi

	if ! (cd "$PULL_STAGING_DIR" && "$SHA256SUM_BIN" -c "$(basename "$checksum_file")") >/dev/null 2>&1; then
		log "[ERROR] checksum verification FAILED for $name — not promoted, previous known-good (if any) retained"
		failures=$((failures + 1))
		continue
	fi

	# Atomic promotion: rename within the same filesystem (PULL_STAGING_DIR
	# and PULL_DEST_DIR must be on the same filesystem for this to be
	# atomic; both default under the same PULL_DEST_DIR tree).
	if ! mv -f "$artifact" "$dest_artifact"; then
		log "[ERROR] promotion (rename) failed for $name — previous known-good (if any) retained"
		failures=$((failures + 1))
		continue
	fi
	mv -f "$checksum_file" "$PULL_DEST_DIR/$(basename "$checksum_file")" 2>/dev/null || true
	manifest_file="${artifact%.tar.gz.gpg}.manifest.json"
	if [ -f "$manifest_file" ]; then
		mv -f "$manifest_file" "$PULL_DEST_DIR/$(basename "$manifest_file")" 2>/dev/null || true
	fi

	log "[OK] verified and promoted: $name"
	promoted=$((promoted + 1))
done
shopt -u nullglob

log "dr_pull status=$([ "$failures" -eq 0 ] && echo completed || echo partial) promoted=$promoted skipped=$skipped failed=$failures"

[ "$failures" -eq 0 ]
