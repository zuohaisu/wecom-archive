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
# All state-changing functions here are DRY_RUN-aware: when is_dry_run
# returns true, the Python helper is never invoked and no network call is
# made; a fake but shaped response is returned so the caller's pipeline
# (which expects e.g. a certID string) keeps working for planning/logging.
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
		return 1
	fi

	local matches actual
	matches=$(printf '%s' "$helper_output" | jq -r '.matches // false' 2>/dev/null)
	actual=$(printf '%s' "$helper_output" | jq -r '.certId // empty' 2>/dev/null)
	if [ "$matches" != "true" ]; then
		echo "certID mismatch — expected=$expected, actual=$actual" >&2
		return 1
	fi
	echo "API certID=$actual confirmed" >&2
	return 0
}
