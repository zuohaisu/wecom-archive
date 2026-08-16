#!/usr/bin/env bash
# Shared, side-effect-free validation for the controlled non-production host.
#
# This file deliberately accepts the environment-file path as an argument so
# Bats can exercise its policy in a temporary directory.  The executable
# entrypoint fixes that path to the operator-managed location; do not expose
# this function as a user-selectable runtime configuration channel.

_nonprod_env_mode() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

validate_nonprod_config() {
    local config_file="$1" mode

    # A symlink could redirect a checked config file after validation.  The
    # service account owns the regular file and its mode is exactly 0600, so
    # the configuration has one unambiguous, least-privileged authority.
    [ -n "$config_file" ] && [ ! -L "$config_file" ] && [ -f "$config_file" ] && [ -O "$config_file" ] || return 1
    mode="$(_nonprod_env_mode "$config_file")" || return 1
    [ "$mode" = "600" ] || return 1

    # The file may carry application values only.  Reject selector overrides
    # from the raw file (including empty assignments) before evaluating it.
    # Bash initializes PATH even with `env -i`, so this check cannot rely on
    # an environment-value test after the file has been sourced.
    if grep -Eq '^[[:space:]]*(export[[:space:]]+)?(DEPLOY_DIR|DEPLOY_STATE_DIR|DEPLOY_ENV_FILE|SERVICE|GIT_REMOTE|GIT_BRANCH|EXPECTED_SHA|PREV_SHA|DEPLOY_LOCK_ALREADY_HELD|INTERNAL_HEALTH|PUBLIC_HEALTH|SHARED_DST|NGINX_DST|SUDO_BIN|PATH|HOME|PYTHONPATH|PIP_CONFIG_FILE)[[:space:]]*=' "$config_file"; then
        return 1
    fi

    # Evaluate only the operator-owned data-only EnvironmentFile in an empty
    # environment.  This prevents ambient SSH/CI variables from satisfying a
    # missing safety control.  Do not print this subprocess's output: a
    # malformed operator value must not reach CI, journals, or terminal logs.
    # shellcheck disable=SC2016
    env -i /bin/bash -eu -c '
        config_file="$1"
        # shellcheck disable=SC1090
        source "$config_file"

        required() {
            [ -n "${!1:-}" ] || exit 1
        }
        forbidden() {
            [ -z "${!1:-}" ] || exit 1
        }

        required DATABASE_URL
        required FIELD_ENCRYPTION_KEY
        required SETTINGS_ENCRYPTION_KEY
        required ADMIN_USERNAME
        required ADMIN_PASSWORD_HASH
        required ARCHIVE_DOMAIN
        required ADMIN_DOMAIN
        required WECOM_THIRD_PARTY_SUITE_ID
        required WECOM_THIRD_PARTY_SUITE_SECRET
        required WECOM_THIRD_PARTY_CALLBACK_URL
        required WECOM_THIRD_PARTY_INSTRUCTION_TOKEN
        required WECOM_THIRD_PARTY_INSTRUCTION_ENCODING_AES_KEY
        required STORAGE_LOCAL_PATH

        [ "${NONPROD_DEPLOYMENT:-}" = "1" ]
        [ "${APP_ENV:-}" = "staging" ]
        [ "${AUTH_MODE:-}" = "password" ]
        [ "${WECHAT_PAY_ENABLED:-}" = "false" ]
        [ "${MEDIA_STORAGE_PROVIDER:-}" = "local" ]
        [ "${KEY_PROVIDER:-local_file}" = "local_file" ]
        [ "$ADMIN_DOMAIN" = "$ARCHIVE_DOMAIN" ]
        [ "$WECOM_THIRD_PARTY_CALLBACK_URL" = "https://${ARCHIVE_DOMAIN}/api/auth/wecom/third-party/callback" ]
        [ "$STORAGE_LOCAL_PATH" = "/srv/apps/wecom-archive-365-nonprod/shared/media" ]

        # The dedicated database name and absent archive credentials make a
        # staging deployment unable to reuse the production archive
        # database or start a production archive worker by configuration.
        case "$DATABASE_URL" in
            */wecom_archive_nonprod|*/wecom_archive_nonprod\?*) ;;
            *) exit 1 ;;
        esac
        forbidden WECOM_CORP_ID
        forbidden WECOM_ARCHIVE_SECRET
        forbidden WECOM_CALLBACK_TOKEN
        forbidden WECOM_CALLBACK_ENCODING_AES_KEY
        forbidden WECOM_PRIVATE_KEY_PATH
        forbidden WECOM_OAUTH_SECRET
        forbidden WECOM_EXTERNAL_CONTACT_SECRET
        forbidden QINIU_ACCESS_KEY
        forbidden QINIU_SECRET_KEY
        forbidden SMTP_HOST
        forbidden SMTP_USER
        forbidden SMTP_PASSWORD
        forbidden SMTP_FROM
        forbidden WECHAT_PAY_APP_ID
        forbidden WECHAT_PAY_MCH_ID
        forbidden WECHAT_PAY_MERCHANT_SERIAL_NO
        forbidden WECHAT_PAY_MERCHANT_PRIVATE_KEY
        forbidden WECHAT_PAY_API_V3_KEY
        forbidden WECHAT_PAY_PUBLIC_KEY_ID
        forbidden WECHAT_PAY_PUBLIC_KEY

    ' _ "$config_file" >/dev/null 2>&1
}
