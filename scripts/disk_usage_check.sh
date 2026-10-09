#!/usr/bin/env bash
#
# disk_usage_check.sh — RND-193 capacity threshold monitor.
#
# Checks disk %, inode %, memory %, swap %, and PostgreSQL connection %
# against configurable thresholds. Prints one [OK]/[WARN]/[DEGRADED] line
# per check; if any check breaches its threshold, sends a single alert
# through the project's EXISTING webhook channel (ssl-renew/notify.sh,
# ALERT_WEBHOOK_URL env contract — see that script's header) rather than
# reimplementing curl/webhook logic here. When ALERT_WEBHOOK_URL is unset,
# notify.sh itself degrades to log-only, so this script never requires a
# webhook to be configured.
#
# Intended to run every 15-30 minutes via a systemd timer (see
# deploy/systemd/wecom-disk-usage-check.{service,timer}; threshold design
# in https://github.com/zuohaisu/wecom-archive/wiki/Backup-and-Recovery and the ops runbook).
#
# Exit codes:
#   0  all checks passed (or degraded — see below)
#   1  at least one check breached its threshold
#
# A DEGRADED check (e.g. PostgreSQL unreachable) is reported but does not
# by itself set exit 1 — it means this script could not verify that one
# check, not that the underlying resource is over threshold.
#
# ── Configuration (env-overridable; defaults tuned for a 2 vCPU / 2GB box) ─
#   DISK_CHECK_PATH        Filesystem to check disk/inode usage on (default: /)
#   DISK_WARN_PCT          Disk use% warning threshold (default: 80)
#   INODE_WARN_PCT         Inode use% warning threshold (default: 80)
#   MEM_WARN_PCT           Memory-pressure warning threshold (default: 90)
#                          Computed as 100 * (1 - MemAvailable/MemTotal), not
#                          raw "used", since buff/cache is reclaimable.
#   SWAP_WARN_PCT          Swap-used warning threshold (default: 50)
#   PG_CONN_WARN_PCT       PostgreSQL connection-use% warning threshold (default: 80)
#   DATABASE_URL           If unset, the PostgreSQL check is skipped (DEGRADED).
#   ALERT_WEBHOOK_URL      Passed straight through to notify.sh — see its header.
#
# Command/path overrides (for tests — never needed in production):
#   DF_BIN, PSQL_BIN, NOTIFY_BIN, MEMINFO_FILE
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DISK_CHECK_PATH="${DISK_CHECK_PATH:-/}"
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"
INODE_WARN_PCT="${INODE_WARN_PCT:-80}"
MEM_WARN_PCT="${MEM_WARN_PCT:-90}"
SWAP_WARN_PCT="${SWAP_WARN_PCT:-50}"
PG_CONN_WARN_PCT="${PG_CONN_WARN_PCT:-80}"

DF_BIN="${DF_BIN:-df}"
PSQL_BIN="${PSQL_BIN:-psql}"
NOTIFY_BIN="${NOTIFY_BIN:-$SCRIPT_DIR/../ssl-renew/notify.sh}"
MEMINFO_FILE="${MEMINFO_FILE:-/proc/meminfo}"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

WARNINGS=()
WARNED_CHECKS=()
DEGRADED=()

_check() {
	local name="$1" pct="$2" threshold="$3" detail="$4"
	if [ "$pct" -ge "$threshold" ]; then
		echo "[$(_ts)] [WARN] $name: ${pct}% >= ${threshold}% threshold ($detail)"
		WARNINGS+=("$name ${pct}% (threshold ${threshold}%): $detail")
		WARNED_CHECKS+=("$name")
	else
		echo "[$(_ts)] [OK] $name: ${pct}% < ${threshold}% threshold ($detail)"
	fi
}

_degrade() {
	local name="$1" reason="$2"
	echo "[$(_ts)] [DEGRADED] $name: $reason" >&2
	DEGRADED+=("$name: $reason")
}

# ── Disk % ──────────────────────────────────────────────────────────────
disk_line="$("$DF_BIN" -P "$DISK_CHECK_PATH" 2>/dev/null | tail -1)"
disk_pct="$(echo "$disk_line" | awk '{print $5}' | tr -d '%')"
if [[ "$disk_pct" =~ ^[0-9]+$ ]]; then
	_check "disk" "$disk_pct" "$DISK_WARN_PCT" "$DISK_CHECK_PATH"
else
	_degrade "disk" "could not parse df -P output for $DISK_CHECK_PATH"
fi

# ── Inode % ─────────────────────────────────────────────────────────────
inode_line="$("$DF_BIN" -iP "$DISK_CHECK_PATH" 2>/dev/null | tail -1)"
inode_pct="$(echo "$inode_line" | awk '{print $5}' | tr -d '%')"
if [[ "$inode_pct" =~ ^[0-9]+$ ]]; then
	_check "inode" "$inode_pct" "$INODE_WARN_PCT" "$DISK_CHECK_PATH"
else
	_degrade "inode" "could not parse df -iP output for $DISK_CHECK_PATH"
fi

# ── Memory pressure % (100 * (1 - MemAvailable/MemTotal)) ──────────────
if [ -r "$MEMINFO_FILE" ]; then
	mem_total="$(awk '/^MemTotal:/{print $2}' "$MEMINFO_FILE")"
	mem_avail="$(awk '/^MemAvailable:/{print $2}' "$MEMINFO_FILE")"
	if [[ "$mem_total" =~ ^[0-9]+$ ]] && [ "$mem_total" -gt 0 ] && [[ "$mem_avail" =~ ^[0-9]+$ ]]; then
		mem_pct=$(((mem_total - mem_avail) * 100 / mem_total))
		_check "memory" "$mem_pct" "$MEM_WARN_PCT" "MemAvailable-based pressure"
	else
		_degrade "memory" "could not parse MemTotal/MemAvailable from $MEMINFO_FILE"
	fi
else
	_degrade "memory" "$MEMINFO_FILE not readable"
fi

# ── Swap % ──────────────────────────────────────────────────────────────
if [ -r "$MEMINFO_FILE" ]; then
	swap_total="$(awk '/^SwapTotal:/{print $2}' "$MEMINFO_FILE")"
	swap_free="$(awk '/^SwapFree:/{print $2}' "$MEMINFO_FILE")"
	if [[ "$swap_total" =~ ^[0-9]+$ ]] && [[ "$swap_free" =~ ^[0-9]+$ ]]; then
		if [ "$swap_total" -eq 0 ]; then
			echo "[$(_ts)] [OK] swap: no swap configured (0 total)"
		else
			swap_pct=$(((swap_total - swap_free) * 100 / swap_total))
			_check "swap" "$swap_pct" "$SWAP_WARN_PCT" "SwapTotal=${swap_total}kB"
		fi
	else
		_degrade "swap" "could not parse SwapTotal/SwapFree from $MEMINFO_FILE"
	fi
fi

# ── PostgreSQL connection % (skipped entirely if DATABASE_URL unset) ───
if [ -n "${DATABASE_URL:-}" ]; then
	pg_conns="$("$PSQL_BIN" "$DATABASE_URL" -tAc 'SELECT count(*) FROM pg_stat_activity;' 2>/dev/null | tr -d '[:space:]')"
	pg_max="$("$PSQL_BIN" "$DATABASE_URL" -tAc 'SHOW max_connections;' 2>/dev/null | tr -d '[:space:]')"
	if [[ "$pg_conns" =~ ^[0-9]+$ ]] && [[ "$pg_max" =~ ^[0-9]+$ ]] && [ "$pg_max" -gt 0 ]; then
		pg_pct=$((pg_conns * 100 / pg_max))
		_check "pg_connections" "$pg_pct" "$PG_CONN_WARN_PCT" "${pg_conns}/${pg_max} connections"
	else
		_degrade "pg_connections" "could not query pg_stat_activity/max_connections (is PostgreSQL reachable?)"
	fi
else
	echo "[$(_ts)] [SKIP] pg_connections: DATABASE_URL not set"
fi

# ── Alert on any WARN via the existing notify.sh webhook contract ──────
if [ "${#WARNINGS[@]}" -gt 0 ]; then
	message=""
	for w in "${WARNINGS[@]}"; do
		if [ -z "$message" ]; then
			message="$w"
		else
			message="$message; $w"
		fi
	done
	stage=""
	for c in "${WARNED_CHECKS[@]}"; do
		if [ -z "$stage" ]; then
			stage="$c"
		else
			stage="$stage,$c"
		fi
	done
	if [ -x "$NOTIFY_BIN" ]; then
		CURRENT_STAGE="$stage" "$NOTIFY_BIN" WARN "disk-usage-check" "$message" || true
	else
		echo "[$(_ts)] [WARN] notify.sh not found/executable at $NOTIFY_BIN — alert logged only, no webhook attempted" >&2
	fi
	exit 1
fi

exit 0
