#!/usr/bin/env bash
#=============================================================================
# Mock `psql` used by disk_usage_check.bats to model the PostgreSQL
# connection-count check without a real database.
#
# Controlled via:
#   MOCK_PSQL_MODE       ok | unreachable (default ok)
#   MOCK_PSQL_CONNS      count(*) FROM pg_stat_activity result (default 7)
#   MOCK_PSQL_MAX_CONN   SHOW max_connections result (default 100)
#   MOCK_PSQL_LOG        if set, the full argv is appended here
#=============================================================================
if [ -n "${MOCK_PSQL_LOG:-}" ]; then
	printf 'psql %s\n' "$*" >>"$MOCK_PSQL_LOG"
fi

if [ "${MOCK_PSQL_MODE:-ok}" = "unreachable" ]; then
	echo "psql: error: connection to server failed" >&2
	exit 1
fi

case "$*" in
*pg_stat_activity*)
	echo "${MOCK_PSQL_CONNS:-7}"
	;;
*max_connections*)
	echo "${MOCK_PSQL_MAX_CONN:-100}"
	;;
*)
	echo ""
	;;
esac
exit 0
