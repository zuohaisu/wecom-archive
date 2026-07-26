#!/usr/bin/env bash
#
# deploy_server.sh — Safe push-to-deploy for wecom-archive-365
#
# Run this script on the production ECS server to pull the latest code
# from GitHub main, install dependencies, run Alembic migrations, verify
# the database actually reached the repository's migration head, restart
# the service, and gate success on a real readiness check — with an
# automatic code rollback to the previous commit if the restart/readiness
# gate fails (RND-227).
#
# Intended to be triggered by a GitHub Actions workflow (.github/workflows/deploy.yml).
#
# ── Required GitHub Secrets ────────────────────────────────────────────────
#   DEPLOY_HOST       SSH host (IP or domain of the ECS server)
#   DEPLOY_USER       SSH user (must be the non-root runtime user, e.g. wecomarchive)
#   DEPLOY_SSH_KEY    Private SSH key used to authenticate on the server
#   DEPLOY_PORT       SSH port (typically 22)
#
# ── Server (sudo) Prerequisites ────────────────────────────────────────────
#   The runtime user (e.g. wecomarchive) must be able to run
#       sudo systemctl restart wecom-archive-365.service
#   without a password prompt.  Add a sudoers drop-in file:
#
#       /etc/sudoers.d/wecomarchive
#       ─────────────────────────────
#       wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl restart wecom-archive-365.service
#
# ── First-Time Server Setup ────────────────────────────────────────────────
#   1. Install git, python3, python3-venv, pip, and curl.
#   2. Create the runtime user (if not exists):
#          sudo adduser wecomarchive --disabled-password --gecos ""
#   3. Clone the repository:
#          sudo mkdir -p /srv/apps
#          sudo git clone git@github.com:zuohaisu/wecom-archive-365.git /srv/apps/wecom-archive-365/current
#          sudo chown -R wecomarchive:wecomarchive /srv/apps/wecom-archive-365
#   4. Create the Python virtual environment:
#          sudo -u wecomarchive bash -c '
#              cd /srv/apps/wecom-archive-365/current/backend
#              python3 -m venv .venv
#              source .venv/bin/activate
#              pip install -r requirements.txt
#          '
#   5. Create backend/.env (DATABASE_URL, etc. — see docs/DEPLOYMENT.md) —
#      this script sources it to give `alembic` the same DATABASE_URL the
#      running service uses.
#   6. Install the systemd service unit (not included in this repo — out of scope).
#   7. Ensure the runtime user has passwordless sudo for `systemctl restart wecom-archive-365.service`
#      as described in the sudoers section above.
#   8. Verify the deployment manually once:
#          sudo -u wecomarchive bash scripts/deploy_server.sh
#
# ── Behavior (RND-227) ──────────────────────────────────────────────────────
#   - Fails fast on any error (set -euo pipefail).
#   - Requires a clean working tree before touching anything (guards the
#     `git checkout` rollback below against ever discarding un-pushed,
#     unreviewed local changes).
#   - When EXPECTED_SHA is set (CI passes the exact commit it tested via
#     the SSH step's env), deploys EXACTLY that commit rather than a
#     floating `origin/main` — closes a race where a second push landing
#     mid-deploy could otherwise get pulled in untested. The CI workflow
#     itself now does the fetch/checkout to EXPECTED_SHA BEFORE invoking
#     this script (see .github/workflows/deploy.yml), so the very
#     deploy that changes this script's own logic already runs the new
#     logic — this script also carries its own EXPECTED_SHA-aware
#     fetch/checkout as a fallback for direct/manual invocation. Falls
#     back to `git pull --ff-only origin main` when EXPECTED_SHA is
#     unset entirely (manual/first-run use, no pinning at all).
#   - A non-blocking flock on $DEPLOY_STATE_DIR/deploy.lock refuses to
#     run if another invocation is already in progress, rather than
#     letting two overlapping runs race each other's PREV_SHA capture,
#     restart, and last-known-good bookkeeping.
#   - Tracks the last commit THIS SCRIPT has itself proven healthy in
#     $DEPLOY_STATE_DIR/last_known_good_sha (outside the git working
#     tree, so it never trips the clean-tree guard) and rolls back to
#     THAT commit, not merely whatever HEAD happened to be a moment ago.
#     Failing to persist this record after an otherwise-successful
#     forward deploy is itself treated as a deploy failure (non-zero
#     exit) — see docs/DEPLOYMENT.md §7.6.1 "Rollback target".
#   - Installs/updates Python dependencies via pip.
#   - Runs compileall to catch syntax / import errors before restart.
#   - Runs `alembic upgrade head` non-interactively using the SAME venv
#     and DATABASE_URL (sourced from backend/.env) as the running service.
#   - Independently verifies the database's current revision(s) equal the
#     repository's migration head(s) (scripts/verify_alembic_head.py) —
#     `alembic upgrade head` exiting 0 alone is not trusted.
#   - Only if both of the above succeed: restarts the systemd service.
#   - Gates success on a real readiness check (/health/ready — DB
#     connectivity + schema revision, not a static "ok"), retried a
#     bounded number of times to absorb warm-up / proxy jitter.
#   - Migration/verification failure -> NO restart; the old process keeps
#     running untouched; the working tree is restored to the previous
#     commit so the disk isn't left mid-deploy.
#   - Restart/readiness failure (after a successful, verified migration)
#     -> automatic rollback: code restored to the previous commit,
#     dependencies reinstalled, service restarted, rollback itself
#     health-gated. The ORIGINAL deploy always exits non-zero regardless
#     of whether the rollback succeeded.
#   - Never runs `alembic downgrade` — a forward migration is not undone
#     by a code rollback. See docs/DEPLOYMENT.md "Code Rollback vs
#     Database Rollback".
#   - Never prints DATABASE_URL, its password, or a full connection
#     string; captured command output is redacted before printing as
#     defense in depth.
#   - Deploys the static homepage only after the backend is confirmed
#     healthy, so a successful static copy can never mask a backend
#     deploy failure.
#   - Exits non-zero on any failure — the workflow will report it.
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration (env-overridable; defaults match production) ────────────
DEPLOY_DIR="${DEPLOY_DIR:-/srv/apps/wecom-archive-365/current}"
SERVICE="${SERVICE:-wecom-archive-365.service}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"

# QA-01 fix: when the CI workflow sets EXPECTED_SHA (the exact commit it
# just tested — pass `EXPECTED_SHA=<github.sha>` via the SSH step), this
# script deploys EXACTLY that commit instead of whatever `origin/main`
# happens to point at by the time this SSH step runs. Without this, two
# rapid pushes to main can race: run A's CI tests commit X, but by the
# time run A's deploy step SSHes in and pulls, `origin/main` may already
# be at commit Y (pushed after X, possibly by a run whose own CI hasn't
# finished or even failed) — `git pull --ff-only` would silently deploy
# Y, which THIS run never tested. Left unset (e.g. the documented manual
# first-run), falls back to the original floating `git pull --ff-only`.
EXPECTED_SHA="${EXPECTED_SHA:-}"

# QA-04 fix: persisted outside the git working tree (sibling to the
# existing `shared/www` convention — see docs/DEPLOYMENT.md §1) so it is
# never seen as an uncommitted change by the clean-tree guard below, and
# survives across separate deploy runs. Records the last commit this
# script itself PROVED healthy (full forward success, or a successful
# rollback) — the rollback target in _rollback_and_restart_old below
# uses this, not just "whatever HEAD happened to be a moment ago", which
# could itself be a commit an earlier, still-unresolved failure left
# behind in an unverified state.
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-/srv/apps/wecom-archive-365/shared/deploy_state}"
LAST_KNOWN_GOOD_FILE="$DEPLOY_STATE_DIR/last_known_good_sha"

# Command locations — overridable so tests can point these at mocks
# without needing real git/sudo/systemctl/curl/python on the test host.
GIT_BIN="${GIT_BIN:-git}"
SUDO_BIN="${SUDO_BIN-sudo}"                     # set to "" to skip sudo entirely
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"
CURL_BIN="${CURL_BIN:-curl}"
PYTHON_BIN="${PYTHON_BIN:-python}"
FLOCK_BIN="${FLOCK_BIN:-flock}"
MV_BIN="${MV_BIN:-mv}"

# Health gate endpoints. Internal uses /health/ready — the authoritative,
# localhost, real readiness check (DB + schema revision — see
# backend/app/main.py) this script gates the restart/rollback decision
# on. Public re-uses the compat-shaped /health (same checks) to confirm
# the request actually makes it through Nginx/DNS/TLS end-to-end; see
# the P0-D note below on why a public-only failure does not trigger a
# code rollback.
INTERNAL_HEALTH="${INTERNAL_HEALTH:-http://127.0.0.1:8035/health/ready}"
# ARCHIVE_DOMAIN — the public hostname this deployment serves the archive
# app on. Set the real value via backend/.env (already sourced below) or
# the calling shell's environment; PUBLIC_HEALTH can also be set directly
# to override independently of ARCHIVE_DOMAIN.
ARCHIVE_DOMAIN="${ARCHIVE_DOMAIN:-archive.example.com}"
PUBLIC_HEALTH="${PUBLIC_HEALTH:-https://${ARCHIVE_DOMAIN}/health}"

# P0-D: retry/timeout knobs, each a single named variable (no
# per-callsite hardcoding) shared by the internal, public, and
# post-rollback readiness checks.
HEALTH_RETRIES="${HEALTH_RETRIES:-10}"
HEALTH_RETRY_INTERVAL_SECONDS="${HEALTH_RETRY_INTERVAL_SECONDS:-2}"
HEALTH_CURL_TIMEOUT_SECONDS="${HEALTH_CURL_TIMEOUT_SECONDS:-5}"

# QA round 3 fix: without this, two overlapping invocations (e.g. a
# manual SSH run overlapping with a CI-triggered one, or any other
# out-of-band double-trigger GitHub's own job concurrency cannot see)
# could interleave — both capture the same PREV_SHA, both pull/restart,
# and whichever finishes last "wins" the last-known-good record even if
# it was not actually the last one to legitimately succeed, leaving the
# on-disk code and the recorded rollback target inconsistent. `flock -n`
# (non-blocking) makes a second concurrent invocation fail immediately
# with a clear message instead of racing. GitHub Actions' own job-level
# `concurrency:` group (.github/workflows/deploy.yml) already serializes
# CI-triggered deploys end-to-end; this is defense-in-depth for
# invocations GitHub cannot serialize.
#
# QA round 3 finding: the CI-driven path's checkout (git status/fetch/
# checkout to EXPECTED_SHA) runs in the WORKFLOW'S inline SSH script,
# BEFORE this script is even invoked (see .github/workflows/deploy.yml
# and the EXPECTED_SHA comment above) -- so this lock, if acquired only
# HERE, would be acquired AFTER that checkout already mutated the
# working tree, leaving the exact race it exists to prevent: a
# concurrent manual deploy already mid-flight would see its checkout
# silently swapped out from under it. DEPLOY_LOCK_ALREADY_HELD=1 is the
# signal that the CALLER (the workflow's inline script) already
# acquired this same lock, on this same fd 9, BEFORE doing ITS checkout
# -- since `bash scripts/deploy_server.sh` is a child process launched
# from within that same shell session, it inherits the caller's open
# fd 9 (and the flock that goes with it) automatically; this script
# must NOT re-open fd 9 in that case, which would create a distinct
# open file description racing against its own parent's lock (a
# guaranteed self-deadlock/false "already in progress"). Only the
# direct/manual invocation path (no wrapper, no inherited lock) takes
# its own lock here, and does so before ANY guard/checkout/mutation.
DEPLOY_LOCK_ALREADY_HELD="${DEPLOY_LOCK_ALREADY_HELD:-0}"
LOCK_FILE="$DEPLOY_STATE_DIR/deploy.lock"
if [ "$DEPLOY_LOCK_ALREADY_HELD" != "1" ]; then
    if ! mkdir -p "$DEPLOY_STATE_DIR" 2>/dev/null; then
        echo "ERROR: could not create $DEPLOY_STATE_DIR for the deploy lock." >&2
        exit 1
    fi
    exec 9>"$LOCK_FILE"
    if ! "$FLOCK_BIN" -n 9; then
        echo "ERROR: another deploy is already in progress (lock held on $LOCK_FILE). Refusing to run concurrently." >&2
        exit 1
    fi
fi
# end DEPLOY_LOCK_ALREADY_HELD guard

echo "=== Deploying wecom-archive-365 ==="
DEPLOY_TIME=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
echo "  Time:   $DEPLOY_TIME"

# ── Helpers ─────────────────────────────────────────────────────────────

# Strips any userinfo (user:password@) from captured command output
# before it is printed. Defense in depth: nothing in this script's own
# flow should ever emit a connection string, but a driver's own error
# text is outside our control.
_redact() {
    sed -E 's#://[^:/@[:space:]]+:[^@[:space:]]*@#://REDACTED@#g'
}

_git() { "$GIT_BIN" "$@"; }

_systemctl_restart() {
    if [ -n "$SUDO_BIN" ]; then
        "$SUDO_BIN" "$SYSTEMCTL_BIN" restart "$1"
    else
        "$SYSTEMCTL_BIN" restart "$1"
    fi
}

_systemctl_is_active() {
    "$SYSTEMCTL_BIN" is-active "$1"
}

_install_deps() {
    "$PYTHON_BIN" -m pip install -r requirements.txt --quiet
}

# _record_known_good <sha> — called only after this script has itself
# proven <sha> healthy (end-to-end forward success, or a successful
# rollback's own re-check). Returns non-zero on a persist failure — QA
# round 2 flagged the original version (which only warned and always
# returned success) as a real gap: an otherwise-fully-successful deploy
# that cannot durably record its own rollback point is not actually
# fully safe, since the NEXT deploy's rollback target depends on this
# file existing and being current. Callers decide severity: the forward
# end-of-script call site below treats this as fatal (the deploy's whole
# point includes leaving behind a trustworthy rollback record); the
# rollback call site does not, since the overall deploy already exits
# non-zero there regardless of this file.
# QA round 3 fix: the previous version's `mv -f` (the final, atomic-
# rename step of the write) ran as a bare statement whose own exit
# status was never checked -- the function unconditionally `return 0`'d
# right after it regardless of whether the rename actually succeeded, so
# a failure there (e.g. the destination path colliding with a directory)
# was silently swallowed and the deploy still reported "Deploy complete"
# with no durable, correct record. Every step -- mkdir, temp-file write,
# AND the rename -- is now individually checked; only a fully-succeeded
# atomic write returns 0. Uses MV_BIN (like the other *_BIN overrides)
# so this exact failure mode is directly testable without relying on
# filesystem-permission tricks to isolate the rename step from the
# temp-file write step, which share the same parent directory.
_record_known_good() {
    local sha="$1"
    if ! mkdir -p "$DEPLOY_STATE_DIR" 2>/dev/null; then
        echo "  → ERROR: could not persist last-known-good SHA (mkdir $DEPLOY_STATE_DIR failed)." >&2
        return 1
    fi
    if ! echo "$sha" >"$LAST_KNOWN_GOOD_FILE.tmp" 2>/dev/null; then
        echo "  → ERROR: could not persist last-known-good SHA (write to $LAST_KNOWN_GOOD_FILE.tmp failed)." >&2
        return 1
    fi
    if ! "$MV_BIN" -f "$LAST_KNOWN_GOOD_FILE.tmp" "$LAST_KNOWN_GOOD_FILE" 2>/dev/null; then
        echo "  → ERROR: could not persist last-known-good SHA (rename to $LAST_KNOWN_GOOD_FILE failed)." >&2
        return 1
    fi
    return 0
}

# _wait_for_health <label> <url> <retries> <interval-seconds>
# Prints one line per attempt (status only — never response body on
# failure) and returns 0 the first time the endpoint responds 2xx,
# non-zero once every attempt is exhausted. A single flaky attempt
# (nginx/DNS/TLS/service warm-up) must never fail the whole deploy.
_wait_for_health() {
    local label="$1" url="$2" retries="$3" interval="$4" attempt
    echo "  → $label: $url (up to $retries attempts, ${interval}s apart)"
    for attempt in $(seq 1 "$retries"); do
        if "$CURL_BIN" -fsS --max-time "$HEALTH_CURL_TIMEOUT_SECONDS" "$url" >/dev/null 2>&1; then
            echo "    attempt $attempt/$retries — OK"
            return 0
        fi
        echo "    attempt $attempt/$retries — not ready yet"
        if [ "$attempt" -lt "$retries" ]; then
            sleep "$interval"
        fi
    done
    echo "    all $retries attempts failed" >&2
    return 1
}

# Pre-restart failure (pull/install/compile/migration/revision-verify):
# the running process was never touched, so there is nothing to restart
# or health-check — only the on-disk working tree needs restoring so the
# server isn't left holding a half-deployed commit.
_restore_worktree_only() {
    echo "  → Restoring working tree to previous commit ($PREV_SHA); service left untouched." >&2
    if _git checkout -B "$GIT_BRANCH" "$PREV_SHA" >/dev/null 2>&1; then
        echo "  → Working tree restored to $PREV_SHA." >&2
    else
        echo "  → WARNING: failed to restore working tree to $PREV_SHA — manual cleanup required." >&2
    fi
}

# Post-migration failure (restart or readiness gate failed, but the
# migration itself was already verified against repository head): full
# code rollback. Never touches the database (no alembic downgrade — see
# docs/DEPLOYMENT.md). Every step is individually guarded so a failure
# partway through still reports a clear result instead of aborting
# silently under `set -e`. Always returns 0 (its own outcome is reported
# via stdout/stderr) — the caller is responsible for the non-zero exit.
_rollback_and_restart_old() {
    local reason="$1"
    # QA-04 fix: roll back to the last commit THIS SCRIPT has actually
    # proven healthy, not just whatever HEAD happened to be immediately
    # before this run's pull. Normally these are the same commit; they
    # only diverge if an earlier deploy's own rollback was left in a
    # FAILED / manually-unresolved state, in which case PREV_SHA could
    # itself be unverified — falls back to PREV_SHA when no persisted
    # state exists yet (e.g. the first deploy after adopting RND-227).
    local rollback_target="$PREV_SHA"
    if [ -f "$LAST_KNOWN_GOOD_FILE" ]; then
        rollback_target="$(cat "$LAST_KNOWN_GOOD_FILE")"
    fi

    echo "=== Rolling back: $reason ===" >&2
    echo "  → Restoring code to last known-good commit ($rollback_target) …" >&2
    if ! _git checkout -B "$GIT_BRANCH" "$rollback_target" >/dev/null 2>&1; then
        echo "ROLLBACK RESULT: FAILED — could not restore working tree to $rollback_target. Manual intervention required immediately." >&2
        return 0
    fi

    echo "  → Reinstalling dependencies for the rolled-back commit …" >&2
    if ! _install_deps; then
        echo "ROLLBACK RESULT: FAILED — dependency reinstall failed for $rollback_target. Manual intervention required immediately." >&2
        return 0
    fi

    echo "  → Restarting service on the rolled-back commit …" >&2
    if ! _systemctl_restart "$SERVICE"; then
        echo "ROLLBACK RESULT: FAILED — systemctl restart failed for $rollback_target. Manual intervention required immediately." >&2
        return 0
    fi

    if ! _wait_for_health "Rollback readiness" "$INTERNAL_HEALTH" "$HEALTH_RETRIES" "$HEALTH_RETRY_INTERVAL_SECONDS"; then
        echo "ROLLBACK RESULT: FAILED — $rollback_target restarted but did not become healthy. Manual intervention required immediately." >&2
        return 0
    fi

    # Non-fatal here (unlike the forward-success call site): the overall
    # deploy already exits non-zero regardless of this file, so a
    # persist failure is logged but does not change that outcome or
    # skip the ROLLBACK RESULT line below.
    _record_known_good "$rollback_target" || true
    echo "ROLLBACK RESULT: SUCCEEDED — service is healthy again on known-good commit $rollback_target." >&2
    return 0
}

# ── 1. Pull latest code (clean-tree guarded) ───────────────────────────────
echo "[1/8] Pulling latest code from $GIT_REMOTE/$GIT_BRANCH …"

if [ ! -d "$DEPLOY_DIR" ]; then
    echo "ERROR: Deploy directory $DEPLOY_DIR does not exist." >&2
    exit 1
fi
cd "$DEPLOY_DIR"

# Guard: verify .git is readable/writable by the current user.
# If a previous git operation ran as root (e.g. manual debug pull),
# .git/index can end up owned by root and break the next deploy.
# Fail fast with a clear diagnostic instead of a cryptic EACCES.
if [ ! -r ".git/index" ] || [ ! -w ".git/index" ]; then
    echo "ERROR: .git/index is not accessible by user $(whoami)." >&2
    echo "  Run the following on the server to fix:" >&2
    echo "    sudo chown wecomarchive:wecomarchive $DEPLOY_DIR/.git/index" >&2
    exit 1
fi

# Guard: this script uses `git checkout -B "$GIT_BRANCH" "$PREV_SHA"` for
# rollback below. That is only safe to run unconditionally (never
# clobbering an operator's unreviewed local edit) if tracked files are
# known-unchanged — enforce that here by checking for modifications to
# tracked files only. Untracked files (.env.bak.*, qn-py-sdk/, etc.) do
# not block deployment (git pull --ff-only will still refuse to
# overwrite one that conflicts with an incoming tracked file).
if [ -n "$(_git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: Working tree at $DEPLOY_DIR has modified tracked files." >&2
    echo "  This script will not proceed while tracked files are locally" >&2
    echo "  modified (git status --short --untracked-files=no):" >&2
    _git status --short --untracked-files=no >&2
    echo "  Clean up or commit, then re-run." >&2
    exit 1
fi

# Non-blocking warning: report untracked files in the checkout so
# operators know they exist but let deployment proceed.
UNTRACKED_FILES="$(_git ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED_FILES" ]; then
    echo "NOTE: Untracked files exist in the deployment checkout (non-blocking):"
    printf '%s\n' "$UNTRACKED_FILES"
fi

# QA round 2 fix (bootstrap lag): PREV_SHA may already be supplied by
# the CALLER via the environment. The CI workflow's SSH step now
# performs its OWN clean-tree guard + fetch + fast-forward-checkout to
# EXPECTED_SHA BEFORE invoking this script (see .github/workflows/
# deploy.yml) — specifically so that invocation always runs the FRESH,
# just-checked-out copy of deploy_server.sh, never a stale on-disk
# version left over from the previous release. That is the only way to
# guarantee the deploy that SHIPS a change to this script's own logic
# also RUNS that logic, rather than only taking effect starting with the
# next deploy. When the caller has already done that, PREV_SHA is
# exported from the environment and HEAD already equals EXPECTED_SHA;
# this script must use the caller's PREV_SHA rather than re-deriving it
# from `git rev-parse HEAD` here, which would incorrectly capture the
# NEW commit as "previous". Falls back to self-capturing + self-pulling
# for manual/first-run use (no caller-side checkout) — see
# "First-Time Server Setup" above.
PREV_SHA="${PREV_SHA:-$(_git rev-parse HEAD)}"
echo "  Previous commit: $PREV_SHA"

CURRENT_SHA=$(_git rev-parse HEAD)
if [ -n "$EXPECTED_SHA" ] && [ "$CURRENT_SHA" = "$EXPECTED_SHA" ]; then
    echo "  Already at EXPECTED_SHA=$EXPECTED_SHA (checked out by the caller) — nothing to pull."
elif [ -n "$EXPECTED_SHA" ]; then
    # QA-01 fix: deploy exactly the commit CI tested, not a floating
    # `origin/main` that may have moved again since this run's own CI
    # gate passed. (Reached when this script is invoked directly, e.g.
    # manually, with EXPECTED_SHA set but without the caller having
    # already checked it out.)
    echo "  Pinned to EXPECTED_SHA=$EXPECTED_SHA (set by caller) …"
    _git fetch --quiet "$GIT_REMOTE" "$GIT_BRANCH"
    if ! _git merge-base --is-ancestor "$PREV_SHA" "$EXPECTED_SHA"; then
        echo "ERROR: EXPECTED_SHA ($EXPECTED_SHA) is not a fast-forward from the current commit ($PREV_SHA) — refusing to deploy a non-fast-forward or unknown commit." >&2
        exit 1
    fi
    if ! _git checkout -B "$GIT_BRANCH" "$EXPECTED_SHA" >/dev/null 2>&1; then
        echo "ERROR: could not check out EXPECTED_SHA ($EXPECTED_SHA)." >&2
        exit 1
    fi
else
    _git pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH"
fi

CURRENT_SHA=$(_git rev-parse HEAD)
echo "  Current commit:  $CURRENT_SHA"
if [ -n "$EXPECTED_SHA" ] && [ "$CURRENT_SHA" != "$EXPECTED_SHA" ]; then
    echo "ERROR: HEAD ($CURRENT_SHA) does not match EXPECTED_SHA ($EXPECTED_SHA) after checkout." >&2
    exit 1
fi
echo ""

cd backend

# ── 2. Install / update Python dependencies ────────────────────────────────
echo "[2/8] Installing Python dependencies …"
if [ ! -d .venv ]; then
    echo "ERROR: Virtual environment not found at $PWD/.venv. Run first-time setup." >&2
    _restore_worktree_only
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Load DATABASE_URL (and any other backend/.env vars) into this shell so
# `alembic upgrade head` below runs against the SAME database the
# systemd service uses — never echoed, never included in any log line.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
else
    echo "ERROR: backend/.env not found — required to supply DATABASE_URL for Alembic." >&2
    _restore_worktree_only
    exit 1
fi

if ! _install_deps; then
    echo "ERROR: Dependency installation failed." >&2
    _restore_worktree_only
    exit 1
fi

# ── 3. Compile-check Python code ───────────────────────────────────────────
echo "[3/8] Checking Python code compilation …"
if ! "$PYTHON_BIN" -m compileall app scripts; then
    echo "ERROR: Compile check failed." >&2
    _restore_worktree_only
    exit 1
fi

# ── 4. Alembic migration (P0-A) ─────────────────────────────────────────────
echo "[4/8] Running Alembic migrations (alembic upgrade head) …"
if ! "$PYTHON_BIN" -m alembic upgrade head 2>&1 | _redact; then
    echo "ERROR: Alembic migration failed. Service was NOT restarted; the old process is still running the old code." >&2
    _restore_worktree_only
    exit 1
fi

# ── 5. Verify DB revision == repository head (P0-B) ─────────────────────────
echo "[5/8] Verifying database revision matches repository head …"
if ! "$PYTHON_BIN" scripts/verify_alembic_head.py 2>&1 | _redact; then
    echo "ERROR: Database revision does not match repository head. Service was NOT restarted." >&2
    _restore_worktree_only
    exit 1
fi

# ── 6. Restart systemd service ──────────────────────────────────────────────
echo "[6/8] Restarting systemd service ($SERVICE) …"
if ! _systemctl_restart "$SERVICE"; then
    echo "ERROR: systemctl restart failed." >&2
    _rollback_and_restart_old "systemctl restart failed"
    exit 1
fi
if ! _systemctl_is_active "$SERVICE" >/dev/null 2>&1; then
    echo "ERROR: Service $SERVICE is not active after restart." >&2
    _rollback_and_restart_old "service not active after restart"
    exit 1
fi

# ── 7. Readiness health gate (P0-C / P0-D) ──────────────────────────────────
echo "[7/8] Readiness health gate …"
if ! _wait_for_health "Internal" "$INTERNAL_HEALTH" "$HEALTH_RETRIES" "$HEALTH_RETRY_INTERVAL_SECONDS"; then
    echo "ERROR: Internal readiness check failed after $HEALTH_RETRIES attempts." >&2
    _rollback_and_restart_old "internal readiness gate failed"
    exit 1
fi
# QA-03 fix: earlier draft made an extra, unguarded curl call here just
# to print the response body for operator visibility. Under `set -e`, a
# transient failure on THAT call (distinct from, and after, the retry
# loop above that already proved the endpoint healthy) aborted the
# script immediately -- skipping the rollback below entirely and
# leaving the broken new commit running with no rollback attempted.
# The call added no gating value (the retry loop already fully decided
# pass/fail), so it is removed rather than papered over with `|| true`:
# an operator who wants the current body can already get it from the
# troubleshooting commands in docs/DEPLOYMENT.md §7.8.
echo ""

if ! _wait_for_health "Public" "$PUBLIC_HEALTH" "$HEALTH_RETRIES" "$HEALTH_RETRY_INTERVAL_SECONDS"; then
    # Deliberately NOT rolling back here: the internal gate already
    # proved the application itself is healthy, so a public-only
    # failure points at the reverse proxy / DNS / TLS layer, which a
    # code rollback cannot fix. Still fails the deploy loudly so an
    # operator investigates Nginx/DNS/TLS on the host.
    echo "ERROR: Public readiness check failed after $HEALTH_RETRIES attempts. The application itself is healthy (internal gate passed) — investigate the reverse proxy / DNS / TLS layer, not the code." >&2
    exit 1
fi

# ── 8. Deploy static site + success ──────────────────────────────────────────
# Runs only after the backend is confirmed healthy above, so a
# successful static copy can never mask a backend deploy failure.
echo "[8/8] Deploying company homepage static files …"
STATIC_SRC="$DEPLOY_DIR/static_site/company_homepage"
# STATIC_SITE_DIR_NAME — the directory name (under shared/www and nginx's
# webroot) this deployment's static homepage is copied to. Set the real
# value via backend/.env or the calling shell's environment.
STATIC_SITE_DIR_NAME="${STATIC_SITE_DIR_NAME:-site}"
SHARED_DST="/srv/apps/wecom-archive-365/shared/www/$STATIC_SITE_DIR_NAME"
NGINX_DST="/var/www/$STATIC_SITE_DIR_NAME"

if [ -d "$STATIC_SRC" ]; then
    # Copy to shared (wecomarchive-owned) first
    cp "$STATIC_SRC/index.html" "$SHARED_DST/index.html"
    cp "$STATIC_SRC/style.css" "$SHARED_DST/style.css"
    echo "  → shared OK ($SHARED_DST)"

    # Then copy to nginx root (needs sudo)
    if [ -n "$SUDO_BIN" ]; then
        "$SUDO_BIN" cp "$SHARED_DST/index.html" "$NGINX_DST/index.html"
        "$SUDO_BIN" cp "$SHARED_DST/style.css" "$NGINX_DST/style.css"
    else
        cp "$SHARED_DST/index.html" "$NGINX_DST/index.html"
        cp "$SHARED_DST/style.css" "$NGINX_DST/style.css"
    fi
    echo "  → nginx root OK ($NGINX_DST)"
else
    echo "  WARN: static site source not found at $STATIC_SRC — skipping"
fi

if ! _record_known_good "$CURRENT_SHA"; then
    echo "ERROR: the service is healthy on $CURRENT_SHA, but the last-known-good rollback record could not be persisted — the next deploy could not reliably roll back if it fails. Treating this deploy as failed." >&2
    exit 1
fi
echo "=== Deploy complete ($PREV_SHA -> $CURRENT_SHA) ==="
