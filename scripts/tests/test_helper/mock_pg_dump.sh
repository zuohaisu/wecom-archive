#!/usr/bin/env bash
#=============================================================================
# Mock `pg_dump` used by backup_once.bats.
#
# Controlled via:
#   MOCK_PG_DUMP_MODE   ok | fail (default ok)
#   MOCK_PG_DUMP_LOG    if set, the full argv is appended here
#=============================================================================
if [ -n "${MOCK_PG_DUMP_LOG:-}" ]; then
	printf 'pg_dump %s\n' "$*" >>"$MOCK_PG_DUMP_LOG"
fi

if [ "${MOCK_PG_DUMP_MODE:-ok}" = "fail" ]; then
	echo "pg_dump: error: connection to database failed" >&2
	exit 1
fi

out=""
prev=""
for a in "$@"; do
	if [ "$prev" = "-f" ]; then out="$a"; fi
	prev="$a"
done
echo "FAKE PG DUMP CONTENT" >"$out"
exit 0
