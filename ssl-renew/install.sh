#!/usr/bin/env bash
#=============================================================================
# install.sh — Idempotent installer / preflight checker for ssl-renew.
#
# Usage:
#   install.sh                          Check-only (default): verifies
#                                        prerequisites and prints the plan.
#                                        Makes no changes. Safe to run
#                                        anywhere, including macOS/CI.
#   install.sh --apply                  Actually create directories, fix
#                                        permissions, install the systemd
#                                        unit files, and daemon-reload.
#                                        Linux + root only. Safe to re-run
#                                        (idempotent).
#
# Options:
#   --apply                  Perform the install instead of just checking.
#   --service-user <name>    System user the service runs as. Default: wecomarchive
#   --config-dir <path>      Per-domain env file directory. Default: /etc/qiniu-ssl-renew
#   --systemd-dir <path>     Where unit files get installed. Default: /etc/systemd/system
#   --repo-root <path>       Path to this checkout. Default: parent of this script's dir.
#
# On macOS (or any non-Linux OS), --apply for the systemd install steps is
# refused with a clear message — this script never pretends a systemd
# install succeeded where there is no systemd. Use the Docker image
# (ssl-renew/Dockerfile) for a full Linux verification, or run this script
# in --apply mode on the real target host during deployment.
#=============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

APPLY=""
SERVICE_USER="wecomarchive"
CONFIG_DIR="/etc/qiniu-ssl-renew"
SYSTEMD_DIR="/etc/systemd/system"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
	cat >&2 <<'EOF'
Usage: install.sh [--apply] [--service-user NAME] [--config-dir PATH]
                   [--systemd-dir PATH] [--repo-root PATH]
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	--apply)
		APPLY=1
		shift
		;;
	--service-user)
		SERVICE_USER="$2"
		shift 2
		;;
	--config-dir)
		CONFIG_DIR="$2"
		shift 2
		;;
	--systemd-dir)
		SYSTEMD_DIR="$2"
		shift 2
		;;
	--repo-root)
		REPO_ROOT="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit "$EXIT_OK"
		;;
	*)
		echo "unknown option: $1" >&2
		usage
		exit "$EXIT_USAGE_ERROR"
		;;
	esac
done

CHECK_FAILURES=0
ok() { echo "  [OK]   $*"; }
fail() {
	echo "  [FAIL] $*"
	CHECK_FAILURES=$((CHECK_FAILURES + 1))
}
info() { echo "  [INFO] $*"; }

echo "== ssl-renew install.sh ($([ -n "$APPLY" ] && echo apply || echo check-only)) =="

OS_NAME="$(uname -s)"
IS_LINUX=""
[ "$OS_NAME" = "Linux" ] && IS_LINUX=1

echo
echo "-- Platform --"
if [ -n "$IS_LINUX" ]; then
	ok "Linux detected ($(uname -r))"
else
	info "$OS_NAME detected — this is not a systemd target."
	info "systemd install steps will be SKIPPED, not simulated."
	info "Use ssl-renew/Dockerfile for a full Linux + systemd-analyze verify check,"
	info "or run 'install.sh --apply' on the real deployment host."
fi

echo
echo "-- Tooling --"
check_bin() {
	local name="$1"
	if command -v "$name" >/dev/null 2>&1; then
		ok "$name found ($(command -v "$name"))"
		return 0
	fi
	fail "$name not found on PATH"
	return 1
}
check_bin bash
check_bin curl
check_bin openssl
check_bin jq

if check_bin python3; then
	if python3 -c "import qiniu" >/dev/null 2>&1; then
		ok "python3 'qiniu' package importable (official SDK — used for all signed Qiniu API calls)"
	else
		fail "python3 cannot import 'qiniu' — run: pip3 install -r $REPO_ROOT/ssl-renew/requirements.txt"
	fi
fi

bash_major="${BASH_VERSINFO[0]:-0}"
if [ "$bash_major" -ge 3 ]; then
	ok "bash version $BASH_VERSION"
else
	fail "bash version $BASH_VERSION is unexpectedly old (< 3.x)"
fi

if [ -x "$HOME/.acme.sh/acme.sh" ] || command -v acme.sh >/dev/null 2>&1; then
	ok "acme.sh found"
else
	info "acme.sh not found — expected for a fresh host; see docs/ssl-renewal/DEPLOYMENT_GUIDE.md"
fi

if [ -n "$IS_LINUX" ]; then
	check_bin systemctl || info "systemd not found — unexpected on Linux; check the base image"
fi

echo
echo "-- Directories & permissions --"
check_dir_writable_or_creatable() {
	local dir="$1"
	if [ -d "$dir" ]; then
		ok "$dir exists"
	else
		info "$dir does not exist yet$([ -n "$APPLY" ] && echo ' — will be created' || echo ' — would be created by --apply')"
	fi
}
check_dir_writable_or_creatable "$CONFIG_DIR"
check_dir_writable_or_creatable "/var/log/qiniu-ssl-renew"

if [ -d "$CONFIG_DIR" ]; then
	while IFS= read -r -d '' envfile; do
		perms=$(stat -f '%Lp' "$envfile" 2>/dev/null || stat -c '%a' "$envfile" 2>/dev/null)
		if [ "$perms" = "600" ]; then
			ok "$envfile mode 600"
		else
			fail "$envfile mode is $perms, expected 600 (contains secrets)"
		fi
	done < <(find "$CONFIG_DIR" -maxdepth 1 -name '*.env' -print0 2>/dev/null)
fi

echo
echo "-- Plan --"
info "service user:  $SERVICE_USER"
info "config dir:    $CONFIG_DIR  (per-domain *.env, mode 600)"
info "systemd dir:   $SYSTEMD_DIR"
info "unit source:   $REPO_ROOT/deploy/systemd/qiniu-ssl-renew@.{service,timer}"

if [ -z "$APPLY" ]; then
	echo
	echo "Check-only run complete. $CHECK_FAILURES failing check(s)."
	echo "Re-run with --apply on the target Linux host to perform the install."
	[ "$CHECK_FAILURES" -eq 0 ]
	exit $?
fi

echo
echo "-- Applying --"

if [ -z "$IS_LINUX" ]; then
	fail "--apply requested but this is not Linux — refusing to fake a systemd install."
	echo
	echo "Install FAILED: $CHECK_FAILURES failing check(s), systemd steps skipped by design."
	exit "$EXIT_GENERIC_FAILURE"
fi

if [ "$(id -u)" -ne 0 ]; then
	fail "--apply on Linux requires root (for /etc/systemd/system and $CONFIG_DIR)"
	echo
	echo "Install FAILED."
	exit "$EXIT_GENERIC_FAILURE"
fi

# Idempotent: mkdir -p / install / cp are all safe to repeat.
mkdir -p "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
if id "$SERVICE_USER" >/dev/null 2>&1; then
	chown "root:$SERVICE_USER" "$CONFIG_DIR" 2>/dev/null || true
fi
ok "$CONFIG_DIR ready (0750)"

mkdir -p /var/log/qiniu-ssl-renew
if id "$SERVICE_USER" >/dev/null 2>&1; then
	chown "$SERVICE_USER:$SERVICE_USER" /var/log/qiniu-ssl-renew
fi
ok "/var/log/qiniu-ssl-renew ready"

while IFS= read -r -d '' envfile; do
	chmod 600 "$envfile"
	ok "chmod 600 $envfile"
done < <(find "$CONFIG_DIR" -maxdepth 1 -name '*.env' -print0 2>/dev/null)

install -m 0644 "$REPO_ROOT/deploy/systemd/qiniu-ssl-renew@.service" "$SYSTEMD_DIR/qiniu-ssl-renew@.service"
install -m 0644 "$REPO_ROOT/deploy/systemd/qiniu-ssl-renew@.timer" "$SYSTEMD_DIR/qiniu-ssl-renew@.timer"
ok "unit files installed to $SYSTEMD_DIR"

systemctl daemon-reload
ok "systemctl daemon-reload"

echo
echo "Install complete. Enable per domain with:"
echo "  systemctl enable --now qiniu-ssl-renew@<domain>.timer"
exit "$EXIT_OK"
