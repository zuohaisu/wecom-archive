#!/usr/bin/env bash
#=============================================================================
# Mock `curl` used to test lib/qiniu.sh and notify.sh without any real
# network access. Copied into a per-test PATH directory by
# tests/test_helper/common.bash::use_mock_curl.
#
# Controlled entirely via env vars:
#   MOCK_CURL_MODE    success | http_error | timeout | connect_fail
#   MOCK_CURL_STATUS  HTTP status code to report (for success/http_error)
#   MOCK_CURL_BODY    response body to write to the -o output file
#   MOCK_CURL_LOG     if set, the full argv is appended here (one line),
#                     letting tests assert on request construction.
#=============================================================================

out_file=""
args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
	if [ "${args[$i]}" = "-o" ]; then
		i=$((i + 1))
		out_file="${args[$i]}"
	fi
	i=$((i + 1))
done

if [ -n "${MOCK_CURL_LOG:-}" ]; then
	printf '%s\n' "$*" >>"$MOCK_CURL_LOG"
fi

case "${MOCK_CURL_MODE:-success}" in
timeout)
	exit 28 # CURLE_OPERATION_TIMEDOUT
	;;
connect_fail)
	exit 7 # CURLE_COULDNT_CONNECT
	;;
*)
	if [ -n "$out_file" ]; then
		# NOTE: don't write this as `${MOCK_CURL_BODY:-{}}` — bash's
		# parameter-expansion parser doesn't brace-match literal `{`/`}`
		# in the default word, so it ends the substitution at the first
		# `}` and leaks a stray literal `}` right after (e.g. an empty
		# MOCK_CURL_BODY would produce "{}" via the default, then the
		# variable being *set* to "{}" instead produces "{}}").
		body="${MOCK_CURL_BODY:-}"
		[ -z "$body" ] && body='{}'
		printf '%s' "$body" >"$out_file"
	fi
	printf '%s' "${MOCK_CURL_STATUS:-200}"
	exit 0
	;;
esac
