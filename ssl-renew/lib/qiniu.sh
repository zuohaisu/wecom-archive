#!/usr/bin/env bash
#=============================================================================
# lib/qiniu.sh — Qiniu SSL certificate upload / bind / verify.
#
# All request signing and HTTP transport is delegated to qiniu_helper.py,
# which uses the OFFICIAL `qiniu` Python SDK (qiniu.Auth / qiniu.DomainManager)
# — no HMAC signing is implemented here or in the helper. This replaces an
# earlier hand-rolled bash QBox signer that did not match the official
# algorithm (it signed the HTTP method, which QBox never includes, and
# always signed the body regardless of Content-Type, when QBox only signs
# the body for `application/x-www-form-urlencoded` requests — never for the
# `application/json` bodies this module actually sends). See
# https://github.com/zuohaisu/wecom-archive/wiki/TLS-Renewal-Architecture section 6.1 and
# tests/test_qiniu_helper.py (golden parity tests against the official SDK).
#
# Secrets never touch argv: AK/SK flow to the helper via the already-exported
# QINIU_ACCESS_KEY/QINIU_SECRET_KEY environment (inherited by the child
# process, not passed as a flag), and the certificate/private key are read
# by the helper directly from the file paths given in --cert-file/--key-file
# (only the *paths* appear in argv, never the file contents or the derived
# Authorization header). See ssl-renew/README.md "Runtime Secret Safety".
#
# Every function here — including the read-only verifiers — is DRY_RUN-aware:
# when is_dry_run returns true, the Python helper is never invoked and no
# network call (write or read-only probe) is made; a fake but shaped response
# is returned so the caller's pipeline (which expects e.g. a certID string)
# keeps working for planning/logging.
#
# The verification functions report WHY they failed through their exit
# status instead of a plain boolean, so a caller can tell "the API proved
# drift" apart from "the API could not answer" (GH-235):
#
#   verify_certID_on_domain  0 confirmed equal | 1 confirmed mismatch |
#                            2 API unavailable (HTTP 404) | 3 other failure
#                            or unverifiable response
#   verify_live_tls_cert     0 live leaf matches | 1 live leaf differs |
#                            2 probe failed or returned invalid evidence
#
# Only the "confirmed mismatch" status is evidence of drift. Every other
# non-zero status means the binding state is UNKNOWN and must fail closed
# (stop, alert) rather than be treated as drift and rewritten.
#
# Requires lib/common.sh to already be sourced (log, filter_secrets,
# is_dry_run, and canonical warn/die reporting).
#=============================================================================

# shellcheck disable=SC2317  # reachable when sourced a second time
if [ -n "${SSL_RENEW_QINIU_LOADED:-}" ]; then
	return 0 2>/dev/null || exit 0
fi
SSL_RENEW_QINIU_LOADED=1

QINIU_HELPER_PYTHON="${QINIU_HELPER_PYTHON:-python3}"
QINIU_HELPER_SCRIPT="${QINIU_HELPER_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/qiniu_helper.py}"
QINIU_HELPER_TIMEOUT="${QINIU_HELPER_TIMEOUT:-15}"

# ── Internal: run qiniu_helper.py and capture ONLY its stdout (the JSON
#    result line). Anything the helper writes to stderr (it shouldn't,
#    normally) is left inherited so it still reaches the systemd journal
#    for debugging, without being mixed into the value this function
#    returns to its caller.
_qiniu_helper_run() {
	"$QINIU_HELPER_PYTHON" "$QINIU_HELPER_SCRIPT" "$@"
}

# Extracts .error from a helper JSON result, with a safe fallback message
# when the output isn't parseable JSON at all (e.g. python3/qiniu missing).
_qiniu_helper_error_message() {
	local helper_output="$1" helper_status="$2"
	local msg
	msg=$(printf '%s' "$helper_output" | jq -r '.error // empty' 2>/dev/null)
	if [ -n "$msg" ]; then
		echo "$msg"
		return
	fi
	echo "qiniu_helper.py failed (exit=$helper_status) — check that $QINIU_HELPER_PYTHON has the 'qiniu' package installed (see ssl-renew/requirements.txt)"
}

# ── Upload certificate to Qiniu. Prints certID on stdout. ──────────────
deploy_to_qiniu() {
	local domain="$1" cert_dir="$2"
	local fullchain_file="$cert_dir/fullchain.cer"
	local privkey_file="$cert_dir/$domain.key"

	if [ ! -f "$fullchain_file" ]; then
		echo "fullchain.cer not found: $fullchain_file" >&2
		return 1
	fi
	if [ ! -f "$privkey_file" ]; then
		echo "private key not found: $privkey_file" >&2
		return 1
	fi

	if is_dry_run; then
		log_dry "would run: $QINIU_HELPER_PYTHON $QINIU_HELPER_SCRIPT upload --domain $domain --cert-file $fullchain_file --key-file $privkey_file --timeout $QINIU_HELPER_TIMEOUT" >&2
		log_dry "  (file paths only — certificate/key contents are never passed as arguments)" >&2
		echo "DRY-RUN-CERTID"
		return 0
	fi

	local helper_output helper_status
	helper_output=$(_qiniu_helper_run upload \
		--domain "$domain" --cert-file "$fullchain_file" --key-file "$privkey_file" \
		--timeout "$QINIU_HELPER_TIMEOUT")
	helper_status=$?

	if [ "$helper_status" -ne 0 ]; then
		_qiniu_helper_error_message "$helper_output" "$helper_status" >&2
		return 1
	fi

	local cert_id
	cert_id=$(printf '%s' "$helper_output" | jq -r '.certID // empty' 2>/dev/null)
	if [ -z "$cert_id" ]; then
		echo "Qiniu upload failed — helper returned no certID" >&2
		return 1
	fi
	echo "$cert_id"
}

# ── Bind an uploaded certID to a CDN domain's HTTPS config. ─────────────
bind_cert_to_domain() {
	local domain="$1" certID="$2"

	if is_dry_run; then
		log_dry "would run: $QINIU_HELPER_PYTHON $QINIU_HELPER_SCRIPT bind --domain $domain --cert-id $certID --timeout $QINIU_HELPER_TIMEOUT" >&2
		return 0
	fi

	local helper_output helper_status
	helper_output=$(_qiniu_helper_run bind \
		--domain "$domain" --cert-id "$certID" --timeout "$QINIU_HELPER_TIMEOUT")
	helper_status=$?

	if [ "$helper_status" -ne 0 ]; then
		_qiniu_helper_error_message "$helper_output" "$helper_status" >&2
		return 1
	fi
	return 0
}

# ── Verify the certID currently bound to a domain matches expected. ─────
# Exit status contract (see the file header):
#   0 = the API confirms the expected certID is bound
#   1 = the API confirms a DIFFERENT certID is bound (real drift)
#   2 = the verification API is unavailable for this domain (HTTP 404 —
#       the production CDN-domain behaviour behind GH-235)
#   3 = any other API failure, or a response that cannot be interpreted
# Callers must never treat 2/3 as proof of drift.
_qiniu_helper_http_status() {
	local helper_output="$1"
	printf '%s' "$helper_output" | jq -r '.http_status // empty' 2>/dev/null
}

verify_certID_on_domain() {
	local domain="$1" expected="$2"

	if is_dry_run; then
		log_dry "would run: $QINIU_HELPER_PYTHON $QINIU_HELPER_SCRIPT verify --domain $domain --expected-cert-id $expected --timeout $QINIU_HELPER_TIMEOUT" >&2
		return 0
	fi

	local helper_output helper_status
	helper_output=$(_qiniu_helper_run verify \
		--domain "$domain" --expected-cert-id "$expected" --timeout "$QINIU_HELPER_TIMEOUT")
	helper_status=$?

	if [ "$helper_status" -ne 0 ]; then
		_qiniu_helper_error_message "$helper_output" "$helper_status" >&2
		if [ "$(_qiniu_helper_http_status "$helper_output")" = "404" ]; then
			return 2
		fi
		return 3
	fi

	local matches actual
	matches=$(printf '%s' "$helper_output" | jq -r '.matches // false' 2>/dev/null)
	actual=$(printf '%s' "$helper_output" | jq -r '.certId // empty' 2>/dev/null)
	if [ "$matches" != "true" ]; then
		if [ "$matches" = "false" ] && [ -n "$actual" ]; then
			echo "certID mismatch — expected=$expected, actual=$actual" >&2
			return 1
		fi
		echo "Qiniu verify returned an invalid response — cannot confirm the bound certID" >&2
		return 3
	fi
	echo "API certID=$actual confirmed" >&2
	return 0
}

# ── Verify the live SNI TLS leaf against the local fullchain leaf. ───────
# Read-only independent evidence for when the Qiniu binding API cannot
# answer (GH-235): connects to <domain>:443 with SNI, validates the chain
# and hostname, and compares the served leaf certificate against the first
# certificate in <cert_file> by DER SHA-256. Needs no Qiniu credentials and
# returns the same status contract as the helper:
#   0 = the served leaf IS the local leaf
#   1 = the served leaf is a DIFFERENT certificate (real drift)
#   2 = the probe failed, or the helper returned an uninterpretable result
# The helper computes both hashes from leaf-certificate DER; this is
# deliberately NOT comparable to renew-wildcard.sh's fullchain-file
# fingerprint.
verify_live_tls_cert() {
	local domain="$1" cert_file="$2"
	local helper_output helper_status ok matches local_sha256 remote_sha256

	if is_dry_run; then
		log_dry "would run: $QINIU_HELPER_PYTHON $QINIU_HELPER_SCRIPT tls-verify --domain $domain --cert-file $cert_file --timeout $QINIU_HELPER_TIMEOUT" >&2
		log_dry "  (read-only SNI probe — leaf-certificate DER comparison, no credentials, no writes; assumed matching for planning only)" >&2
		return 0
	fi

	helper_output=$(_qiniu_helper_run tls-verify \
		--domain "$domain" --cert-file "$cert_file" \
		--timeout "$QINIU_HELPER_TIMEOUT")
	helper_status=$?
	if [ "$helper_status" -ne 0 ]; then
		_qiniu_helper_error_message "$helper_output" "$helper_status" >&2
		return 2
	fi

	ok=$(printf '%s' "$helper_output" | jq -r '.ok == true' 2>/dev/null)
	matches=$(printf '%s' "$helper_output" | jq -r 'if .matches == true then "true" elif .matches == false then "false" else empty end' 2>/dev/null)
	local_sha256=$(printf '%s' "$helper_output" | jq -r '.local_sha256 // empty' 2>/dev/null)
	remote_sha256=$(printf '%s' "$helper_output" | jq -r '.remote_sha256 // empty' 2>/dev/null)
	if [ "$ok" != "true" ] || [ "${#local_sha256}" -ne 64 ] ||
		[ "${#remote_sha256}" -ne 64 ] ||
		[[ "$local_sha256$remote_sha256" == *[!0-9a-f]* ]]; then
		echo "TLS probe returned an invalid leaf-certificate fingerprint result" >&2
		return 2
	fi

	if [ "$matches" = "true" ] && [ "$local_sha256" = "$remote_sha256" ]; then
		return 0
	fi
	if [ "$matches" = "false" ] && [ "$local_sha256" != "$remote_sha256" ]; then
		echo "live TLS leaf does not match local leaf (local_sha256=$local_sha256, live_sha256=$remote_sha256)" >&2
		return 1
	fi
	echo "TLS probe returned inconsistent leaf-certificate fingerprints" >&2
	return 2
}
