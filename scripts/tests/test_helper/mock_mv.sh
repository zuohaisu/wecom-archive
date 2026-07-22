#!/usr/bin/env bash
#=============================================================================
# Mock `mv` for deploy_server.bats. Used to isolate a failure in the final
# atomic-rename step of _record_known_good's write from a failure in the
# earlier mkdir/temp-file-write steps, which real mv's ordinary semantics
# (moving a file onto an existing directory nests it rather than erroring,
# and permission failures on the shared parent directory would break the
# temp-file write too) cannot reliably isolate on their own.
#
# Controlled via:
#   MOCK_MV_MODE   ok | fail   (default ok)
#   MOCK_MV_LOG    if set, the full argv is appended here
#=============================================================================

if [ -n "${MOCK_MV_LOG:-}" ]; then
	printf 'mv %s\n' "$*" >>"$MOCK_MV_LOG"
fi

[ "${MOCK_MV_MODE:-ok}" = "ok" ] && exit 0 || exit 1
