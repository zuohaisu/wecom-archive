#!/usr/bin/env bats
# Tests for scripts/assert_scheduled_workloads.sh (GH-104).
#
# Repo-mode tests run the real script against a fixture deploy/systemd
# tree built fresh per test (never the real repository state, so these
# stay correct even as real units are added/reclassified). Server-mode
# tests point SYSTEMCTL_BIN/SYSTEMD_UNIT_DIR at fixtures/mocks — never a
# real systemd instance.

load test_helper/common

ASSERT_SCRIPT="$SCRIPTS_ROOT/assert_scheduled_workloads.sh"

setup() {
	TEST_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/assert-workloads-test.XXXXXX")"
	export TEST_TMPDIR
	DEPLOY_DIR="$TEST_TMPDIR/deploy_dir"
	mkdir -p "$DEPLOY_DIR/deploy/systemd" "$DEPLOY_DIR/backend/scripts" "$DEPLOY_DIR/scripts"
	export DEPLOY_DIR
	MANIFEST="$DEPLOY_DIR/deploy/systemd/WORKLOAD_MANIFEST"
	MANAGED_UNITS_FILE="$DEPLOY_DIR/deploy/systemd/MANAGED_UNITS"
	export MANIFEST MANAGED_UNITS_FILE
}

teardown() {
	rm -rf "$TEST_TMPDIR"
}

# ── Fixture helpers ─────────────────────────────────────────────────────

write_unit() {
	local name="$1" execstart="$2" workdir="${3:-/srv/apps/wecom-archive-365/current/backend}"
	cat >"$DEPLOY_DIR/deploy/systemd/$name" <<EOF
[Unit]
Description=fixture unit $name

[Service]
Type=oneshot
WorkingDirectory=$workdir
ExecStart=$execstart

[Install]
WantedBy=multi-user.target
EOF
}

write_trigger() {
	local name="$1" unit="$2"
	local ext="${name##*.}"
	{
		echo "[Unit]"
		echo "Description=fixture trigger $name"
		echo
		if [ "$ext" = "timer" ]; then
			echo "[Timer]"
			echo "OnCalendar=*:0/5"
		else
			echo "[Path]"
			echo "PathChanged=/tmp/fixture.trigger"
		fi
		echo "Unit=$unit"
		echo
		echo "[Install]"
		echo "WantedBy=timers.target"
	} >"$DEPLOY_DIR/deploy/systemd/$name"
}

run_assert() {
	DEPLOY_DIR="$DEPLOY_DIR" MANIFEST="$MANIFEST" MANAGED_UNITS_FILE="$MANAGED_UNITS_FILE" \
		bash "$ASSERT_SCRIPT" "$@"
}

# ── Repo-mode: happy path ────────────────────────────────────────────────

@test "a well-formed required service+timer pair passes and requires MANAGED_UNITS listing" {
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/demo.py"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	run run_assert
	[ "$status" -eq 0 ]
	assert_output_contains "all checks passed"
}

@test "repo mode resolves entrypoints under an arbitrary current checkout root" {
	write_unit "demo-job.service" "/srv/example-app/current/backend/.venv/bin/python scripts/demo.py" "/srv/example-app/current/backend"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	run run_assert
	[ "$status" -eq 0 ]
	assert_output_contains "all checks passed"
}

@test "canonical manifest and MANAGED_UNITS parse and agree (real repo files)" {
	# Empty (not unset) DEPLOY_DIR/MANIFEST/MANAGED_UNITS_FILE still hit the
	# script's own ${VAR:-default} fallback, which resolves against the
	# real checkout -- setup() above exported these pointing at the
	# per-test fixture tree, so they must be explicitly cleared here.
	run env DEPLOY_DIR= MANIFEST= MANAGED_UNITS_FILE= bash "$ASSERT_SCRIPT"
	[ "$status" -eq 0 ]
	assert_output_contains "all checks passed"
}

# ── Repo-mode: required-unit correctness ────────────────────────────────

@test "a required unit whose ExecStart references a missing script fails" {
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/does_not_exist.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "ExecStart references missing entrypoint"
}

@test "a manifest row claiming repo_status=present for a missing file fails" {
	cat >"$MANIFEST" <<'EOF'
ghost-job.service|required|present|true|false|oneshot|false|fixture
EOF
	: >"$MANAGED_UNITS_FILE"
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "does not exist"
}

@test "required=true but missing from MANAGED_UNITS fails" {
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/demo.py"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	: >"$MANAGED_UNITS_FILE"
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "absent from MANAGED_UNITS"
}

# ── Repo-mode: deferred/manual/deprecated can never sneak into auto-enable ──

@test "a deferred unit listed in MANAGED_UNITS fails (could be silently auto-enabled)" {
	write_unit "risky-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/risky.py"
	touch "$DEPLOY_DIR/backend/scripts/risky.py"
	write_trigger "risky-job.timer" "risky-job.service"
	cat >"$MANIFEST" <<'EOF'
risky-job.service|deferred|present|false|false|oneshot|false|fixture
risky-job.timer|deferred|present|false|false|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
risky-job.service
risky-job.timer
EOF
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "could be silently auto-installed/enabled"
}

@test "a destructive unit accidentally flagged auto_install=true fails" {
	write_unit "destroy-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/destroy.py"
	touch "$DEPLOY_DIR/backend/scripts/destroy.py"
	write_trigger "destroy-job.timer" "destroy-job.service"
	cat >"$MANIFEST" <<'EOF'
destroy-job.service|deferred|present|true|false|oneshot|true|fixture (mis-authored)
destroy-job.timer|deferred|present|true|true|timer|true|fixture (mis-authored)
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
destroy-job.service
destroy-job.timer
EOF
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "destructive=true units must never be auto_install/auto_enable=true"
}

@test "MANAGED_UNITS entry with no manifest row fails" {
	write_unit "orphan-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/orphan.py"
	touch "$DEPLOY_DIR/backend/scripts/orphan.py"
	: >"$MANIFEST"
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
orphan-job.service
EOF
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "no WORKLOAD_MANIFEST row"
}

@test "a .timer with auto_install=true but auto_enable=false fails self-consistency" {
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/demo.py"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|false|timer|false|fixture (bug: should be auto_enable=true)
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "must also have auto_enable=true"
}

@test "repo_status=absent requires auto_install/auto_enable=false" {
	cat >"$MANIFEST" <<'EOF'
<future-job>|required|absent|true|false|timer|false|fixture (bug)
EOF
	: >"$MANAGED_UNITS_FILE"
	run run_assert
	[ "$status" -eq 1 ]
	assert_output_contains "repo_status=absent must have auto_install=false"
}

# ── Server-mode ──────────────────────────────────────────────────────────

mock_systemctl_always() {
	local enabled_word="$1" active_word="$2"
	cat >"$TEST_TMPDIR/systemctl" <<EOF
#!/usr/bin/env bash
case "\$1" in
  is-enabled) echo "$enabled_word"; [ "$enabled_word" = "enabled" ] && exit 0 || exit 1 ;;
  is-active) echo "$active_word"; [ "$active_word" = "active" ] && exit 0 || exit 3 ;;
esac
EOF
	chmod +x "$TEST_TMPDIR/systemctl"
}

@test "server mode: required trigger installed+enabled+active passes" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/demo.py"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	touch "$TEST_TMPDIR/unitdir/demo-job.service" "$TEST_TMPDIR/unitdir/demo-job.timer"
	mock_systemctl_always enabled active
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 0 ]
	assert_output_contains "all checks passed"
}

@test "server mode: required trigger missing from server fails" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/demo.py"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	mock_systemctl_always enabled active
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 1 ]
	assert_output_contains "required but not installed"
}

@test "server mode: required trigger disabled fails" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "demo-job.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/demo.py"
	touch "$DEPLOY_DIR/backend/scripts/demo.py"
	write_trigger "demo-job.timer" "demo-job.service"
	cat >"$MANIFEST" <<'EOF'
demo-job.service|required|present|true|false|oneshot|false|fixture
demo-job.timer|required|present|true|true|timer|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
demo-job.service
demo-job.timer
EOF
	touch "$TEST_TMPDIR/unitdir/demo-job.service" "$TEST_TMPDIR/unitdir/demo-job.timer"
	mock_systemctl_always disabled inactive
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 1 ]
	assert_output_contains "is not enabled"
}

@test "server mode: static-helper unit is never required to be enabled" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "helper@.service" "/srv/apps/wecom-archive-365/current/scripts/notify.sh"
	touch "$DEPLOY_DIR/scripts/notify.sh"
	cat >"$MANIFEST" <<'EOF'
helper@.service|static-helper|present|true|false|oneshot-on-demand|false|fixture
EOF
	cat >"$MANAGED_UNITS_FILE" <<'EOF'
helper@.service
EOF
	touch "$TEST_TMPDIR/unitdir/helper@.service"
	mock_systemctl_always disabled inactive
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 0 ]
}

@test "server mode: a non-destructive deferred unit found enabled only WARNs" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "reachability-like.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/reach.py"
	touch "$DEPLOY_DIR/backend/scripts/reach.py"
	write_trigger "reachability-like.timer" "reachability-like.service"
	cat >"$MANIFEST" <<'EOF'
reachability-like.service|deferred|present|false|false|oneshot|false|fixture
reachability-like.timer|deferred|present|false|false|timer|false|fixture
EOF
	: >"$MANAGED_UNITS_FILE"
	touch "$TEST_TMPDIR/unitdir/reachability-like.service" "$TEST_TMPDIR/unitdir/reachability-like.timer"
	mock_systemctl_always enabled active
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 0 ]
	assert_output_contains "confirm this was an intentional, approved activation"
}

@test "server mode: a destructive deferred unit found enabled in production FAILs" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "purge-like.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/purge.py"
	touch "$DEPLOY_DIR/backend/scripts/purge.py"
	write_trigger "purge-like.timer" "purge-like.service"
	cat >"$MANIFEST" <<'EOF'
purge-like.service|deferred|present|false|false|oneshot|true|fixture
purge-like.timer|deferred|present|false|false|timer|true|fixture
EOF
	: >"$MANAGED_UNITS_FILE"
	touch "$TEST_TMPDIR/unitdir/purge-like.service" "$TEST_TMPDIR/unitdir/purge-like.timer"
	mock_systemctl_always enabled active
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 1 ]
	assert_output_contains "must be disabled"
}

@test "server mode: a deprecated unit re-enabled in production FAILs even if not destructive" {
	mkdir -p "$TEST_TMPDIR/unitdir"
	write_unit "old-relay.service" "/srv/apps/wecom-archive-365/current/backend/.venv/bin/python scripts/relay.py"
	touch "$DEPLOY_DIR/backend/scripts/relay.py"
	write_trigger "old-relay.timer" "old-relay.service"
	cat >"$MANIFEST" <<'EOF'
old-relay.service|deprecated|present|false|false|oneshot|false|fixture
old-relay.timer|deprecated|present|false|false|timer|false|fixture
EOF
	: >"$MANAGED_UNITS_FILE"
	touch "$TEST_TMPDIR/unitdir/old-relay.service" "$TEST_TMPDIR/unitdir/old-relay.timer"
	mock_systemctl_always enabled active
	export SYSTEMCTL_BIN="$TEST_TMPDIR/systemctl" SYSTEMD_UNIT_DIR="$TEST_TMPDIR/unitdir"
	run run_assert --server
	[ "$status" -eq 1 ]
	assert_output_contains "must be disabled"
}
