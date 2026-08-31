#!/usr/bin/env bash
#=============================================================================
# lib/common.sh — shared logging, secret filtering, config loading, and
# domain validation helpers used by renew.sh, notify.sh and verify_https.sh.
#
# Not directly executable. Source it:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=lib/common.sh
#   source "$SCRIPT_DIR/lib/common.sh"
#=============================================================================

# Guard against double-sourcing.
# shellcheck disable=SC2317  # reachable when sourced a second time
if [ -n "${SSL_RENEW_COMMON_LOADED:-}" ]; then
	return 0 2>/dev/null || exit 0
fi
SSL_RENEW_COMMON_LOADED=1

# ── Exit codes (shared vocabulary across scripts) ───────────────────────────
export EXIT_OK=0
export EXIT_GENERIC_FAILURE=1
export EXIT_USAGE_ERROR=2

# ── Dry-run / staging flags ─────────────────────────────────────────────────
# Normalize to "1" / "" so callers can test with [ -n "$DRY_RUN" ].
# Exported: notify.sh and verify_https.sh run as child processes and must
# see the same mode as their parent.
export DRY_RUN="${DRY_RUN:-}"
export ACME_STAGING="${ACME_STAGING:-}"

is_dry_run() { [ -n "$DRY_RUN" ] && [ "$DRY_RUN" != "0" ]; }
is_staging() { [ -n "$ACME_STAGING" ] && [ "$ACME_STAGING" != "0" ]; }

# ── Logging ──────────────────────────────────────────────────────────────
# LOG_TAG may be set by the caller (e.g. "[media.example.com]"). Exported
# so it's consistent if a caller forks a helper process that also logs.
export LOG_TAG="${LOG_TAG:-}"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

log() {
	echo "[$(_ts)] ${LOG_TAG} $*"
}

log_dry() {
	echo "[$(_ts)] ${LOG_TAG} [DRY-RUN] $*"
}

# ── Secret filtering ─────────────────────────────────────────────────────
# Redacts common secret-shaped tokens from arbitrary text. Used before ANY
# external command output, API response, or user-supplied message is logged
# or forwarded to notify.sh / a webhook.
filter_secrets() {
	sed -E \
		-e 's/(DP_Id|DP_Key|QINIU_ACCESS_KEY|QINIU_SECRET_KEY|SAVED_QINIU_AK|SAVED_QINIU_SK|ALERT_WEBHOOK_URL)=[^[:space:]"'\'']*/\1=[REDACTED]/g' \
		-e 's/"?(DP_|QINIU_|SAVED_QINIU_|TOKEN|SECRET|WEBHOOK_URL)[A-Za-z0-9_]*"?[[:space:]]*[:=][[:space:]]*"?[^[:space:]"'\'',}]*"?/[REDACTED]/g' \
		-e 's#(https?://)[^[:space:]"'\'']*@#\1[REDACTED]@#g' \
		-e 's/(Authorization[[:space:]]*:[[:space:]]*QBox[[:space:]]+)[A-Za-z0-9_:-]+/\1[REDACTED]/g'
}

# ── Canonical failure reporting ─────────────────────────────────────────
# warn/die deliberately only report. The caller chooses alert delivery:
# qiniu-ssl-renew-wildcard.service relies on systemd OnFailure= so a hard
# failure creates exactly one alert, while warn-only paths remain non-fatal.
# DOMAIN is used by renew.sh; the wildcard flow supplies its fixed domain via
# SSL_RENEW_ALERT_DOMAIN rather than reading DOMAIN from its EnvironmentFile.
_ssl_renew_safe_message() {
	local message

	# Do not emit an unfiltered message if the filtering runtime itself is
	# unavailable. The marker is fixed, contains no secret, and still leaves
	# the structured stage/domain fields for operator diagnosis.
	if ! command -v sed >/dev/null 2>&1; then
		printf '%s' 'secret-filter-unavailable'
		return 0
	fi

	if ! message=$(printf '%s' "$*" | filter_secrets); then
		printf '%s' 'secret-filter-failed'
		return 0
	fi
	printf '%s' "$message"
}

_ssl_renew_report() {
	local level="$1"
	shift
	local stage="${CURRENT_STAGE:-unknown}"
	local domain="${SSL_RENEW_ALERT_DOMAIN:-${DOMAIN:-unknown}}"
	local safe_domain safe_message
	safe_domain=$(_ssl_renew_safe_message "$domain")
	safe_message=$(_ssl_renew_safe_message "$*")

	# stderr is the canonical stream for warning/error diagnostics. Systemd
	# captures both streams into the same renewal log, while callers and tests
	# can reliably distinguish diagnostics from normal progress output.
	printf '[%s] [%s] stage=%s domain=%s message=%s\n' \
		"$(_ts)" "$level" "$stage" "$safe_domain" "$safe_message" >&2
}

warn() {
	_ssl_renew_report WARN "$@"
}

die() {
	_ssl_renew_report ERROR "$@"
	exit "$EXIT_GENERIC_FAILURE"
}

# ── Domain validation ────────────────────────────────────────────────────
# Conservative RFC-1123-ish hostname check: labels of alnum/hyphen, 1-63
# chars, no leading/trailing hyphen, at least one dot, total <= 253 chars.
validate_domain() {
	local domain="$1"

	if [ -z "$domain" ]; then
		echo "domain is empty" >&2
		return 1
	fi
	if [ "${#domain}" -gt 253 ]; then
		echo "domain exceeds 253 characters: $domain" >&2
		return 1
	fi
	if [[ ! "$domain" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; then
		echo "domain fails format validation: $domain" >&2
		return 1
	fi
	return 0
}

# ── Config loading ───────────────────────────────────────────────────────
# If CONFIG_FILE is set, source it. Designed so systemd's EnvironmentFile=
# (which populates the environment before the process starts) and a
# manually-sourced file behave identically for the rest of the script.
load_config() {
	if [ -n "${CONFIG_FILE:-}" ]; then
		if [ ! -f "$CONFIG_FILE" ]; then
			echo "config file not found: $CONFIG_FILE" >&2
			return "$EXIT_USAGE_ERROR"
		fi
		# shellcheck disable=SC1090
		source "$CONFIG_FILE"
	fi
	return 0
}

# ── Portable timeout wrapper ─────────────────────────────────────────────
# Prefers GNU `timeout` (Linux prod, or `gtimeout` from macOS coreutils);
# falls back to a background-process + kill implementation so the scripts
# still work on a bare macOS dev machine with neither installed.
run_with_timeout() {
	local secs="$1"
	shift
	if command -v timeout >/dev/null 2>&1; then
		timeout "$secs" "$@"
		return $?
	fi
	if command -v gtimeout >/dev/null 2>&1; then
		gtimeout "$secs" "$@"
		return $?
	fi

	"$@" &
	local pid=$!
	(
		sleep "$secs"
		kill -TERM "$pid" 2>/dev/null
	) &
	local watcher=$!
	local status=0
	if wait "$pid" 2>/dev/null; then
		status=0
	else
		status=$?
	fi
	kill "$watcher" 2>/dev/null
	wait "$watcher" 2>/dev/null
	return "$status"
}

# require_env NAME NAME2 ... — dies with EXIT_USAGE_ERROR listing every
# missing variable (not just the first) so operators fix config in one pass.
require_env() {
	local missing=()
	local name
	for name in "$@"; do
		if [ -z "${!name:-}" ]; then
			missing+=("$name")
		fi
	done
	if [ "${#missing[@]}" -gt 0 ]; then
		echo "missing required environment variable(s): ${missing[*]}" >&2
		return "$EXIT_USAGE_ERROR"
	fi
	return 0
}
