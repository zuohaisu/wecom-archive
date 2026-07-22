#!/usr/bin/env bash
#=============================================================================
# Mock `curl` used by deploy_server.bats to test the readiness-gate retry
# loop (and the automatic rollback it triggers) without any real network
# access or a running app.
#
# Branches on whether the requested URL contains "internal" or "public"
# (tests point INTERNAL_HEALTH / PUBLIC_HEALTH at distinguishable dummy
# URLs so this match is unambiguous) and tracks a per-label call counter
# under MOCK_CURL_STATE_DIR so a mode can succeed only after N calls --
# this is what lets one test model "the forward-path gate exhausts its
# retries and fails, then the SAME url succeeds on the rollback's first
# retry" (RND-227 Scenario 4) with a single counter.
#
# Controlled via:
#   MOCK_CURL_STATE_DIR        required — counter files live here
#   MOCK_CURL_INTERNAL_MODE    always_ok | always_fail | ok_after:N | fail_after:N
#   MOCK_CURL_PUBLIC_MODE      always_ok | always_fail | ok_after:N | fail_after:N
#     ok_after:N   fails while count<N, succeeds once count>=N (models a
#                  retry loop that eventually succeeds)
#     fail_after:N succeeds while count<=N, fails once count>N (models a
#                  gate that passes, then a LATER unrelated call fails --
#                  e.g. RND-227 QA-03's "extra print curl after the
#                  retry loop already succeeded")
#   MOCK_CURL_LOG               if set, the full argv is appended here
#=============================================================================

if [ -n "${MOCK_CURL_LOG:-}" ]; then
	printf 'curl %s\n' "$*" >>"$MOCK_CURL_LOG"
fi

url="${*: -1}"
case "$url" in
*internal*) label="internal" ;;
*public*) label="public" ;;
*) label="other" ;;
esac

case "$label" in
internal) mode="${MOCK_CURL_INTERNAL_MODE:-always_ok}" ;;
public) mode="${MOCK_CURL_PUBLIC_MODE:-always_ok}" ;;
*) mode="${MOCK_CURL_OTHER_MODE:-always_ok}" ;;
esac

: "${MOCK_CURL_STATE_DIR:?MOCK_CURL_STATE_DIR not set}"
counter_file="$MOCK_CURL_STATE_DIR/${label}_count"
count=0
[ -f "$counter_file" ] && count="$(cat "$counter_file")"
count=$((count + 1))
echo "$count" >"$counter_file"

case "$mode" in
always_ok)
	exit 0
	;;
always_fail)
	exit 22 # curl -f exit code for an HTTP error response
	;;
ok_after:*)
	threshold="${mode#ok_after:}"
	if [ "$count" -ge "$threshold" ]; then
		exit 0
	else
		exit 22
	fi
	;;
fail_after:*)
	threshold="${mode#fail_after:}"
	if [ "$count" -le "$threshold" ]; then
		exit 0
	else
		exit 22
	fi
	;;
*)
	exit 0
	;;
esac
