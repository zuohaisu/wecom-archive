#!/usr/bin/env bash
#=============================================================================
# systemd_static_check.sh — best-effort structural check of a systemd unit
# file, for use where `systemd-analyze verify` itself isn't available (e.g.
# macOS with no Docker). This is NOT a substitute for `systemd-analyze
# verify` — it only catches gross structural mistakes (missing sections,
# missing required directives, malformed KEY=VALUE lines). Always run the
# real `systemd-analyze verify` (via ssl-renew/Dockerfile) before deploying.
#
# Usage: systemd_static_check.sh <unit-file> [unit-file ...]
# Exit: 0 if every file passes, 1 if any file fails a check, 2 on usage error.
#=============================================================================
set -uo pipefail

if [ $# -lt 1 ]; then
	echo "Usage: systemd_static_check.sh <unit-file> [unit-file ...]" >&2
	exit 2
fi

FAILURES=0

check_unit_file() {
	local file="$1"
	local base
	base="$(basename "$file")"
	local ext="${base##*.}"

	if [ ! -f "$file" ]; then
		echo "[FAIL] $file: not found"
		FAILURES=$((FAILURES + 1))
		return
	fi

	# [Install] is required for .timer (systemctl enable targets it
	# directly); a timer-triggered .service is never enabled on its own,
	# so [Install] there is optional, not required.
	local -a required_sections=("[Unit]")
	case "$ext" in
	service) required_sections+=("[Service]") ;;
	timer) required_sections+=("[Timer]" "[Install]") ;;
	*)
		echo "[FAIL] $file: unrecognized unit extension '.$ext' (expected .service/.timer)"
		FAILURES=$((FAILURES + 1))
		return
		;;
	esac

	local ok=1
	for section in "${required_sections[@]}"; do
		if ! grep -qF "$section" "$file"; then
			echo "[FAIL] $file: missing required section $section"
			FAILURES=$((FAILURES + 1))
			ok=0
		fi
	done

	# Every non-blank, non-comment, non-section line must be KEY=VALUE.
	local bad_lines
	bad_lines=$(grep -vE '^\s*(#|;|$|\[[A-Za-z]+\])' "$file" | grep -vE '^\s*[A-Za-z][A-Za-z0-9]*=' || true)
	if [ -n "$bad_lines" ]; then
		echo "[FAIL] $file: malformed line(s) (expected KEY=VALUE):"
		echo "${bad_lines//$'\n'/$'\n'    }"
		FAILURES=$((FAILURES + 1))
		ok=0
	fi

	if [ "$ext" = "service" ]; then
		grep -q '^ExecStart=' "$file" || {
			echo "[FAIL] $file: [Service] has no ExecStart="
			FAILURES=$((FAILURES + 1))
			ok=0
		}
		grep -q '^TimeoutStartSec=' "$file" || {
			echo "[FAIL] $file: [Service] has no TimeoutStartSec= (default 90s is too short for this workload)"
			FAILURES=$((FAILURES + 1))
			ok=0
		}
		grep -q '^User=' "$file" || {
			echo "[FAIL] $file: [Service] has no User= (must not run as root)"
			FAILURES=$((FAILURES + 1))
			ok=0
		}
	fi

	if [ "$ext" = "timer" ]; then
		grep -q '^OnCalendar=' "$file" || {
			echo "[FAIL] $file: [Timer] has no OnCalendar="
			FAILURES=$((FAILURES + 1))
			ok=0
		}
		if ! grep -q '^WantedBy=' "$file"; then
			echo "[FAIL] $file: [Install] has no WantedBy="
			FAILURES=$((FAILURES + 1))
			ok=0
		fi
	fi

	[ "$ok" -eq 1 ] && echo "[OK]   $file: structural checks passed (NOT a substitute for systemd-analyze verify)"
}

for f in "$@"; do
	check_unit_file "$f"
done

if [ "$FAILURES" -gt 0 ]; then
	echo "$FAILURES structural issue(s) found." >&2
	exit 1
fi
exit 0
