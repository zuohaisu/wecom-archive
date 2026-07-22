#!/usr/bin/env bash
#=============================================================================
# Mock `systemctl` used by deploy_server.bats.
#
# Controlled via:
#   MOCK_SYSTEMCTL_RESTART_MODE   ok | fail   (default ok)
#   MOCK_SYSTEMCTL_ACTIVE         1 | 0       (is-active result, default 1)
#   MOCK_SYSTEMCTL_LOG            if set, the full argv is appended here
#=============================================================================

if [ -n "${MOCK_SYSTEMCTL_LOG:-}" ]; then
	printf 'systemctl %s\n' "$*" >>"$MOCK_SYSTEMCTL_LOG"
fi

case "$1" in
restart)
	[ "${MOCK_SYSTEMCTL_RESTART_MODE:-ok}" = "ok" ] && exit 0 || exit 1
	;;
is-active)
	if [ "${MOCK_SYSTEMCTL_ACTIVE:-1}" = "1" ]; then
		echo active
		exit 0
	else
		echo failed
		exit 3
	fi
	;;
*)
	exit 0
	;;
esac
