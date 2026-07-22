#!/usr/bin/env bash
#=============================================================================
# Mock `python` used by deploy_server.bats. deploy_server.sh only ever runs
# python for: `-m pip install ...`, `-m compileall ...`, `-m alembic
# upgrade head`, and `scripts/verify_alembic_head.py` -- branches on argv,
# never touches a real venv/pip/alembic.
#
# Controlled via:
#   MOCK_PIP_EXIT / MOCK_COMPILE_EXIT / MOCK_ALEMBIC_EXIT / MOCK_VERIFY_EXIT
#   (each defaults to 0 -- success)
#   MOCK_PYTHON_LOG   if set, the full argv is appended here
#=============================================================================

if [ -n "${MOCK_PYTHON_LOG:-}" ]; then
	printf 'python %s\n' "$*" >>"$MOCK_PYTHON_LOG"
fi

if [ "$1" = "-m" ]; then
	case "$2" in
	pip)
		exit "${MOCK_PIP_EXIT:-0}"
		;;
	compileall)
		exit "${MOCK_COMPILE_EXIT:-0}"
		;;
	alembic)
		exit "${MOCK_ALEMBIC_EXIT:-0}"
		;;
	*)
		exit 0
		;;
	esac
fi

case "$1" in
*verify_alembic_head.py)
	exit "${MOCK_VERIFY_EXIT:-0}"
	;;
*)
	exit 0
	;;
esac
