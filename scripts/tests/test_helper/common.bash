#!/usr/bin/env bash
#=============================================================================
# Shared bats test helpers for deploy_server.bats (RND-227, P0-G).
#
# Builds a throwaway fixture tree that LOOKS like the production deploy
# layout (a fake git repo, a fake venv, a fake backend/.env) but every
# external command deploy_server.sh shells out to — git, python, systemctl,
# sudo, curl — is a mock script under test_helper/, wired in via PATH and
# the script's own *_BIN override variables. No test ever touches a real
# git remote, a real venv, a real systemd service, or a real network
# endpoint, and DEPLOY_DIR always lives under a per-test mktemp directory,
# never a real production path.
#=============================================================================

SCRIPTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPTS_ROOT/deploy_server.sh"

deploy_common_setup() {
	TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/deploy-server-test.XXXXXX")"
	export TEST_TMPDIR

	# ── Fixture "production" tree ────────────────────────────────────
	DEPLOY_DIR="$TEST_TMPDIR/deploy_dir"
	mkdir -p "$DEPLOY_DIR/.git" "$DEPLOY_DIR/backend/.venv/bin"
	: >"$DEPLOY_DIR/.git/index"
	chmod u+rw "$DEPLOY_DIR/.git/index"
	: >"$DEPLOY_DIR/backend/requirements.txt"

	# Fake venv activate — just needs to exist and be sourceable; the
	# mock bin dir is already first on PATH (see below), so there is
	# nothing real to activate.
	cat >"$DEPLOY_DIR/backend/.venv/bin/activate" <<'EOF'
# fake activate for bats tests — no-op, mocks are already on PATH
EOF

	# Fake backend/.env — DATABASE_URL includes a fake password so tests
	# can assert it never appears in captured script output (redaction).
	# ADMIN_DOMAIN mirrors production: the deploy loads it as literal data
	# to build the internal readiness probe's Host header (GH-161).
	cat >"$DEPLOY_DIR/backend/.env" <<'EOF'
DATABASE_URL=postgresql://mockuser:supersecretpassword@localhost:5432/mockdb
ADMIN_DOMAIN=admin.example.com
EOF

	# ── Mock command bin dir ─────────────────────────────────────────
	MOCK_BIN_DIR="$TEST_TMPDIR/mockbin"
	mkdir -p "$MOCK_BIN_DIR"
	cp "$TEST_HELPER_DIR/mock_git.sh" "$MOCK_BIN_DIR/git"
	cp "$TEST_HELPER_DIR/mock_python.sh" "$MOCK_BIN_DIR/python"
	cp "$TEST_HELPER_DIR/mock_systemctl.sh" "$MOCK_BIN_DIR/systemctl"
	cp "$TEST_HELPER_DIR/mock_curl.sh" "$MOCK_BIN_DIR/curl"
	cp "$TEST_HELPER_DIR/mock_flock.sh" "$MOCK_BIN_DIR/flock"
	cp "$TEST_HELPER_DIR/mock_mv.sh" "$MOCK_BIN_DIR/mock_mv" # NOT wired as MV_BIN by default -- see mv-failure regression test
	cp "$TEST_HELPER_DIR/mock_sudo.sh" "$MOCK_BIN_DIR/sudo" # NOT wired as SUDO_BIN by default (stays "") -- see step 10 tests
	chmod +x "$MOCK_BIN_DIR"/*
	ORIGINAL_PATH="$PATH"
	PATH="$MOCK_BIN_DIR:$PATH"
	export PATH

	# ── Mock state ────────────────────────────────────────────────────
	MOCK_GIT_STATE_DIR="$TEST_TMPDIR/git_state"
	mkdir -p "$MOCK_GIT_STATE_DIR"
	PREV_SHA="prevsha0000000000000000000000000000"
	NEW_SHA="newsha1111111111111111111111111111"
	echo "$PREV_SHA" >"$MOCK_GIT_STATE_DIR/HEAD"
	export MOCK_GIT_STATE_DIR
	export MOCK_GIT_NEW_SHA="$NEW_SHA"
	export PREV_SHA NEW_SHA

	MOCK_CURL_STATE_DIR="$TEST_TMPDIR/curl_state"
	mkdir -p "$MOCK_CURL_STATE_DIR"
	export MOCK_CURL_STATE_DIR

	# Deliberately OUTSIDE $DEPLOY_DIR (mirrors the production
	# shared/deploy_state layout) so the persisted last-known-good file
	# is never seen as an uncommitted change by the clean-tree guard.
	DEPLOY_STATE_DIR="$TEST_TMPDIR/deploy_state"
	export DEPLOY_STATE_DIR

	CMD_LOG="$TEST_TMPDIR/commands.log"
	: >"$CMD_LOG"
	export MOCK_GIT_LOG="$CMD_LOG"
	export MOCK_PYTHON_LOG="$CMD_LOG"
	export MOCK_SYSTEMCTL_LOG="$CMD_LOG"
	export MOCK_CURL_LOG="$CMD_LOG"
	export MOCK_FLOCK_LOG="$CMD_LOG"
	export MOCK_MV_LOG="$CMD_LOG"
	export MOCK_SUDO_LOG="$CMD_LOG"
	export CMD_LOG

	# ── deploy_server.sh configuration overrides ─────────────────────
	export DEPLOY_DIR
	export SERVICE="mock.service"
	export GIT_REMOTE="origin"
	export GIT_BRANCH="main"
	export EXPECTED_SHA=""          # unset by default -- floating-pull path
	export DEPLOY_ENV_FILE=""        # default remains checkout-local backend/.env
	export SUDO_BIN=""                     # tests never need real sudo
	export SYSTEMCTL_BIN="systemctl"       # resolved via mocked PATH
	export CURL_BIN="curl"
	export PYTHON_BIN="python"
	export FLOCK_BIN="flock"          # resolved via mocked PATH
	export FFMPEG_BIN="true"           # avoid host package management in deploy tests

	# Step 9 (static homepage) targets. deploy_server.sh defaults these to
	# /srv/apps/... and /var/www/... — real production paths this suite must
	# never touch — so they are redirected into the per-test temp tree.
	# Until these existed, step 9 was completely untested: the fixture never
	# created $STATIC_SRC, so every test took its "source not found" branch
	# and three consecutive production breakages shipped through a green suite.
	export SHARED_DST="$TEST_TMPDIR/shared_www"
	export NGINX_DST="$TEST_TMPDIR/nginx_root"
	# Step 10 (managed systemd units) target — never a real /etc/systemd/system.
	export SYSTEMD_UNIT_DIR="$TEST_TMPDIR/systemd_units"
	mkdir -p "$SYSTEMD_UNIT_DIR"
	export INTERNAL_HEALTH="http://mock-host/internal-health"
	export PUBLIC_HEALTH="http://mock-host/public-health"
	export HEALTH_RETRIES=3
	export HEALTH_RETRY_INTERVAL_SECONDS=0
	export HEALTH_CURL_TIMEOUT_SECONDS=1

	# Defaults: everything succeeds unless a test overrides it.
	export MOCK_GIT_PULL_MODE=ok
	export MOCK_GIT_CHECKOUT_MODE=ok
	export MOCK_GIT_MERGE_BASE_MODE=ok
	export MOCK_GIT_DIRTY=0
	export MOCK_GIT_UNTRACKED_PATHS=""
	export MOCK_GIT_TARGET_PATHS=""
	export MOCK_PIP_EXIT=0
	export MOCK_COMPILE_EXIT=0
	export MOCK_ALEMBIC_EXIT=0
	export MOCK_VERIFY_EXIT=0
	export MOCK_SYSTEMCTL_RESTART_MODE=ok
	export MOCK_SYSTEMCTL_ACTIVE=1
	export MOCK_FLOCK_MODE=ok
	export MOCK_MV_MODE=ok # only relevant if a test opts into MV_BIN="$MOCK_BIN_DIR/mock_mv"
	export MOCK_CURL_INTERNAL_MODE=always_ok
	export MOCK_CURL_PUBLIC_MODE=always_ok
	export MOCK_SUDO_MODE=ok # only relevant if a test opts into SUDO_BIN="sudo"
}

deploy_common_teardown() {
	PATH="$ORIGINAL_PATH"
	# The unreachable-webroot test deliberately chmod 000's a directory to
	# simulate a root-owned Nginx root; without restoring permissions first,
	# rm -rf cannot descend into it and the temp tree would leak.
	[ -n "${TEST_TMPDIR:-}" ] && chmod -R u+rwX "$TEST_TMPDIR" 2>/dev/null
	[ -n "${TEST_TMPDIR:-}" ] && rm -rf "$TEST_TMPDIR"
}

# seed_static_site — create a realistic static_site/company_homepage tree
# under $DEPLOY_DIR so step 9 actually runs. Includes a nested asset dir
# (the whole point of the cp -a rewrite: an earlier version copied only
# index.html + style.css and left every other asset 404ing) and a README.md
# (which must never reach a served directory).
seed_static_site() {
	local src="$DEPLOY_DIR/static_site/company_homepage"
	mkdir -p "$src/brand" "$src/assets"
	printf '<html>home</html>\n' >"$src/index.html"
	printf 'body{}\n' >"$src/style.css"
	printf 'contributor docs\n' >"$src/README.md"
	printf '<svg/>\n' >"$src/brand/icon.svg"
	printf 'console.log(1)\n' >"$src/assets/app.js"
	printf '{}\n' >"$src/site.webmanifest"
}

# run_deploy — invokes the real deploy_server.sh with all mocks/overrides
# from setup already exported. Use bats' `run` around this, e.g.:
#     run run_deploy
run_deploy() {
	bash "$DEPLOY_SCRIPT"
}

# assert_output_contains / assert_output_not_contains -- substring checks
# against bats' own $output, used instead of a bare
# `[[ "$output" == *"..."* ]]` statement.
#
# This is NOT a style preference: on macOS's system bash (3.2 -- Apple has
# shipped that same GPLv2-era build since 2007 and never upgrades it), a
# bare `[[ ... ]]` statement evaluating to false does NOT trigger the
# errexit behavior bats' test runner relies on to mark a test failed --
# the assertion silently evaluates to false and execution just continues
# to the next line, so the test can end up reported "ok" even though the
# check never actually held. Confirmed by direct reproduction: a bare
# `[[ "abc" == "xyz" ]]` (no glob, plain double-bracket) does not fail a
# bats test under /bin/bash on this platform, while the POSIX-portable
# equivalents (`[ ... ]`, or `[[ ... ]]` used as an `if` condition, or a
# function's return status, as here) do. Every substring assertion in
# this suite MUST go through one of these two helpers, never a bare
# `[[ "$output" == *...* ]]` -- see deploy_server.bats's history for
# where this was caught (independent QA round 2).
assert_output_contains() {
	if [[ "$output" != *"$1"* ]]; then
		echo "expected output to contain: $1" >&2
		echo "--- actual output ---" >&2
		echo "$output" >&2
		return 1
	fi
}

assert_output_not_contains() {
	if [[ "$output" == *"$1"* ]]; then
		echo "expected output to NOT contain: $1" >&2
		echo "--- actual output ---" >&2
		echo "$output" >&2
		return 1
	fi
}

current_head() {
	cat "$MOCK_GIT_STATE_DIR/HEAD"
}

known_good() {
	cat "$DEPLOY_STATE_DIR/last_known_good_sha" 2>/dev/null
}

seed_known_good() {
	mkdir -p "$DEPLOY_STATE_DIR"
	echo "$1" >"$DEPLOY_STATE_DIR/last_known_good_sha"
}

# seed_managed_units — create deploy/systemd/MANAGED_UNITS plus a fake
# <name>.service/.timer pair under $DEPLOY_DIR, mirroring the real
# wecom-external-contact-reconcile.{service,timer} shape closely enough for
# step 10's install/enable logic to exercise both file kinds.
seed_managed_units() {
	local dir="$DEPLOY_DIR/deploy/systemd"
	mkdir -p "$dir"
	cat >"$dir/MANAGED_UNITS" <<'EOF'
# test manifest
demo-job.service
demo-job.timer
EOF
	cat >"$dir/demo-job.service" <<'EOF'
[Unit]
Description=demo job

[Service]
Type=oneshot
ExecStart=/bin/true
EOF
	cat >"$dir/demo-job.timer" <<'EOF'
[Unit]
Description=demo job timer

[Timer]
OnCalendar=*-*-* 04:15:00

[Install]
WantedBy=timers.target
EOF
}
