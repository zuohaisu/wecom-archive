#!/usr/bin/env bash
#=============================================================================
# Mock `git` used by deploy_server.bats to test scripts/deploy_server.sh's
# command ordering and rollback behavior without a real repository, remote,
# or network access.
#
# State is a single file, $MOCK_GIT_STATE_DIR/HEAD, holding the "current"
# commit SHA — `rev-parse HEAD` reads it, `pull`/`checkout` write it.
#
# Controlled entirely via env vars:
#   MOCK_GIT_STATE_DIR       required — directory holding the HEAD state file
#   MOCK_GIT_PULL_MODE       ok | fail   (default ok)
#   MOCK_GIT_NEW_SHA         SHA `pull` moves HEAD to when MODE=ok
#   MOCK_GIT_DIRTY           1 => `status --porcelain` reports a dirty tree
#   MOCK_GIT_CHECKOUT_MODE   ok | fail   (default ok)
#   MOCK_GIT_MERGE_BASE_MODE ok | fail   (default ok) -- `merge-base --is-ancestor`
#   MOCK_GIT_UNTRACKED_PATHS newline-delimited paths emitted by `ls-files --others`
#   MOCK_GIT_TARGET_PATHS    newline-delimited target-tree paths for `cat-file`
#   MOCK_GIT_LOG             if set, the full argv is appended here
#=============================================================================

if [ -n "${MOCK_GIT_LOG:-}" ]; then
	printf 'git %s\n' "$*" >>"$MOCK_GIT_LOG"
fi

: "${MOCK_GIT_STATE_DIR:?MOCK_GIT_STATE_DIR not set}"
head_file="$MOCK_GIT_STATE_DIR/HEAD"

path_in_target() {
	local path="$1"
	printf '%s\n' "${MOCK_GIT_TARGET_PATHS:-}" | grep -Fqx -- "$path"
}

path_has_target_descendant() {
	local path="$1" candidate
	while IFS= read -r candidate; do
		case "$candidate" in
		"$path"/*) return 0 ;;
		esac
	done <<EOF
${MOCK_GIT_TARGET_PATHS:-}
EOF
	return 1
}

case "$1" in
rev-parse)
	case "${2:-}" in
	FETCH_HEAD) printf '%s\n' "${MOCK_GIT_NEW_SHA:?MOCK_GIT_NEW_SHA not set}" ;;
	*) cat "$head_file" ;;
	esac
	exit 0
	;;
status)
	if [ "${MOCK_GIT_DIRTY:-0}" = "1" ]; then
		echo " M some/dirty/file.py"
	fi
	exit 0
	;;
fetch)
	exit 0
	;;
ls-files)
	if [[ " $* " == *" -z "* ]]; then
		while IFS= read -r path; do
			[ -n "$path" ] && printf '%s\0' "$path"
		done <<EOF
${MOCK_GIT_UNTRACKED_PATHS:-}
EOF
	else
		printf '%s\n' "${MOCK_GIT_UNTRACKED_PATHS:-}"
	fi
	exit 0
	;;
cat-file)
	path="${*: -1}"
	path="${path#*:}"
	case "${2:-}" in
	-e)
		path_in_target "$path" || path_has_target_descendant "$path"
		exit $?
		;;
	-t)
		if path_in_target "$path"; then
			echo blob
		elif path_has_target_descendant "$path"; then
			echo tree
		else
			exit 1
		fi
		exit 0
		;;
	esac
	exit 1
	;;
merge-base)
	# deploy_server.sh only ever calls: git merge-base --is-ancestor <a> <b>
	[ "${MOCK_GIT_MERGE_BASE_MODE:-ok}" = "ok" ] && exit 0 || exit 1
	;;
pull)
	if [ "${MOCK_GIT_PULL_MODE:-ok}" = "ok" ]; then
		: "${MOCK_GIT_NEW_SHA:?MOCK_GIT_NEW_SHA not set}"
		echo "$MOCK_GIT_NEW_SHA" >"$head_file"
		exit 0
	else
		echo "mock git pull: simulated failure" >&2
		exit 1
	fi
	;;
checkout)
	# deploy_server.sh calls: git checkout -B "$GIT_BRANCH" "$PREV_SHA"
	sha="${*: -1}"
	if [ "${MOCK_GIT_CHECKOUT_MODE:-ok}" = "ok" ]; then
		echo "$sha" >"$head_file"
		exit 0
	else
		echo "mock git checkout: simulated failure" >&2
		exit 1
	fi
	;;
*)
	exit 0
	;;
esac
