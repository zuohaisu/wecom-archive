#!/usr/bin/env bash
#=============================================================================
# Mock `notify.sh` (ssl-renew/notify.sh's contract: `notify.sh <LEVEL> <TAG>
# <MESSAGE...>`) used by both disk_usage_check.bats and backup_once.bats to
# assert an alert was (or was not) sent, without a real webhook. Also
# surfaces CURRENT_STAGE (the real notify.sh's FAILED_STAGE input) so tests
# can assert callers set it to something more specific than the bare LEVEL.
#
# Controlled via:
#   MOCK_NOTIFY_LOG   if set, "LEVEL|TAG|MESSAGE|CURRENT_STAGE" is appended here
#=============================================================================
level="${1:-}"
tag="${2:-}"
shift 2 2>/dev/null || true
message="$*"

if [ -n "${MOCK_NOTIFY_LOG:-}" ]; then
	printf 'NOTIFY %s|%s|%s|%s\n' "$level" "$tag" "$message" "${CURRENT_STAGE:-}" >>"$MOCK_NOTIFY_LOG"
fi
exit 0
