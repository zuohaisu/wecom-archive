#!/usr/bin/env bash
#=============================================================================
# notify.sh — Generic webhook alerting for SSL renewal events.
#
# Usage:   notify.sh <LEVEL> <DOMAIN> <MESSAGE...>
# Example: notify.sh ERROR media.crowntime.cn "certID mismatch"
#
# Behavior:
#   - If ALERT_WEBHOOK_URL is unset: logs the alert with a [DEGRADED] tag
#     and exits 0. This is an explicit, visible degrade-to-log-only mode,
#     not a silent no-op.
#   - If ALERT_WEBHOOK_URL is set: POSTs a JSON payload to it.
#       {domain, hostname, failed_stage, timestamp, error_summary}
#   - This script ALWAYS exits 0 (success, timeout, non-2xx, or connection
#     failure alike). A broken alert channel must never change the exit
#     code of the caller's main pipeline — the webhook outcome is only
#     ever logged.
#
# Env:
#   ALERT_WEBHOOK_URL       Webhook endpoint (unset => log-only degrade)
#   ALERT_WEBHOOK_TIMEOUT   Per-attempt timeout in seconds (default: 10)
#   CURRENT_STAGE           Optional; the pipeline stage that failed/warned
#                           (e.g. "qiniu_upload"). Falls back to LEVEL.
#=============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

LEVEL="${1:-INFO}"
DOMAIN="${2:-unknown}"
shift 2 2>/dev/null || true
MESSAGE="$*"
export LOG_TAG="[$DOMAIN]"

ALERT_WEBHOOK_TIMEOUT="${ALERT_WEBHOOK_TIMEOUT:-10}"
FAILED_STAGE="${CURRENT_STAGE:-$LEVEL}"
HOSTNAME_VAL="$(hostname 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Message may contain fragments of command output — always redact before
# it touches a log line or a webhook payload.
SAFE_MESSAGE="$(printf '%s' "$MESSAGE" | filter_secrets)"

echo "[$(_ts)] [NOTIFY] [$LEVEL] [$DOMAIN] $SAFE_MESSAGE"

if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
	echo "[$(_ts)] [NOTIFY] [DEGRADED] ALERT_WEBHOOK_URL not configured — alert logged only, no webhook sent"
	exit 0
fi

if is_dry_run; then
	log_dry "would POST alert to webhook (URL redacted): domain=$DOMAIN failed_stage=$FAILED_STAGE"
	exit 0
fi

payload=$(jq -n \
	--arg domain "$DOMAIN" \
	--arg hostname "$HOSTNAME_VAL" \
	--arg failed_stage "$FAILED_STAGE" \
	--arg timestamp "$TIMESTAMP" \
	--arg error_summary "$SAFE_MESSAGE" \
	'{domain: $domain, hostname: $hostname, failed_stage: $failed_stage, timestamp: $timestamp, error_summary: $error_summary}' \
	2>/dev/null)

if [ -z "$payload" ]; then
	echo "[$(_ts)] [NOTIFY] [WARN] failed to build JSON payload (is jq installed?) — webhook not sent"
	exit 0
fi

resp_file=$(mktemp "${TMPDIR:-/tmp}/notify-webhook.XXXXXX")
http_code=""
if http_code=$(curl -s -o "$resp_file" -w '%{http_code}' \
	--connect-timeout "$ALERT_WEBHOOK_TIMEOUT" --max-time "$ALERT_WEBHOOK_TIMEOUT" \
	-X POST "$ALERT_WEBHOOK_URL" \
	-H 'Content-Type: application/json' \
	-d "$payload" 2>/dev/null); then
	case "$http_code" in
	2??)
		echo "[$(_ts)] [NOTIFY] [OK] webhook delivered (HTTP $http_code)"
		;;
	*)
		echo "[$(_ts)] [NOTIFY] [WARN] webhook returned non-2xx: HTTP $http_code — alert already logged above, main pipeline exit code unaffected"
		;;
	esac
else
	curl_status=$?
	if [ "$curl_status" -eq 28 ]; then
		echo "[$(_ts)] [NOTIFY] [WARN] webhook request timed out after ${ALERT_WEBHOOK_TIMEOUT}s — alert already logged above, main pipeline exit code unaffected"
	else
		echo "[$(_ts)] [NOTIFY] [WARN] webhook request failed (curl exit=$curl_status, connection error) — alert already logged above, main pipeline exit code unaffected"
	fi
fi
rm -f "$resp_file"

exit 0
