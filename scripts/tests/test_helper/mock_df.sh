#!/usr/bin/env bash
#=============================================================================
# Mock `df` used by disk_usage_check.bats.
#
# Controlled via:
#   MOCK_DF_DISK_PCT   Use%% value returned for a plain `df -P` call (default 31)
#   MOCK_DF_INODE_PCT  Use%% value returned for a `df -iP` call (default 4)
#   MOCK_DF_LOG        if set, the full argv is appended here
#=============================================================================
if [ -n "${MOCK_DF_LOG:-}" ]; then
	printf 'df %s\n' "$*" >>"$MOCK_DF_LOG"
fi

mount_path="${*: -1}"

case "$*" in
*-i*)
	pct="${MOCK_DF_INODE_PCT:-4}"
	echo "Filesystem      Inodes  IUsed   IFree IUse% Mounted on"
	echo "/dev/vda3      2608144 103528 2504616 ${pct}% $mount_path"
	;;
*)
	pct="${MOCK_DF_DISK_PCT:-31}"
	echo "Filesystem      Size  Used Avail Use% Mounted on"
	echo "/dev/vda3        40G   12G   27G ${pct}% $mount_path"
	;;
esac
exit 0
