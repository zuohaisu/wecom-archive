#!/usr/bin/env bats
# Integration tests for scripts/deploy_server.sh (RND-227, P0-G).
#
# Runs the REAL deploy_server.sh end to end, with git/python/systemctl/curl
# replaced by scripted mocks (test_helper/mock_*.sh) via PATH + the script's
# own *_BIN override variables, and DEPLOY_DIR pointed at a per-test mktemp
# fixture tree — never a real repository, venv, systemd service, network
# endpoint, or production host. Covers the four scenarios RND-227 requires:
#   1. full success (fetch/checkout -> install -> compile -> migrate -> verify ->
#      restart -> readiness -> success)
#   2. migration failure (deploy stops before restart)
#   3. revision mismatch after a "successful" upgrade command (deploy stops
#      before restart)
#   4. restart/readiness failure after a verified migration (automatic
#      rollback to the previous commit, rollback itself health-gated, and
#      the ORIGINAL deploy still exits non-zero)
# plus the clean-working-tree guard and connection-string redaction.

load test_helper/common

setup() { deploy_common_setup; }
teardown() { deploy_common_teardown; }

# Returns the 1-based line number of the first log line matching $1, or
# empty if none — used to assert relative command ordering.
line_of() {
	grep -n "$1" "$CMD_LOG" | head -1 | cut -d: -f1
}

# ---------------------------------------------------------------------
# Scenario 1 — Migration Success
# ---------------------------------------------------------------------

@test "scenario 1: full success runs fetch/checkout -> install -> compile -> migrate -> verify -> restart -> readiness -> success" {
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"

	# Ordering: target fetch/checkout before pip before compileall before
	# alembic before verify_alembic_head.py before systemctl restart before curl.
	fetch_line=$(line_of "git fetch")
	checkout_line=$(line_of "git checkout")
	pip_line=$(line_of "python -m pip install")
	compile_line=$(line_of "python -m compileall")
	alembic_line=$(line_of "python -m alembic upgrade head")
	verify_line=$(line_of "verify_alembic_head.py")
	restart_line=$(line_of "systemctl restart")
	curl_line=$(line_of "curl")

	[ -n "$fetch_line" ]
	[ -n "$checkout_line" ]
	[ -n "$pip_line" ]
	[ -n "$compile_line" ]
	[ -n "$alembic_line" ]
	[ -n "$verify_line" ]
	[ -n "$restart_line" ]
	[ -n "$curl_line" ]

	[ "$fetch_line" -lt "$checkout_line" ]
	[ "$checkout_line" -lt "$pip_line" ]
	[ "$pip_line" -lt "$compile_line" ]
	[ "$compile_line" -lt "$alembic_line" ]
	[ "$alembic_line" -lt "$verify_line" ]
	[ "$verify_line" -lt "$restart_line" ]
	[ "$restart_line" -lt "$curl_line" ]

	# Code actually moved to the new commit, not rolled back.
	[ "$(current_head)" = "$NEW_SHA" ]
}

@test "scenario 1: readiness output never contains the DATABASE_URL password" {
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_not_contains "supersecretpassword"
	run grep -c "supersecretpassword" "$CMD_LOG"
	[ "$status" -ne 0 ] # grep -c: no match => exit 1, count 0
}

# ---------------------------------------------------------------------
# Scenario 2 — Migration Failure
# ---------------------------------------------------------------------

@test "scenario 2: migration failure stops the deploy before restart, static, or health check" {
	export MOCK_ALEMBIC_EXIT=1
	run run_deploy
	[ "$status" -ne 0 ]

	run grep -c "systemctl restart" "$CMD_LOG"
	[ "$status" -ne 0 ] # never called

	run grep -c "^curl " "$CMD_LOG"
	[ "$status" -ne 0 ] # health check never reached

	# Pre-restart failure -> worktree restored, but git checkout must not
	# have been asked to restart anything (no systemctl call in the log).
	[ "$(current_head)" = "$PREV_SHA" ]
}

# ---------------------------------------------------------------------
# Scenario 3 — Revision Mismatch
# ---------------------------------------------------------------------

@test "scenario 3: revision mismatch after upgrade stops the deploy before restart" {
	export MOCK_VERIFY_EXIT=1
	run run_deploy
	[ "$status" -ne 0 ]

	# alembic upgrade head itself "succeeded" (exit 0) -- only the
	# independent revision verification failed.
	run grep -c "python -m alembic upgrade head" "$CMD_LOG"
	[ "$status" -eq 0 ]

	run grep -c "systemctl restart" "$CMD_LOG"
	[ "$status" -ne 0 ] # never called

	[ "$(current_head)" = "$PREV_SHA" ]
}

# ---------------------------------------------------------------------
# Scenario 4 — Health Failure and Rollback
# ---------------------------------------------------------------------

@test "scenario 4: readiness failure after a verified migration triggers rollback, and the deploy still fails" {
	# HEALTH_RETRIES=3 (see common.bash) -> the forward-path gate makes
	# exactly 3 calls, all before the counter crosses this threshold, so
	# it exhausts its retries and fails; the SAME url then succeeds on
	# the rollback's first attempt (call #4).
	export MOCK_CURL_INTERNAL_MODE="ok_after:4"

	run run_deploy
	[ "$status" -ne 0 ] # original deployment must still report failure

	assert_output_contains "ROLLBACK RESULT: SUCCEEDED"

	# systemctl restart called twice: once for the forward deploy, once
	# for the rollback.
	restart_calls=$(grep -c "systemctl restart" "$CMD_LOG")
	[ "$restart_calls" -eq 2 ]

	# Dependencies reinstalled as part of rollback (pip called twice:
	# forward deploy + rollback).
	pip_calls=$(grep -c "python -m pip install" "$CMD_LOG")
	[ "$pip_calls" -eq 2 ]

	# Code ends up back on the previous commit.
	[ "$(current_head)" = "$PREV_SHA" ]

	# Migration must never be re-run or downgraded during rollback.
	alembic_calls=$(grep -c "python -m alembic" "$CMD_LOG")
	[ "$alembic_calls" -eq 1 ]
	run grep -c "downgrade" "$CMD_LOG"
	[ "$status" -ne 0 ]
}

@test "scenario 4b: public-only readiness failure does not trigger a rollback" {
	# Internal gate passes; only the public URL (proxy/DNS/TLS layer)
	# fails -- a code rollback cannot fix that, so it must not happen.
	export MOCK_CURL_PUBLIC_MODE=always_fail

	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_not_contains "Rolling back"
	assert_output_contains "investigate the reverse proxy"

	restart_calls=$(grep -c "systemctl restart" "$CMD_LOG")
	[ "$restart_calls" -eq 1 ] # only the forward restart, no rollback restart

	# Forward deploy already moved code to the new commit -- a
	# proxy-layer failure must not roll that back.
	[ "$(current_head)" = "$NEW_SHA" ]
}

@test "scenario 4c: rollback itself failing to become healthy is reported and the deploy still fails" {
	export MOCK_CURL_INTERNAL_MODE=always_fail # forward AND rollback readiness both fail

	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "ROLLBACK RESULT: FAILED"
	assert_output_contains "Manual intervention required"
}

# ---------------------------------------------------------------------
# Step 2 — ffmpeg host prerequisite (GH-133)
# ---------------------------------------------------------------------
# ffmpeg is a HOST prerequisite; this deploy only checks for it and never
# installs it. GH-133 removed the old auto-install fallback (which shelled
# out to `apt-get` -- a binary that does not even exist on the real
# Alibaba Cloud Linux 3 / dnf-based production host) in favor of a
# fail-fast check with an actionable message. Scenario 1 above
# (FFMPEG_BIN="true", see common.bash) already exercises the present-case
# happy path end-to-end; these tests cover the missing-case failure and
# confirm no package manager is ever invoked either way.

@test "step 2: ffmpeg present passes the host-prerequisite check" {
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Checking ffmpeg host prerequisite"
}

@test "step 2: ffmpeg missing fails the deploy fast with an actionable message, before any dependency install or restart" {
	export FFMPEG_BIN="definitely-not-a-real-binary-gh133"

	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "ffmpeg is required on the production host but is not installed"
	assert_output_contains "docs/operations/deploy-sudoers.md"
	assert_output_contains "does not have package-manager sudo"

	run grep -c "python -m pip install" "$CMD_LOG"
	[ "$status" -ne 0 ] # dependency install never reached

	run grep -c "systemctl restart" "$CMD_LOG"
	[ "$status" -ne 0 ] # restart never reached

	# Pre-restart failure -> worktree restored to the previous commit.
	[ "$(current_head)" = "$PREV_SHA" ]
}

@test "step 2: neither the present nor the missing ffmpeg path ever shells out to apt-get or dnf" {
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_not_contains "apt-get"
	assert_output_not_contains "dnf"

	export FFMPEG_BIN="definitely-not-a-real-binary-gh133"
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_not_contains "apt-get"
	assert_output_not_contains "dnf"
}

# ---------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------

@test "guard: a dirty working tree aborts before any fetch, checkout, install, or restart" {
	export MOCK_GIT_DIRTY=1
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "modified tracked files"

	run grep -cE "git fetch|git checkout" "$CMD_LOG"
	[ "$status" -ne 0 ]
	run grep -c "systemctl restart" "$CMD_LOG"
	[ "$status" -ne 0 ]

	[ "$(current_head)" = "$PREV_SHA" ]
}

@test "guard: a missing .git/index (unreadable) aborts before capturing PREV_SHA" {
	rm -f "$DEPLOY_DIR/.git/index"
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains ".git/index is not accessible"
}

@test "readiness retry count and interval come from HEALTH_RETRIES, not a hardcoded loop" {
	export HEALTH_RETRIES=5
	export MOCK_CURL_INTERNAL_MODE=always_fail
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "attempt 5/5"
	internal_calls=$(grep -c "internal-health" "$CMD_LOG")
	# 5 forward attempts + 5 rollback attempts (rollback also fails here)
	[ "$internal_calls" -eq 10 ]
}

# ---------------------------------------------------------------------
# Independent QA regression tests (RND-227 QA round 1)
# ---------------------------------------------------------------------

@test "QA-03 regression: a flaky call right after the readiness gate already passed does not skip rollback" {
	# fail_after:1 -- the retry loop's own single attempt (count=1)
	# succeeds and the gate reports OK; any call AFTER that (count>1)
	# would fail. Before the fix, an unguarded extra curl here aborted
	# the whole script under `set -e` with no rollback attempted at
	# all. After the fix, there is no such extra call, so the deploy
	# proceeds normally past the gate.
	export MOCK_CURL_INTERNAL_MODE="fail_after:1"
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_not_contains "Rolling back"
}

@test "QA-05 regression: a missing .venv after a successful checkout restores the working tree (not left mid-deploy)" {
	rm -rf "$DEPLOY_DIR/backend/.venv"
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "Virtual environment not found"
	assert_output_contains "Restoring working tree"
	[ "$(current_head)" = "$PREV_SHA" ]
	run grep -c "systemctl restart" "$CMD_LOG"
	[ "$status" -ne 0 ] # never restarted
}

@test "QA-05 regression: a missing backend/.env after a successful checkout restores the working tree" {
	rm -f "$DEPLOY_DIR/backend/.env"
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "deployment configuration file not found"
	assert_output_contains "Restoring working tree"
	[ "$(current_head)" = "$PREV_SHA" ]
}

@test "external EnvironmentFile supplies deployment configuration without leaking its value" {
	local external_env="$TEST_TMPDIR/external-nonprod.env"
	cat >"$external_env" <<'EOF'
DATABASE_URL=postgresql://nonprod:external-config-sentinel@localhost:5432/mockdb
EOF
	rm -f "$DEPLOY_DIR/backend/.env"
	export DEPLOY_ENV_FILE="$external_env"

	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_not_contains "external-config-sentinel"
}

@test "configuration is parsed as data and never executes a malformed PEM continuation" {
	local marker="$TEST_TMPDIR/env-command-must-not-run"
	cat >>"$DEPLOY_DIR/backend/.env" <<EOF
WECHAT_PAY_MERCHANT_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----
touch "$marker"
EOF

	run run_deploy
	[ "$status" -eq 0 ]
	[ ! -e "$marker" ]
	assert_output_contains "ignored non-KEY=value configuration line(s)"
	assert_output_not_contains "env-command-must-not-run"
}

@test "QA-04: a fully successful deploy records the new commit as last-known-good" {
	run run_deploy
	[ "$status" -eq 0 ]
	[ "$(known_good)" = "$NEW_SHA" ]
}

@test "QA-04: rollback targets the persisted last-known-good commit, not just pre-checkout HEAD" {
	# Simulates: an EARLIER deploy left HEAD at a torn/unverified commit
	# ("torn-sha") without updating last-known-good, which still points
	# at the actual last-proven-healthy commit ("real-good-sha"). THIS
	# run's own PREV_SHA (torn-sha) must NOT be used as the rollback
	# target -- the persisted, actually-verified commit must be.
	echo "torn-sha-0000000000000000000000000000" >"$MOCK_GIT_STATE_DIR/HEAD"
	seed_known_good "real-good-sha-000000000000000000000"

	export MOCK_CURL_INTERNAL_MODE="ok_after:4" # forward gate exhausts, rollback succeeds
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "Restoring code to last known-good commit (real-good-sha-000000000000000000000)"
	assert_output_not_contains "Restoring code to last known-good commit (torn-sha"
	[ "$(current_head)" = "real-good-sha-000000000000000000000" ]
	[ "$(known_good)" = "real-good-sha-000000000000000000000" ]
}

@test "QA-04: with no persisted state yet, rollback falls back to pre-checkout HEAD (bootstrap case)" {
	# No seed_known_good call -- $DEPLOY_STATE_DIR/last_known_good_sha
	# does not exist, exactly like the first deploy after adopting
	# RND-227 on a server that predates it.
	export MOCK_CURL_INTERNAL_MODE="ok_after:4"
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "Restoring code to last known-good commit ($PREV_SHA)"
	[ "$(current_head)" = "$PREV_SHA" ]
}

# ---------------------------------------------------------------------
# GH-121 — untracked-path / target-tree checkout preflight
# ---------------------------------------------------------------------

@test "GH-121: no untracked collision permits the pinned deploy to proceed" {
	export EXPECTED_SHA="$NEW_SHA"

	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_not_contains "deployment preflight blocked checkout"
	[ "$(current_head)" = "$NEW_SHA" ]
}

@test "GH-121: CD reads the target helper before its checkout" {
	local workflow="$SCRIPTS_ROOT/../.github/workflows/deploy.yml"
	local preflight_line checkout_line
	preflight_line=$(grep -n 'git show "$EXPECTED_SHA:scripts/deploy_preflight.sh"' "$workflow" | cut -d: -f1)
	checkout_line=$(grep -n 'git checkout -B main "$EXPECTED_SHA"' "$workflow" | cut -d: -f1)

	[ -n "$preflight_line" ]
	[ -n "$checkout_line" ]
	[ "$preflight_line" -lt "$checkout_line" ]
}

@test "GH-121: an untracked file colliding with the target fails before checkout without exposing or deleting it" {
	local collision_path="server-only-artifact"
	local synthetic_content="synthetic-server-only-content-must-not-appear"
	printf '%s\n' "$synthetic_content" >"$DEPLOY_DIR/$collision_path"
	export EXPECTED_SHA="$NEW_SHA"
	export MOCK_GIT_UNTRACKED_PATHS="$collision_path"
	export MOCK_GIT_TARGET_PATHS="$collision_path"

	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "deployment preflight blocked checkout"
	assert_output_contains "$collision_path"
	assert_output_contains "No checkout or cleanup was performed"
	assert_output_not_contains "$synthetic_content"
	[ -f "$DEPLOY_DIR/$collision_path" ]
	[ "$(cat "$DEPLOY_DIR/$collision_path")" = "$synthetic_content" ]
	[ "$(current_head)" = "$PREV_SHA" ]
	run grep -c "^git checkout" "$CMD_LOG"
	[ "$status" -ne 0 ]
}

@test "GH-121: an unrelated untracked path does not block checkout" {
	printf 'synthetic-diagnostic\n' >"$DEPLOY_DIR/unrelated-diagnostic"
	export EXPECTED_SHA="$NEW_SHA"
	export MOCK_GIT_UNTRACKED_PATHS="unrelated-diagnostic"
	export MOCK_GIT_TARGET_PATHS="repo-managed-artifact"

	run run_deploy
	[ "$status" -eq 0 ]
	[ -f "$DEPLOY_DIR/unrelated-diagnostic" ]
	[ "$(current_head)" = "$NEW_SHA" ]
}

@test "GH-121: an untracked symlink blocks a target path below that symlink" {
	local collision_path="server-only-link"
	ln -s "/synthetic-server-only-target" "$DEPLOY_DIR/$collision_path"
	export EXPECTED_SHA="$NEW_SHA"
	export MOCK_GIT_UNTRACKED_PATHS="$collision_path"
	export MOCK_GIT_TARGET_PATHS="$collision_path/repo-child"

	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "$collision_path"
	[ -L "$DEPLOY_DIR/$collision_path" ]
	[ "$(current_head)" = "$PREV_SHA" ]
	run grep -c "^git checkout" "$CMD_LOG"
	[ "$status" -ne 0 ]
}

@test "GH-121: an untracked descendant blocks a target regular-file parent" {
	mkdir -p "$DEPLOY_DIR/server-only-directory"
	printf 'synthetic-child\n' >"$DEPLOY_DIR/server-only-directory/child"
	export EXPECTED_SHA="$NEW_SHA"
	export MOCK_GIT_UNTRACKED_PATHS="server-only-directory/child"
	export MOCK_GIT_TARGET_PATHS="server-only-directory"

	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "server-only-directory/child"
	[ -f "$DEPLOY_DIR/server-only-directory/child" ]
	[ "$(current_head)" = "$PREV_SHA" ]
	run grep -c "^git checkout" "$CMD_LOG"
	[ "$status" -ne 0 ]
}

@test "QA-01: EXPECTED_SHA pins the deploy to the CI-tested commit" {
	export EXPECTED_SHA="$NEW_SHA"
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Pinned to EXPECTED_SHA=$NEW_SHA"
	[ "$(current_head)" = "$NEW_SHA" ]
	run grep -c "^git pull" "$CMD_LOG"
	[ "$status" -ne 0 ] # floating `git pull` must never run when pinned
}

@test "QA-01: a non-fast-forward EXPECTED_SHA is refused before checkout/restart" {
	export EXPECTED_SHA="$NEW_SHA"
	export MOCK_GIT_MERGE_BASE_MODE=fail
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "not a fast-forward"
	run grep -c "systemctl restart" "$CMD_LOG"
	[ "$status" -ne 0 ]
	[ "$(current_head)" = "$PREV_SHA" ] # never even checked out
}

@test "GH-121: EXPECTED_SHA unset resolves, preflights, and checks out one fetched target (manual/first-run use)" {
	run run_deploy
	[ "$status" -eq 0 ]
	run grep -c "^git fetch" "$CMD_LOG"
	[ "$status" -eq 0 ]
	run grep -c "^git checkout" "$CMD_LOG"
	[ "$status" -eq 0 ]
	run grep -c "^git pull" "$CMD_LOG"
	[ "$status" -ne 0 ]
	assert_output_not_contains "Pinned to EXPECTED_SHA"
}

# ---------------------------------------------------------------------
# Independent QA regression tests (RND-227 QA round 2)
# ---------------------------------------------------------------------

@test "QA round 2 / bootstrap: when the caller already checked out EXPECTED_SHA and exported PREV_SHA, the script skips its own fetch/checkout" {
	# Simulates the new .github/workflows/deploy.yml flow: the workflow's
	# OWN inline script already fetched+checked-out EXPECTED_SHA and
	# exported PREV_SHA before invoking deploy_server.sh -- this is what
	# actually closes the "deploy that ships a script change still runs
	# the OLD script" bootstrap gap, since the checkout logic now lives
	# in the always-fresh workflow YAML, not the possibly-stale on-disk
	# script.
	echo "$NEW_SHA" >"$MOCK_GIT_STATE_DIR/HEAD" # caller's checkout already happened
	export EXPECTED_SHA="$NEW_SHA"
	export PREV_SHA="$PREV_SHA" # caller-captured value (already set by common setup)

	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Already at EXPECTED_SHA=$NEW_SHA (checked out by the caller)"
	assert_output_contains "Previous commit: $PREV_SHA"
	run grep -c "^git pull\|git fetch\|git checkout" "$CMD_LOG"
	[ "$status" -ne 0 ] # no redundant pull/fetch/checkout from this script
	[ "$(current_head)" = "$NEW_SHA" ]
}

@test "QA round 2 / concurrency: a held lock refuses to run, before anything else happens" {
	export MOCK_FLOCK_MODE=fail
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "another deploy is already in progress"
	# nothing beyond the flock check itself should have run
	run grep -cE "^(git|python|systemctl|curl) " "$CMD_LOG"
	[ "$status" -ne 0 ]
	[ "$(current_head)" = "$PREV_SHA" ]
}

@test "QA round 2 / last-known-good durability: a persist failure after full success fails the deploy" {
	# The lock file must already exist and be writable (so the earlier
	# flock step still succeeds), but the directory itself must refuse
	# NEW file creation (so _record_known_good's temp-file write fails)
	# -- otherwise this collides with the lock's own `mkdir -p` instead
	# of isolating the known-good persistence path.
	mkdir -p "$DEPLOY_STATE_DIR"
	: >"$DEPLOY_STATE_DIR/deploy.lock"
	chmod 644 "$DEPLOY_STATE_DIR/deploy.lock"
	chmod 555 "$DEPLOY_STATE_DIR"
	run run_deploy
	chmod 755 "$DEPLOY_STATE_DIR" # restore so teardown's rm -rf can clean up
	[ "$status" -ne 0 ]
	assert_output_contains "could not persist last-known-good"
	assert_output_contains "Treating this deploy as failed"
	assert_output_not_contains "Deploy complete"
}

@test "QA round 2 / last-known-good durability: a persist failure during rollback is logged but does not change the rollback outcome" {
	# The lock file must already exist and be writable (so flock/exec
	# still succeeds), but the directory itself must refuse new file
	# creation (so _record_known_good's temp-file write fails) --
	# otherwise this collides with the lock setup itself instead of
	# isolating the known-good persistence path.
	mkdir -p "$DEPLOY_STATE_DIR"
	: >"$DEPLOY_STATE_DIR/deploy.lock"
	chmod 644 "$DEPLOY_STATE_DIR/deploy.lock"
	chmod 555 "$DEPLOY_STATE_DIR"
	export MOCK_CURL_INTERNAL_MODE="ok_after:4" # forward gate exhausts, rollback succeeds
	run run_deploy
	chmod 755 "$DEPLOY_STATE_DIR" # restore so teardown's rm -rf can clean up
	[ "$status" -ne 0 ] # already non-zero regardless
	assert_output_contains "ROLLBACK RESULT: SUCCEEDED"
	assert_output_contains "could not persist last-known-good"
}

# ---------------------------------------------------------------------
# Independent QA regression tests (RND-227 QA round 3)
# ---------------------------------------------------------------------

@test "QA round 3 / mv failure: the rename step failing is NOT reported as deploy success" {
	# Isolates a failure in the FINAL atomic-rename step specifically
	# (as opposed to the earlier mkdir/temp-write steps, already covered
	# above) -- real `mv`'s ordinary failure modes can't cleanly isolate
	# this (moving onto an existing directory nests rather than errors;
	# a read-only parent dir would break the temp-file write too), so
	# this test points MV_BIN at a mock that fails only the rename call.
	export MV_BIN="$MOCK_BIN_DIR/mock_mv"
	export MOCK_MV_MODE=fail
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "rename to"
	assert_output_contains "could not persist last-known-good"
	assert_output_contains "Treating this deploy as failed"
	assert_output_not_contains "Deploy complete"
	# The temp file write itself succeeded (only the rename was mocked
	# to fail) -- confirms this test isolates the rename step, not an
	# earlier one already covered by the mkdir/temp-write test above.
	[ -f "$DEPLOY_STATE_DIR/last_known_good_sha.tmp" ]
}

@test "QA round 3 / lock ordering: DEPLOY_LOCK_ALREADY_HELD=1 skips this script's own flock call" {
	# Simulates being invoked from the workflow's wrapper script, which
	# now acquires the lock itself BEFORE doing its checkout (see
	# .github/workflows/deploy.yml) and passes DEPLOY_LOCK_ALREADY_HELD=1
	# down. This script must not attempt its own flock in that case --
	# a second, distinct open-file-description lock attempt on the same
	# path would self-deadlock against the fd the caller already holds.
	export DEPLOY_LOCK_ALREADY_HELD=1
	export MOCK_FLOCK_MODE=fail # if the script DID call flock, this would wrongly abort it
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	run grep -c "^flock " "$CMD_LOG"
	[ "$status" -ne 0 ] # flock was never invoked by this script
}

@test "QA round 3 / lock ordering: without the flag, direct invocation still takes its own lock (manual-run path unchanged)" {
	export MOCK_FLOCK_MODE=fail
	run run_deploy
	[ "$status" -ne 0 ]
	assert_output_contains "another deploy is already in progress"
	run grep -c "^flock " "$CMD_LOG"
	[ "$status" -eq 0 ] # flock WAS invoked (and correctly denied) this time
}

# ---------------------------------------------------------------------
# Step 9 — static homepage publication
#
# This step had ZERO coverage until now: the fixture never created
# $STATIC_SRC, so every test above silently exercised its "static site
# source not found — skipping" branch. Three production breakages shipped
# through this blind spot in a row (rsync introduced but absent from the
# host; a self-healing `sudo apt-get install rsync` that the host's
# sudoers forbids; then `sudo mkdir` failing for that same reason).
# ---------------------------------------------------------------------

@test "step 9: publishes the whole homepage tree, including nested assets, and never serves README.md" {
	seed_static_site
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "shared OK"
	assert_output_contains "nginx root OK"

	# Every asset reaches BOTH destinations -- not just index.html/style.css.
	for f in index.html style.css site.webmanifest brand/icon.svg assets/app.js; do
		[ -f "$SHARED_DST/$f" ]
		[ -f "$NGINX_DST/$f" ]
	done

	# Contributor docs must never be published to a served directory.
	[ ! -e "$SHARED_DST/README.md" ]
	[ ! -e "$NGINX_DST/README.md" ]
}

@test "step 9: an unreachable Nginx webroot warns loudly but does NOT fail the deploy or skip the last-known-good record" {
	# Regression test for the CD failures of 2026-08-01/02: the runtime user
	# has no passwordless sudo beyond `systemctl restart`, so the webroot copy
	# cannot succeed. That must not fail a deploy whose backend is already
	# restarted, health-gated and live -- and must not skip _record_known_good,
	# which would leave the NEXT deploy without a rollback target.
	[ "$(id -u)" -ne 0 ] || skip "running as root: a permission-denied webroot is not reproducible"

	local locked="$TEST_TMPDIR/locked"
	mkdir -p "$locked"
	chmod 000 "$locked"
	export NGINX_DST="$locked/site"     # unwritable parent, and SUDO_BIN="" in this fixture

	seed_static_site
	run run_deploy

	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "could not publish the homepage"
	assert_output_contains "backend deploy is UNAFFECTED"
	assert_output_not_contains "nginx root OK"

	# The shared staging copy still happened ...
	[ -f "$SHARED_DST/index.html" ]
	# ... and the rollback record was still written.
	[ "$(known_good)" = "$NEW_SHA" ]
}

@test "step 9: publishes into an already-writable webroot without needing any privilege escalation" {
	# Tier 1 of _publish_static_dir: an operator who chowned the webroot (or
	# symlinked it into shared/) needs no privilege escalation whatsoever.
	# SUDO_BIN is "" throughout this fixture, so tier 3 is unavailable and a
	# pre-existing destination rules out tier 2 -- success here can only mean
	# the plain-cp path handled it.
	#
	# (SUDO_BIN deliberately NOT sabotaged to prove that: it is global, so
	# pointing it at a bogus binary would break step 7's service restart and
	# fail this test for an unrelated reason.)
	mkdir -p "$NGINX_DST"
	seed_static_site
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "nginx root OK"
	[ -f "$NGINX_DST/assets/app.js" ]
}

@test "step 9: symlinked webroot (DEPLOYMENT.md §9) publishes as success — cp identical is not an error" {
	# Regression for the §9 one-time fix: once /var/www/<site> is a symlink
	# into shared/www/<site>, source and destination are the SAME directory,
	# and `cp -a src/. dst/` exits 1 with "are identical (not copied)".
	# Before Tier 0 existed, that cp failure was swallowed by the
	# unconditional `return 0` in tier 1, so every deploy after the symlink
	# fix logged a cp error while still claiming "nginx root OK". The
	# symlinked state is now a first-class supported state: publish reports
	# success and the copy is simply not attempted (nothing to copy — the
	# webroot IS the shared tree).
	mkdir -p "$SHARED_DST"
	ln -s "$SHARED_DST" "$NGINX_DST"
	seed_static_site
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "nginx root OK"
	# The cp identical error must never surface in the deploy log.
	assert_output_not_contains "are identical"
	# Content is served straight out of the shared tree through the symlink.
	[ -f "$NGINX_DST/index.html" ]
	[ -f "$NGINX_DST/assets/app.js" ]
}

@test "step 9: published webroot is world-readable for the nginx worker (regression RND-263)" {
	# RND-263 (2026-08-03): the static source is checked out under the
	# runtime user's umask (027 on prod), which turns git-tracked 100644
	# files into 0640 owned by the deploy user. cp -a preserves that mode
	# into the Nginx webroot; Nginx's worker runs as `nginx`, NOT in the
	# deploy user's group, so a 0640 webroot makes the whole homepage
	# HTTP 403. The publish step must normalise the webroot to o+rX.
	mkdir -p "$NGINX_DST"
	seed_static_site
	# Simulate the umask-027 checkout: files land 0640, dirs 0750.
	find "$DEPLOY_DIR/static_site/company_homepage" -type f -exec chmod 0640 {} +
	find "$DEPLOY_DIR/static_site/company_homepage" -type d -exec chmod 0750 {} +
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "nginx root OK"
	# Files must be world-readable (ls -ld column 8-10 = other perms).
	[ "$(ls -ld "$NGINX_DST/index.html" | cut -c8-10)" = "r--" ]
	[ "$(ls -ld "$NGINX_DST/assets/app.js" | cut -c8-10)" = "r--" ]
	# Directories must be world-searchable so Nginx can traverse them.
	[ "$(ls -ld "$NGINX_DST" | cut -c8-10)" = "r-x" ]
	[ "$(ls -ld "$NGINX_DST/assets" | cut -c8-10)" = "r-x" ]
}

@test "step 9: a real copy failure in a writable webroot is reported as WARN, not nginx root OK" {
	# Regression for tiers 1/2 returning 0 unconditionally: a genuine cp
	# failure (here: a read-only file blocking an overwrite) must propagate
	# as the WARN path, never as a false "nginx root OK". The destination
	# directory itself is writable, so the old tier-1 `cp; return 0` would
	# have swallowed this exact failure.
	mkdir -p "$NGINX_DST"
	printf 'locked\n' >"$NGINX_DST/index.html"
	chmod 000 "$NGINX_DST/index.html"
	seed_static_site
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "could not publish the homepage"
	assert_output_not_contains "nginx root OK"
}

# ---------------------------------------------------------------------
# Step 10 — Managed systemd units (issue #46 bug 3 fix)
#
# A new background-job unit (deploy/systemd/wecom-external-contact-
# reconcile.{service,timer}) sat in the repo, never installed on the host,
# because installing a NEW systemd unit was always a separate manual
# runbook step this CD pipeline never performed -- so the avatar-sync
# timer it drives never ran, for any tenant, ever. This step closes that
# gap for any unit explicitly listed in deploy/systemd/MANAGED_UNITS.
# ---------------------------------------------------------------------

@test "step 10: no MANAGED_UNITS manifest — step is a clean no-op" {
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "no deploy/systemd/MANAGED_UNITS manifest"
}

@test "step 10: SUDO_BIN unset — skips without attempting to write SYSTEMD_UNIT_DIR" {
	seed_managed_units
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "SUDO_BIN unset"
	[ ! -e "$SYSTEMD_UNIT_DIR/demo-job.service" ]
}

@test "step 10: installs both units, reloads once, and enables --now only the .timer" {
	export SUDO_BIN="sudo" # resolved via the mocked PATH (test_helper/mock_sudo.sh)
	seed_managed_units
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "installed/updated demo-job.service"
	assert_output_contains "installed/updated demo-job.timer"

	[ -f "$SYSTEMD_UNIT_DIR/demo-job.service" ]
	[ -f "$SYSTEMD_UNIT_DIR/demo-job.timer" ]

	# The oneshot .service is installed so its .timer can trigger it, but is
	# never enabled/started directly -- only the .timer is.
	grep -q "systemctl enable --now demo-job.timer" "$CMD_LOG"
	! grep -q "systemctl enable --now demo-job.service" "$CMD_LOG"

	# daemon-reload runs exactly once even though two units changed. Matched
	# anchored (not a bare substring grep): CMD_LOG has one line from the
	# sudo mock logging its own invocation ("sudo -n systemctl daemon-
	# reload") and a second from the execed systemctl mock logging its own
	# argv ("systemctl daemon-reload") -- a bare substring grep matches
	# both lines for what is really only one daemon-reload call.
	[ "$(grep -c '^systemctl daemon-reload$' "$CMD_LOG")" -eq 1 ]
}

@test "step 10: an already-installed, unchanged unit is left alone (no redundant cp/reload)" {
	export SUDO_BIN="sudo"
	seed_managed_units
	cp "$DEPLOY_DIR/deploy/systemd/demo-job.service" "$SYSTEMD_UNIT_DIR/demo-job.service"
	cp "$DEPLOY_DIR/deploy/systemd/demo-job.timer" "$SYSTEMD_UNIT_DIR/demo-job.timer"
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_not_contains "installed/updated demo-job"
	# Nothing changed, so daemon-reload must not run at all.
	! grep -q "systemctl daemon-reload" "$CMD_LOG"
	# The timer is still (re-)enabled --now every deploy -- enable --now is
	# idempotent and this is the only way a unit that was manually disabled
	# gets re-armed without a human noticing.
	grep -q "systemctl enable --now demo-job.timer" "$CMD_LOG"
}

@test "step 10: a changed unit is re-copied and triggers exactly one daemon-reload" {
	export SUDO_BIN="sudo"
	seed_managed_units
	printf 'stale on disk\n' >"$SYSTEMD_UNIT_DIR/demo-job.service"
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "installed/updated demo-job.service"
	[ "$(cat "$SYSTEMD_UNIT_DIR/demo-job.service")" != "stale on disk" ]
}

@test "step 10: sudo denied (no NOPASSWD grant) WARNs per unit but never fails or rolls back the deploy" {
	export SUDO_BIN="sudo"
	export MOCK_SUDO_MODE=deny
	seed_managed_units
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "could not install demo-job.service into"
	assert_output_contains "could not install demo-job.timer into"
	[ ! -e "$SYSTEMD_UNIT_DIR/demo-job.service" ]
	# The rollback record is still written -- a missing sudoers grant for
	# an auxiliary timer must never cost the next deploy its rollback target.
	[ "$(known_good)" = "$NEW_SHA" ]
}

@test "step 10: a manifest entry with no matching file on disk WARNs and does not abort the deploy" {
	export SUDO_BIN="sudo"
	seed_managed_units
	rm "$DEPLOY_DIR/deploy/systemd/demo-job.timer"
	run run_deploy
	[ "$status" -eq 0 ]
	assert_output_contains "Deploy complete"
	assert_output_contains "MANAGED_UNITS lists demo-job.timer but"
	assert_output_contains "does not exist"
	# The .service that DID exist still installs normally.
	[ -f "$SYSTEMD_UNIT_DIR/demo-job.service" ]
}
