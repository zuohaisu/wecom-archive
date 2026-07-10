#!/usr/bin/env bats
# Real OS-level proof that qiniu_helper.py never exposes secrets via argv
# (visible to any local user via `ps`), stdout, stderr, or the renew.sh log
# file — even during a real, in-flight subprocess execution. Complements
# the in-process assertions in tests/test_qiniu_helper.py.

load test_helper/common

setup() {
    common_setup
    source "$SSL_RENEW_ROOT/lib/common.sh"
    source "$SSL_RENEW_ROOT/lib/qiniu.sh"
    SECRET_AK="SUPERSECRETAK-argvtest-00000000"
    SECRET_SK="SUPERSECRETSK-argvtest-11111111"
    export QINIU_ACCESS_KEY="$SECRET_AK" QINIU_SECRET_KEY="$SECRET_SK"
    mkdir -p "$TEST_TMPDIR/cert"
    gen_cert media.crowntime.cn \
        "$TEST_TMPDIR/cert/fullchain.cer" \
        "$TEST_TMPDIR/cert/media.crowntime.cn.key"
}
teardown() { common_teardown; }

# ── 1. argv does not contain AK/SK/Authorization/private key ───────────
@test "argv of a running qiniu_helper.py upload never contains AK/SK or key content" {
    port=$(find_free_port)
    # --delay gives us a window to sample `ps` while the process is alive
    # and has already parsed argv (the exposure window that matters).
    start_mock_server "$port" --body '{"certID":"abc"}' --delay 2 >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$port"

    "$QINIU_HELPER_PYTHON" "$QINIU_HELPER_SCRIPT" upload \
        --domain media.crowntime.cn \
        --cert-file "$TEST_TMPDIR/cert/fullchain.cer" \
        --key-file "$TEST_TMPDIR/cert/media.crowntime.cn.key" \
        --timeout 10 &
    child_pid=$!

    sleep 0.5
    # macOS/BSD and Linux both support `ps -ww -o command= -p PID`.
    ps_output=$(ps -ww -o command= -p "$child_pid" 2>/dev/null || true)
    # Also capture the full process table in case the PID above is a
    # shell wrapper rather than the python interpreter itself.
    ps_output_full=$(ps -ww -o command= 2>/dev/null | grep -F "qiniu_helper.py" || true)

    wait "$child_pid" || true

    combined="$ps_output"$'\n'"$ps_output_full"
    [[ "$combined" != *"$SECRET_AK"* ]]
    [[ "$combined" != *"$SECRET_SK"* ]]
    [[ "$combined" != *"BEGIN PRIVATE KEY"* ]]
    # Only the file *path* may appear, never file content.
    [[ "$combined" == *"--cert-file"* ]]
    [[ "$combined" == *"fullchain.cer"* ]]
}

# ── 2/3. stdout/stderr never contain secrets (success and failure) ──────
@test "stdout/stderr of a successful upload never contain AK/SK/private key" {
    port=$(find_free_port)
    start_mock_server "$port" --body '{"certID":"abc"}' >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$port"

    run "$QINIU_HELPER_PYTHON" "$QINIU_HELPER_SCRIPT" upload \
        --domain media.crowntime.cn \
        --cert-file "$TEST_TMPDIR/cert/fullchain.cer" \
        --key-file "$TEST_TMPDIR/cert/media.crowntime.cn.key" \
        --timeout 10
    [ "$status" -eq 0 ]
    [[ "$output" != *"$SECRET_AK"* ]]
    [[ "$output" != *"$SECRET_SK"* ]]
    [[ "$output" != *"BEGIN PRIVATE KEY"* ]]
}

@test "stdout/stderr of a failed upload (401) never contain AK/SK/private key" {
    port=$(find_free_port)
    start_mock_server "$port" --status 401 --body '{"error":"denied"}' >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$port"

    run "$QINIU_HELPER_PYTHON" "$QINIU_HELPER_SCRIPT" upload \
        --domain media.crowntime.cn \
        --cert-file "$TEST_TMPDIR/cert/fullchain.cer" \
        --key-file "$TEST_TMPDIR/cert/media.crowntime.cn.key" \
        --timeout 10
    [ "$status" -ne 0 ]
    [[ "$output" == *"HTTP 401"* ]]
    [[ "$output" != *"$SECRET_AK"* ]]
    [[ "$output" != *"$SECRET_SK"* ]]
    [[ "$output" != *"BEGIN PRIVATE KEY"* ]]
}

# ── 4. Renew.sh log file never contains secrets end-to-end ─────────────
@test "renew.sh log output never contains AK/SK/private key even on a Qiniu failure" {
    port=$(find_free_port)
    start_mock_server "$port" --status 403 --body '{"error":"denied"}' >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$port"

    mkdir -p "$HOME/.acme.sh"
    cat >"$HOME/.acme.sh/acme.sh" <<EOF
#!/usr/bin/env bash
mkdir -p "$HOME/.acme.sh/media.crowntime.cn"
cp "$TEST_TMPDIR/cert/fullchain.cer" "$HOME/.acme.sh/media.crowntime.cn/fullchain.cer"
cp "$TEST_TMPDIR/cert/media.crowntime.cn.key" "$HOME/.acme.sh/media.crowntime.cn/media.crowntime.cn.key"
exit 0
EOF
    chmod +x "$HOME/.acme.sh/acme.sh"

    "$SSL_RENEW_ROOT/renew.sh" media.crowntime.cn >"$TEST_TMPDIR/renew.log" 2>&1 || true

    [[ "$(cat "$TEST_TMPDIR/renew.log")" != *"$SECRET_AK"* ]]
    [[ "$(cat "$TEST_TMPDIR/renew.log")" != *"$SECRET_SK"* ]]
    [[ "$(cat "$TEST_TMPDIR/renew.log")" != *"BEGIN PRIVATE KEY"* ]]
    grep -q "HTTP 403" "$TEST_TMPDIR/renew.log"
}

# ── 5. Exception path (connection refused) never leaks secrets ─────────
@test "a Qiniu connection failure never leaks AK/SK via the exception path" {
    unused_port=$(find_free_port)
    export QINIU_API_HOST="http://127.0.0.1:$unused_port"

    run "$QINIU_HELPER_PYTHON" "$QINIU_HELPER_SCRIPT" bind \
        --domain media.crowntime.cn --cert-id certid-123 --timeout 3
    [ "$status" -ne 0 ]
    [[ "$output" != *"$SECRET_AK"* ]]
    [[ "$output" != *"$SECRET_SK"* ]]
}

# ── 6. Debug-mode logging still does not leak secrets ───────────────────
@test "PYTHONVERBOSE / debug-style env flags do not cause qiniu_helper.py to leak secrets" {
    port=$(find_free_port)
    start_mock_server "$port" --body '{"certID":"abc"}' >/dev/null
    export QINIU_API_HOST="http://127.0.0.1:$port"
    export PYTHONVERBOSE=1
    export QINIU_HELPER_DEBUG=1

    run "$QINIU_HELPER_PYTHON" "$QINIU_HELPER_SCRIPT" upload \
        --domain media.crowntime.cn \
        --cert-file "$TEST_TMPDIR/cert/fullchain.cer" \
        --key-file "$TEST_TMPDIR/cert/media.crowntime.cn.key" \
        --timeout 10
    [[ "$output" != *"$SECRET_AK"* ]]
    [[ "$output" != *"$SECRET_SK"* ]]
    [[ "$output" != *"BEGIN PRIVATE KEY"* ]]
}
