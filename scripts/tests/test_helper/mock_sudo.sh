#!/usr/bin/env bash
# Mock `sudo` for deploy_server.bats — never touches real privilege.
#
# deploy_server.sh calls this both as `sudo <systemctl restart ...>` (step
# 7, no -n — the one sudo call this script always needs to succeed for a
# deploy to proceed at all) and `sudo -n <cmd> [args...]` everywhere else
# (step 10 and _publish_static_dir). In MOCK_SUDO_MODE=ok (default) it
# simply execs the wrapped command, so the real (also-mocked, where
# applicable) systemctl/cp still runs and any side effects/log lines a
# test asserts on still happen.
#
# MOCK_SUDO_MODE=deny simulates a missing/refused NOPASSWD sudoers grant
# for the NON-INTERACTIVE (-n) call sites only -- the exact failure mode
# their WARN-and-continue paths exist for. It deliberately never denies
# the interactive `systemctl restart` call: SUDO_BIN is a single global
# variable shared by every privileged call site in the script, so a test
# exercising step 10's deny path must not also break step 7's restart for
# an unrelated reason (see deploy_server.bats's step 9 comments on
# SUDO_BIN being global for the same reasoning).
set -euo pipefail

: "${MOCK_SUDO_MODE:=ok}"
: "${MOCK_SUDO_LOG:=/dev/null}"

echo "sudo $*" >>"$MOCK_SUDO_LOG"

non_interactive=0
if [ "${1:-}" = "-n" ]; then
    non_interactive=1
    shift
fi

if [ "$MOCK_SUDO_MODE" = "deny" ] && [ "$non_interactive" = "1" ]; then
    exit 1
fi

exec "$@"
