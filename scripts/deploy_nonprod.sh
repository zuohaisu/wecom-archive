#!/usr/bin/env bash
# Deploy an exact main SHA only to the isolated non-production installation.
#
# This wrapper has no user-selectable host, service, checkout, environment
# file, database, or webroot.  Those fixed names are the blast-radius guard;
# use scripts/deploy_server.sh for the separately approved production path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly NONPROD_ROOT="/srv/apps/wecom-archive-365-nonprod"
readonly NONPROD_DEPLOY_DIR="$NONPROD_ROOT/current"
readonly NONPROD_ENV_FILE="/etc/wecom-archive-365/nonprod.env"
readonly NONPROD_SERVICE="wecom-archive-365-nonprod.service"
readonly NONPROD_STATE_DIR="$NONPROD_ROOT/shared/deploy_state"
readonly NONPROD_SHARED_WEBROOT="$NONPROD_ROOT/shared/www/site"
readonly NONPROD_INTERNAL_HEALTH="http://127.0.0.1:18035/health/ready"

expected_sha="${EXPECTED_SHA:-}"
if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: non-production deployment requires an exact 40-character commit SHA." >&2
    exit 1
fi

# The config validator sources the fixed 0600 operator-managed file in an
# empty environment and never prints values.  It must pass before the generic
# deploy script can fetch, migrate, restart, or copy any files.
if ! "$SCRIPT_DIR/validate_nonprod_config.sh"; then
    exit 1
fi

# GitHub Actions takes fd 9 before it checks out the fresh copy of this
# wrapper.  A direct invocation leaves this as 0 and deploy_server.sh takes
# the same non-production lock itself before any checkout.  Reject a forged
# hand-off flag instead of silently skipping that lock.
lock_already_held="${DEPLOY_LOCK_ALREADY_HELD:-0}"
previous_sha=""
case "$lock_already_held" in
    0) ;;
    1)
        previous_sha="${PREV_SHA:-}"
        if [ ! -e "/proc/$$/fd/9" ] || [[ ! "$previous_sha" =~ ^[0-9a-f]{40}$ ]]; then
            echo "ERROR: non-production deployment lock hand-off is invalid." >&2
            exit 1
        fi
        ;;
    *)
        echo "ERROR: non-production deployment lock hand-off is invalid." >&2
        exit 1
        ;;
esac

# Do not inherit arbitrary SSH, runner, or operator variables into the
# deploy process.  Runtime configuration is loaded only from DEPLOY_ENV_FILE
# by deploy_server.sh; only the SHA and already-held lock hand-off survive.
exec env -i \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    HOME="/home/wecomarchive-nonprod" \
    EXPECTED_SHA="$expected_sha" \
    PREV_SHA="$previous_sha" \
    DEPLOY_LOCK_ALREADY_HELD="$lock_already_held" \
    DEPLOY_ENV_FILE="$NONPROD_ENV_FILE" \
    DEPLOY_DIR="$NONPROD_DEPLOY_DIR" \
    DEPLOY_STATE_DIR="$NONPROD_STATE_DIR" \
    SERVICE="$NONPROD_SERVICE" \
    GIT_REMOTE="origin" \
    GIT_BRANCH="main" \
    INTERNAL_HEALTH="$NONPROD_INTERNAL_HEALTH" \
    SHARED_DST="$NONPROD_SHARED_WEBROOT" \
    NGINX_DST="$NONPROD_SHARED_WEBROOT" \
    bash "$SCRIPT_DIR/deploy_server.sh"
