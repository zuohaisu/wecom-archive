#!/usr/bin/env bash
#=============================================================================
# Mock `flock` for deploy_server.bats. Real `flock` is Linux-only (util-linux)
# and unavailable on macOS dev machines, so deploy_server.sh's concurrency
# lock is exercised through this mock in tests instead -- deploy_server.sh
# only ever calls it as: flock -n <fd>
#
# Controlled via:
#   MOCK_FLOCK_MODE   ok | fail   (default ok) -- ok simulates lock acquired,
#                      fail simulates the lock already being held elsewhere
#   MOCK_FLOCK_LOG     if set, the full argv is appended here
#=============================================================================

if [ -n "${MOCK_FLOCK_LOG:-}" ]; then
	printf 'flock %s\n' "$*" >>"$MOCK_FLOCK_LOG"
fi

[ "${MOCK_FLOCK_MODE:-ok}" = "ok" ] && exit 0 || exit 1
