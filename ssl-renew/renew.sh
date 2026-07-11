#!/usr/bin/env bash
#=============================================================================
# renew.sh — Automated SSL certificate renewal + Qiniu CDN deployment
#
# Usage:
#   renew.sh [<domain>] [--dry-run] [--staging]
#
# <domain> may be omitted if DOMAIN is set in the environment (e.g. via
# systemd EnvironmentFile= or CONFIG_FILE=). If both are given, they must
# match (this catches instance-name / config-file mismatches early).
#
# Modes (see README.md for the full comparison table):
#   production  (default)      real ACME CA, real Qiniu upload/bind
#   --staging / ACME_STAGING=1  Let's Encrypt staging CA; Qiniu deployment
#                               is skipped entirely (staging certs are not
#                               production-trusted, so nothing to deploy)
#   --dry-run / DRY_RUN=1        no network calls that could change state;
#                               prints the plan and exits
#
# Architecture:  docs/ssl-renewal/ARCHITECTURE.md
#
# Requires:
#   - acme.sh (installed in $HOME/.acme.sh/)
#   - openssl, curl, jq, sha256sum
#   - Environment: QINIU_ACCESS_KEY / QINIU_SECRET_KEY
#     (or SAVED_QINIU_AK / SAVED_QINIU_SK from acme.sh account.conf)
#   - acme.sh account.conf must contain DP_Id / DP_Key (DNSPod API token)
#
# Exit codes:
#   0  success (including a successful dry-run plan)
#   1  generic runtime failure (renew/upload/bind/verify error)
#   2  usage / configuration error (bad args, missing required env)
#=============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/qiniu.sh
source "$SCRIPT_DIR/lib/qiniu.sh"

usage() {
	cat >&2 <<'EOF'
Usage: renew.sh [<domain>] [--dry-run] [--staging]

<domain> may be omitted if DOMAIN is set in the environment.
EOF
}

# ── Argument parsing ─────────────────────────────────────────────────────
ARG_DOMAIN=""
while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run)
		export DRY_RUN=1
		shift
		;;
	--staging)
		export ACME_STAGING=1
		shift
		;;
	-h | --help)
		usage
		exit "$EXIT_OK"
		;;
	-*)
		echo "unknown option: $1" >&2
		usage
		exit "$EXIT_USAGE_ERROR"
		;;
	*)
		if [ -n "$ARG_DOMAIN" ]; then
			echo "unexpected extra argument: $1" >&2
			usage
			exit "$EXIT_USAGE_ERROR"
		fi
		ARG_DOMAIN="$1"
		shift
		;;
	esac
done

# ── Config loading ───────────────────────────────────────────────────────
if ! load_config; then
	exit "$EXIT_USAGE_ERROR"
fi

if [ -n "$ARG_DOMAIN" ]; then
	if [ -n "${DOMAIN:-}" ] && [ "$DOMAIN" != "$ARG_DOMAIN" ]; then
		echo "domain argument ($ARG_DOMAIN) does not match DOMAIN env var ($DOMAIN)" >&2
		exit "$EXIT_USAGE_ERROR"
	fi
	DOMAIN="$ARG_DOMAIN"
fi

if [ -z "${DOMAIN:-}" ]; then
	echo "no domain given (pass as argument or set DOMAIN)" >&2
	usage
	exit "$EXIT_USAGE_ERROR"
fi

if ! validate_domain "$DOMAIN"; then
	exit "$EXIT_USAGE_ERROR"
fi

if ! is_dry_run; then
	have_direct_creds=0
	have_saved_creds=0
	[ -n "${QINIU_ACCESS_KEY:-}" ] && [ -n "${QINIU_SECRET_KEY:-}" ] && have_direct_creds=1
	[ -n "${SAVED_QINIU_AK:-}" ] && [ -n "${SAVED_QINIU_SK:-}" ] && have_saved_creds=1
	if [ "$have_direct_creds" -ne 1 ] && [ "$have_saved_creds" -ne 1 ]; then
		echo "missing required Qiniu credentials: set QINIU_ACCESS_KEY+QINIU_SECRET_KEY or SAVED_QINIU_AK+SAVED_QINIU_SK" >&2
		exit "$EXIT_USAGE_ERROR"
	fi
fi

# ── Setup ────────────────────────────────────────────────────────────────
CERT_DIR="${CERT_DIR:-$HOME/.acme.sh/$DOMAIN}"
STATE_DIR="${STATE_DIR:-$CERT_DIR}"
FP_FILE="$STATE_DIR/.deployed_fp"
TLS_OK_FILE="$STATE_DIR/.tls_verified"
MISMATCH_FILE="$STATE_DIR/.tls_mismatch_days"
LOCK_FILE="$STATE_DIR/.renew.lock"
export LOG_TAG="[$DOMAIN]"

ACME_SH="${ACME_SH:-$HOME/.acme.sh/acme.sh}"
DNS_PROVIDER="${DNS_PROVIDER:-dns_dp}"
TLS_MIN_DAYS="${TLS_MIN_DAYS:-15}"
TLS_VERIFY_TIMEOUT="${TLS_VERIFY_TIMEOUT:-10}"
TLS_VERIFY_RETRIES="${TLS_VERIFY_RETRIES:-5}"
TLS_MISMATCH_DAYS_LIMIT="${TLS_MISMATCH_DAYS_LIMIT:-7}"

CURRENT_STAGE="init"

# ── Notification-wrapped logging ────────────────────────────────────────
warn() {
	log "[WARN]  $*"
	CURRENT_STAGE="$CURRENT_STAGE" "$SCRIPT_DIR/notify.sh" WARN "$DOMAIN" "$*" || true
}
die() {
	log "[ERROR] $*"
	CURRENT_STAGE="$CURRENT_STAGE" "$SCRIPT_DIR/notify.sh" ERROR "$DOMAIN" "$*" || true
	exit "$EXIT_GENERIC_FAILURE"
}

mkdir -p "$STATE_DIR" 2>/dev/null || true

# ── Idempotency guard: refuse to run two renewals for the same domain
#    concurrently (e.g. a slow run still active when the timer fires again).
if command -v flock >/dev/null 2>&1; then
	exec 9>"$LOCK_FILE"
	if ! flock -n 9; then
		log "[INFO] another renew.sh run is already in progress for $DOMAIN — exiting"
		exit "$EXIT_OK"
	fi
else
	# flock is not available (e.g. macOS dev machine). Best-effort PID lock.
	if [ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE" 2>/dev/null)" 2>/dev/null; then
		log "[INFO] another renew.sh run is already in progress for $DOMAIN (pid=$(cat "$LOCK_FILE")) — exiting"
		exit "$EXIT_OK"
	fi
	echo $$ >"$LOCK_FILE"
	trap 'rm -f "$LOCK_FILE"' EXIT
fi

# ═════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════

if is_dry_run; then
	run_mode="dry-run"
elif is_staging; then
	run_mode="staging"
else
	run_mode="production"
fi
log "[INFO] ====== SSL renewal started (mode=$run_mode) ======"

if is_dry_run; then
	log_dry "would run: $ACME_SH --renew --dns $DNS_PROVIDER -d $DOMAIN $(is_staging && echo '--staging')"
	log_dry "cert dir: $CERT_DIR"
	log_dry "state dir: $STATE_DIR"
	if [ -f "$CERT_DIR/fullchain.cer" ]; then
		local_fp_preview=$(sha256sum "$CERT_DIR/fullchain.cer" 2>/dev/null | awk '{print $1}')
		log_dry "current local fullchain.cer sha256: ${local_fp_preview:-<unreadable>}"
	else
		log_dry "no existing local certificate at $CERT_DIR/fullchain.cer"
	fi
	deployed_fp_preview=""
	[ -f "$FP_FILE" ] && deployed_fp_preview=$(cat "$FP_FILE")
	log_dry "recorded deployed_fp: ${deployed_fp_preview:-<none>}"
	log_dry "tls_verified flag present: $([ -f "$TLS_OK_FILE" ] && echo yes || echo no)"
	log_dry "would upload renewed certificate to Qiniu (POST /sslcert) if fingerprint changed"
	log_dry "would bind uploaded certID to $DOMAIN (PUT /domain/$DOMAIN/httpsconf)"
	log_dry "would verify certID via API and then verify_https.sh (min-days=$TLS_MIN_DAYS)"
	log "[OK]  dry-run plan complete — no external state was modified"
	exit "$EXIT_OK"
fi

# ── Step 1: acme.sh --renew ─────────────────────────────────────────────

CURRENT_STAGE="acme_renew"
staging_flag=""
is_staging && staging_flag="--staging"

log "[INFO] Running $ACME_SH --renew${staging_flag:+ $staging_flag}"
set +e
"$ACME_SH" --renew --dns "$DNS_PROVIDER" -d "$DOMAIN" $staging_flag 2>&1 | filter_secrets
acme_exit=${PIPESTATUS[0]}
set -e

if [ "$acme_exit" -eq 0 ]; then
	log "[INFO] acme.sh --renew completed"
elif [ "$acme_exit" -eq 2 ]; then
	log "[INFO] acme.sh --renew skipped (cert not due yet, exit=$acme_exit)"
else
	die "acme.sh --renew failed (exit=$acme_exit)"
fi

if is_staging; then
	log "[INFO] staging mode: skipping Qiniu production deployment for $DOMAIN"
	log "[INFO] staging certificate is issued locally only — not trusted by browsers, not deployed"
	log "[OK]  staging renewal complete (fp=n/a, no deployment)"
	exit "$EXIT_OK"
fi

# ── Step 2: local fingerprint ───────────────────────────────────────────

CURRENT_STAGE="local_fingerprint"
if [ ! -f "$CERT_DIR/fullchain.cer" ]; then
	die "fullchain.cer not found: $CERT_DIR/fullchain.cer"
fi

local_fp=$(sha256sum "$CERT_DIR/fullchain.cer" | awk '{print $1}')
log "[INFO] local_fp=$local_fp"

# ── Step 3: decision ────────────────────────────────────────────────────

deployed_fp=""
[ -f "$FP_FILE" ] && deployed_fp=$(cat "$FP_FILE")

if [ "$local_fp" = "$deployed_fp" ] && [ -n "$deployed_fp" ]; then

	# ── Cert already deployed. Check if TLS was verified ─────────────────
	if [ -f "$TLS_OK_FILE" ]; then
		log "[INFO] fully deployed + TLS verified — nothing to do, exit"
		exit "$EXIT_OK"
	fi

	# ── TLS not yet verified from previous run ───────────────────────────
	CURRENT_STAGE="tls_verify"
	log "[INFO] cert deployed but TLS not yet verified — re-checking"

	mismatch_days=$(cat "$MISMATCH_FILE" 2>/dev/null || echo 0)

	if "$SCRIPT_DIR/verify_https.sh" "$DOMAIN" --cert-file "$CERT_DIR/fullchain.cer" \
		--min-days "$TLS_MIN_DAYS" --timeout "$TLS_VERIFY_TIMEOUT" --retries "$TLS_VERIFY_RETRIES"; then
		log "[INFO] TLS now verified after ${mismatch_days} day(s)"
		touch "$TLS_OK_FILE"
		rm -f "$MISMATCH_FILE"
		exit "$EXIT_OK"
	fi

	# Still mismatched — increment counter
	mismatch_days=$((mismatch_days + 1))
	echo "$mismatch_days" >"$MISMATCH_FILE"

	if [ "$mismatch_days" -ge "$TLS_MISMATCH_DAYS_LIMIT" ]; then
		die "TLS verification still failing after ${mismatch_days} days — CDN may not be serving correct certificate — manual investigation required"
	fi

	warn "TLS verification still failing (day ${mismatch_days} of mismatch)"
	warn "certID verified via API — CDN propagation may be delayed"
	warn "will re-check on next run"
	exit "$EXIT_OK"
fi

# ── Step 4: full deployment path (local_fp != deployed_fp) ──────────────

log "[INFO] local_fp != deployed_fp (local=$local_fp, deployed=${deployed_fp:-none})"
log "[INFO] proceeding to deploy ..."

CURRENT_STAGE="qiniu_upload"
upload_output=$(deploy_to_qiniu "$DOMAIN" "$CERT_DIR" 2>&1)
upload_status=$?
if [ "$upload_status" -ne 0 ]; then
	die "$upload_output"
fi
certID="$upload_output"
log "[INFO] certificate uploaded to Qiniu — certID=$certID"

CURRENT_STAGE="qiniu_bind"
if ! bind_err=$(bind_cert_to_domain "$DOMAIN" "$certID" 2>&1); then
	die "$bind_err"
fi
log "[INFO] certID=$certID bound to $DOMAIN"

# ── Step 5a: API verification (MUST pass) ───────────────────────────────

CURRENT_STAGE="qiniu_api_verify"
if ! verify_msg=$(verify_certID_on_domain "$DOMAIN" "$certID" 2>&1); then
	die "$verify_msg"
fi
log "[INFO] $verify_msg"

# API verified — record deployment
echo "$local_fp" >"$FP_FILE"
rm -f "$TLS_OK_FILE" # new cert, old TLS flag invalid
rm -f "$MISMATCH_FILE"

# ── Step 5b: HTTPS verification (MAY fail due to CDN propagation) ───────

CURRENT_STAGE="tls_verify"
if "$SCRIPT_DIR/verify_https.sh" "$DOMAIN" --cert-file "$CERT_DIR/fullchain.cer" \
	--min-days "$TLS_MIN_DAYS" --timeout "$TLS_VERIFY_TIMEOUT" --retries "$TLS_VERIFY_RETRIES"; then
	log "[INFO] HTTPS verification passed"
	touch "$TLS_OK_FILE"
else
	warn "HTTPS verification not yet passing — CDN propagation in progress"
	warn "certificate is uploaded & bound (certID=$certID confirmed via API)"
	warn "TLS will be re-checked on next timer run"
	echo 1 >"$MISMATCH_FILE"
fi

# ── Done ─────────────────────────────────────────────────────────────────

CURRENT_STAGE="done"
log "[OK]  deployment complete (fp=$local_fp)"
exit "$EXIT_OK"
