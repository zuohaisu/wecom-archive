#!/usr/bin/env bash
#=============================================================================
# verify_https.sh — Standalone HTTPS/TLS verification for a domain.
#
# Usage:
#   verify_https.sh <domain> [options]
#
# Options:
#   --host <ip_or_hostname>     Connect to this address instead of <domain>
#                                (SNI/Host stay as <domain>). Used to point
#                                at a mock server in tests.
#   --port <port>                Default: 443
#   --cert-file <path>           Compare remote leaf cert fingerprint
#                                against this local cert file (sha256).
#   --fingerprint <sha256hex>    Compare remote leaf cert fingerprint
#                                against an explicit expected value.
#   --min-days <n>                Minimum remaining validity in days.
#                                 Default: 15
#   --timeout <seconds>           Per-attempt network timeout. Default: 10
#   --retries <n>                  Retry attempts on transient failure.
#                                  Default: 3
#   --retry-delay <seconds>        Delay between retries. Default: 3
#   --skip-dns-check               Skip standalone DNS resolution check
#                                  (useful when --host points at a mock IP
#                                  for a domain that doesn't really resolve).
#   --skip-http-check               Skip the HTTP-level reachability check.
#   --insecure-http-check           Do not validate the TLS chain during the
#                                   HTTP-level check (test/mock use only).
#
# Exit codes:
#   0   all checks passed
#   2   usage / argument error
#   10  DNS resolution failed
#   11  TCP/TLS connection failed (refused / unreachable)
#   12  TLS handshake did not yield a certificate (SNI/handshake failure)
#   13  certificate domain (CN/SAN) does not match
#   14  certificate chain verification failed
#   15  certificate has expired
#   16  certificate remaining validity below --min-days threshold
#   17  TLS 1.2 handshake failed
#   18  HTTP response could not be obtained (excludes 401/403, which count
#       as success — they prove TLS + routing work)
#   19  certificate fingerprint mismatch
#   20  timeout exceeded after all retries
#=============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

EXIT_DNS_FAILED=10
EXIT_CONNECT_FAILED=11
EXIT_NO_CERTIFICATE=12
EXIT_DOMAIN_MISMATCH=13
EXIT_CHAIN_INVALID=14
EXIT_EXPIRED=15
EXIT_EXPIRING_SOON=16
EXIT_TLS12_FAILED=17
EXIT_HTTP_UNREACHABLE=18
EXIT_FINGERPRINT_MISMATCH=19
EXIT_TIMEOUT=20

usage() {
	cat >&2 <<'EOF'
Usage: verify_https.sh <domain> [options]
See script header for full option list and exit code table.
EOF
}

DOMAIN=""
CONNECT_HOST=""
PORT=443
CERT_FILE=""
EXPECT_FINGERPRINT=""
MIN_DAYS=15
TIMEOUT=10
RETRIES=3
RETRY_DELAY=3
SKIP_DNS=""
SKIP_HTTP=""
INSECURE_HTTP=""

if [ $# -lt 1 ]; then
	usage
	exit "$EXIT_USAGE_ERROR"
fi
DOMAIN="$1"
shift

while [ $# -gt 0 ]; do
	case "$1" in
	--host)
		CONNECT_HOST="$2"
		shift 2
		;;
	--port)
		PORT="$2"
		shift 2
		;;
	--cert-file)
		CERT_FILE="$2"
		shift 2
		;;
	--fingerprint)
		EXPECT_FINGERPRINT="$2"
		shift 2
		;;
	--min-days)
		MIN_DAYS="$2"
		shift 2
		;;
	--timeout)
		TIMEOUT="$2"
		shift 2
		;;
	--retries)
		RETRIES="$2"
		shift 2
		;;
	--retry-delay)
		RETRY_DELAY="$2"
		shift 2
		;;
	--skip-dns-check)
		SKIP_DNS=1
		shift
		;;
	--skip-http-check)
		SKIP_HTTP=1
		shift
		;;
	--insecure-http-check)
		INSECURE_HTTP=1
		shift
		;;
	-h | --help)
		usage
		exit "$EXIT_OK"
		;;
	*)
		echo "unknown option: $1" >&2
		usage
		exit "$EXIT_USAGE_ERROR"
		;;
	esac
done

if ! validate_domain "$DOMAIN"; then
	exit "$EXIT_USAGE_ERROR"
fi

CONNECT_TARGET="${CONNECT_HOST:-$DOMAIN}"
export LOG_TAG="[$DOMAIN]"

if [ -n "$CERT_FILE" ] && [ -n "$EXPECT_FINGERPRINT" ]; then
	echo "--cert-file and --fingerprint are mutually exclusive" >&2
	exit "$EXIT_USAGE_ERROR"
fi
if [ -n "$CERT_FILE" ]; then
	if [ ! -f "$CERT_FILE" ]; then
		echo "cert file not found: $CERT_FILE" >&2
		exit "$EXIT_USAGE_ERROR"
	fi
	EXPECT_FINGERPRINT=$(openssl x509 -in "$CERT_FILE" -fingerprint -sha256 -noout | sed 's/.*=//')
fi

# ── DNS resolution check ─────────────────────────────────────────────────
resolve_domain() {
	local host="$1"
	if command -v getent >/dev/null 2>&1; then
		getent hosts "$host" >/dev/null 2>&1
		return $?
	fi
	if command -v python3 >/dev/null 2>&1; then
		python3 -c "
import socket, sys
socket.setdefaulttimeout(5)
try:
    socket.getaddrinfo(sys.argv[1], None)
except OSError:
    sys.exit(1)
" "$host" >/dev/null 2>&1
		return $?
	fi
	if command -v host >/dev/null 2>&1; then
		host "$host" >/dev/null 2>&1
		return $?
	fi
	if command -v dig >/dev/null 2>&1; then
		[ -n "$(dig +short "$host" 2>/dev/null)" ]
		return $?
	fi
	# No resolver tool available — cannot determine, do not block.
	return 0
}

if [ -z "$SKIP_DNS" ]; then
	log "[INFO] checking DNS resolution for $DOMAIN"
	if ! resolve_domain "$DOMAIN"; then
		log "[FAIL] DNS resolution failed for $DOMAIN"
		exit "$EXIT_DNS_FAILED"
	fi
	log "[OK]   DNS resolves"
fi

# ── TLS connect + certificate retrieval (with retry/backoff) ────────────
CERT_PEM_FILE=$(mktemp "${TMPDIR:-/tmp}/verify-https-cert.XXXXXX")
ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/verify-https-err.XXXXXX")
trap 'rm -f "$CERT_PEM_FILE" "$ERR_FILE" "$CERT_PEM_FILE.raw"' EXIT

FETCH_LAST_STATUS="timeout"

# Reports its result via $FETCH_LAST_STATUS + return code, NOT via stdout —
# log() also writes to stdout, so an echo-based return value here would get
# silently corrupted by interleaved log lines when callers do `x=$(fetch_certificate)`.
fetch_certificate() {
	local attempt=1
	local last_status="timeout"
	while [ "$attempt" -le "$RETRIES" ]; do
		log "[INFO] TLS connect attempt $attempt/$RETRIES to $CONNECT_TARGET:$PORT (SNI=$DOMAIN, timeout=${TIMEOUT}s)"

		: >"$CERT_PEM_FILE"
		: >"$ERR_FILE"
		run_with_timeout "$TIMEOUT" bash -c \
			"echo | openssl s_client -connect '$CONNECT_TARGET:$PORT' -servername '$DOMAIN' 2>'$ERR_FILE'" \
			>"$CERT_PEM_FILE.raw" 2>/dev/null || true
		local err_output=""
		err_output=$(cat "$ERR_FILE" 2>/dev/null)

		if [ -s "$CERT_PEM_FILE.raw" ]; then
			sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' "$CERT_PEM_FILE.raw" >"$CERT_PEM_FILE"
		fi
		rm -f "$CERT_PEM_FILE.raw"

		if [ -s "$CERT_PEM_FILE" ]; then
			return 0
		fi

		if echo "$err_output" | grep -qiE 'connection refused|no route to host|network is unreachable'; then
			last_status="refused"
		elif [ -z "$err_output" ]; then
			last_status="timeout"
		else
			last_status="handshake"
		fi

		log "[WARN] no certificate retrieved (attempt $attempt, reason=$last_status)"
		attempt=$((attempt + 1))
		[ "$attempt" -le "$RETRIES" ] && sleep "$RETRY_DELAY"
	done
	FETCH_LAST_STATUS="$last_status"
	return 1
}

if ! fetch_certificate; then
	case "$FETCH_LAST_STATUS" in
	refused)
		log "[FAIL] TLS connection refused/unreachable to $CONNECT_TARGET:$PORT"
		exit "$EXIT_CONNECT_FAILED"
		;;
	timeout)
		log "[FAIL] TLS connection timed out after $RETRIES attempts"
		exit "$EXIT_TIMEOUT"
		;;
	*)
		log "[FAIL] TLS handshake did not return a certificate (SNI=$DOMAIN)"
		exit "$EXIT_NO_CERTIFICATE"
		;;
	esac
fi
log "[OK]   certificate retrieved via TLS"

# ── Certificate domain match (CN or SAN) ─────────────────────────────────
if ! openssl x509 -in "$CERT_PEM_FILE" -noout -checkend 0 >/dev/null 2>&1; then
	end_date=$(openssl x509 -in "$CERT_PEM_FILE" -noout -enddate 2>/dev/null | sed 's/notAfter=//')
	log "[FAIL] certificate has expired (notAfter=$end_date)"
	exit "$EXIT_EXPIRED"
fi

subject_and_san=$(openssl x509 -in "$CERT_PEM_FILE" -noout -text 2>/dev/null)
domain_matches=0
if echo "$subject_and_san" | grep -qE "DNS:${DOMAIN//./\\.}(,|$| )"; then
	domain_matches=1
elif openssl x509 -in "$CERT_PEM_FILE" -noout -subject 2>/dev/null | grep -qE "CN\s*=\s*${DOMAIN//./\\.}(,|$)"; then
	domain_matches=1
fi
if [ "$domain_matches" -ne 1 ]; then
	log "[FAIL] certificate does not cover domain $DOMAIN"
	exit "$EXIT_DOMAIN_MISMATCH"
fi
log "[OK]   certificate covers $DOMAIN"

# ── Certificate chain verification ───────────────────────────────────────
if [ -n "${VERIFY_HTTPS_CA_BUNDLE:-}" ]; then
	if ! openssl verify -CAfile "$VERIFY_HTTPS_CA_BUNDLE" "$CERT_PEM_FILE" >/dev/null 2>&1; then
		log "[FAIL] certificate chain verification failed against $VERIFY_HTTPS_CA_BUNDLE"
		exit "$EXIT_CHAIN_INVALID"
	fi
	log "[OK]   certificate chain verified against $VERIFY_HTTPS_CA_BUNDLE"
elif [ -z "${VERIFY_HTTPS_SKIP_CHAIN:-}" ]; then
	if ! openssl verify "$CERT_PEM_FILE" >/dev/null 2>&1; then
		log "[FAIL] certificate chain verification failed against system trust store"
		exit "$EXIT_CHAIN_INVALID"
	fi
	log "[OK]   certificate chain verified against system trust store"
fi

# ── Remaining validity threshold ─────────────────────────────────────────
min_seconds=$((MIN_DAYS * 86400))
if ! openssl x509 -in "$CERT_PEM_FILE" -noout -checkend "$min_seconds" >/dev/null 2>&1; then
	end_date=$(openssl x509 -in "$CERT_PEM_FILE" -noout -enddate 2>/dev/null | sed 's/notAfter=//')
	log "[FAIL] certificate expires within ${MIN_DAYS} day(s) threshold (notAfter=$end_date)"
	exit "$EXIT_EXPIRING_SOON"
fi
log "[OK]   certificate valid for at least ${MIN_DAYS} more day(s)"

# ── Fingerprint check (optional) ──────────────────────────────────────────
if [ -n "$EXPECT_FINGERPRINT" ]; then
	actual_fp=$(openssl x509 -in "$CERT_PEM_FILE" -fingerprint -sha256 -noout | sed 's/.*=//')
	if [ "$actual_fp" != "$EXPECT_FINGERPRINT" ]; then
		log "[FAIL] fingerprint mismatch (expected=$EXPECT_FINGERPRINT actual=$actual_fp)"
		exit "$EXIT_FINGERPRINT_MISMATCH"
	fi
	log "[OK]   fingerprint matches: $actual_fp"
fi

# ── TLS 1.2 availability ──────────────────────────────────────────────────
if ! run_with_timeout "$TIMEOUT" bash -c \
	"echo | openssl s_client -connect '$CONNECT_TARGET:$PORT' -servername '$DOMAIN' -tls1_2 2>/dev/null" |
	grep -q "BEGIN CERTIFICATE"; then
	log "[FAIL] TLS 1.2 handshake failed"
	exit "$EXIT_TLS12_FAILED"
fi
log "[OK]   TLS 1.2 available"

# ── HTTP-level reachability (401/403 count as success) ────────────────────
if [ -z "$SKIP_HTTP" ]; then
	curl_args=(-s -o /dev/null -w '%{http_code}'
		--connect-timeout "$TIMEOUT" --max-time "$((TIMEOUT * 2))")
	[ -n "$INSECURE_HTTP" ] && curl_args+=(-k)
	if [ -n "$CONNECT_HOST" ]; then
		curl_args+=(--resolve "${DOMAIN}:${PORT}:${CONNECT_HOST}")
	fi
	curl_args+=("https://${DOMAIN}:${PORT}/")

	# curl prints "000" via -w on a failed handshake/connect *and* exits
	# non-zero; don't also `|| echo 000` or the two concatenate into "000000".
	http_code=$(curl "${curl_args[@]}" 2>/dev/null)
	[ -z "$http_code" ] && http_code="000"
	case "$http_code" in
	2* | 3* | 401 | 403)
		log "[OK]   HTTP reachable (status=$http_code)"
		;;
	*)
		log "[FAIL] HTTP status unusable: $http_code"
		exit "$EXIT_HTTP_UNREACHABLE"
		;;
	esac
fi

log "[OK]   all HTTPS checks passed for $DOMAIN"
exit "$EXIT_OK"
