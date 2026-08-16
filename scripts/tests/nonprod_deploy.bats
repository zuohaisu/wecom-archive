#!/usr/bin/env bats

# Unit tests for the policy used by both the non-production systemd service
# and deploy wrapper.  They never invoke a deploy, git, database, network, or
# systemd command.

LIB="$BATS_TEST_DIRNAME/../nonprod_deployment_lib.sh"
WRAPPER="$BATS_TEST_DIRNAME/../deploy_nonprod.sh"

write_valid_config() {
    cat >"$1" <<'EOF'
NONPROD_DEPLOYMENT=1
APP_ENV=staging
DATABASE_URL=postgresql://nonprod:unused@localhost:5432/wecom_archive_nonprod
FIELD_ENCRYPTION_KEY=SENTINEL_FIELD_ENCRYPTION_KEY
SETTINGS_ENCRYPTION_KEY=SENTINEL_SETTINGS_ENCRYPTION_KEY
AUTH_MODE=password
ADMIN_USERNAME=nonprod-admin
ADMIN_PASSWORD_HASH=SENTINEL_PASSWORD_HASH
ARCHIVE_DOMAIN=archive.nonprod.example.test
ADMIN_DOMAIN=archive.nonprod.example.test
WECOM_THIRD_PARTY_SUITE_ID=SENTINEL_SUITE_ID
WECOM_THIRD_PARTY_SUITE_SECRET=SENTINEL_SECRET
WECOM_THIRD_PARTY_CALLBACK_URL=https://archive.nonprod.example.test/api/auth/wecom/third-party/callback
WECOM_THIRD_PARTY_INSTRUCTION_TOKEN=SENTINEL_TOKEN
WECOM_THIRD_PARTY_INSTRUCTION_ENCODING_AES_KEY=SENTINEL_AES_KEY
WECHAT_PAY_ENABLED=false
MEDIA_STORAGE_PROVIDER=local
KEY_PROVIDER=local_file
STORAGE_LOCAL_PATH=/srv/apps/wecom-archive-365-nonprod/shared/media
EOF
    chmod 600 "$1"
}

setup() {
    TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/nonprod-deploy-test.XXXXXX")"
    CONFIG_FILE="$TEST_TMPDIR/nonprod.env"
    write_valid_config "$CONFIG_FILE"
}

teardown() {
    rm -rf "$TEST_TMPDIR"
}

validate() {
    env APP_ENV=production /bin/bash -c 'source "$1"; validate_nonprod_config "$2"' _ "$LIB" "$CONFIG_FILE"
}

@test "wrapper refuses an unpinned deployment before it reads configuration" {
    run env -u EXPECTED_SHA "$WRAPPER"
    [ "$status" -ne 0 ]
    if [[ "$output" != *"requires an exact 40-character commit SHA"* ]]; then
        echo "wrapper did not report the expected fixed failure" >&2
        return 1
    fi
}

@test "accepts a self-contained, restricted staging configuration" {
    run validate
    [ "$status" -eq 0 ]
}

@test "rejects a configuration readable by its group" {
    chmod 640 "$CONFIG_FILE"

    run validate
    [ "$status" -ne 0 ]
}

@test "rejects an unset non-production marker without printing configuration values" {
    sed -i.bak 's/^NONPROD_DEPLOYMENT=1$/NONPROD_DEPLOYMENT=0/' "$CONFIG_FILE"

    run validate
    [ "$status" -ne 0 ]
    if [[ "$output" == *"SENTINEL_SECRET"* ]]; then
        echo "validator leaked a configuration value" >&2
        return 1
    fi
}

@test "rejects a payment-enabled configuration" {
    sed -i.bak 's/^WECHAT_PAY_ENABLED=false$/WECHAT_PAY_ENABLED=true/' "$CONFIG_FILE"

    run validate
    [ "$status" -ne 0 ]
}

@test "rejects a database name outside the isolated non-production database" {
    sed -i.bak 's#wecom_archive_nonprod#wecom_archive#' "$CONFIG_FILE"

    run validate
    [ "$status" -ne 0 ]
}

@test "rejects an archive credential even when every staging control is present" {
    printf '%s\n' 'WECOM_ARCHIVE_SECRET=SENTINEL_SECRET' >>"$CONFIG_FILE"

    run validate
    [ "$status" -ne 0 ]
}

@test "rejects a deployment selector from the EnvironmentFile" {
    printf '%s\n' 'DEPLOY_DIR=/srv/apps/wecom-archive-365/current' >>"$CONFIG_FILE"

    run validate
    [ "$status" -ne 0 ]
}
