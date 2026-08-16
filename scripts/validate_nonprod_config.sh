#!/usr/bin/env bash
# Validate the one approved non-production runtime configuration channel.
# Emits no values, paths, or parsed configuration details on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly NONPROD_ENV_FILE="/etc/wecom-archive-365/nonprod.env"

# shellcheck source=nonprod_deployment_lib.sh
source "$SCRIPT_DIR/nonprod_deployment_lib.sh"

if ! validate_nonprod_config "$NONPROD_ENV_FILE"; then
    echo "ERROR: controlled non-production configuration is invalid or unavailable." >&2
    exit 1
fi

echo "Non-production configuration policy: OK"
