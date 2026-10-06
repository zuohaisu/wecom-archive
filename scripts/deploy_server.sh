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
#   GH-133: this script never invokes a package manager (no `dnf`, no
#   `apt-get`) and the runtime user does NOT need package-manager sudo of
#   any kind. ffmpeg is a HOST PREREQUISITE (see step 2 / _check_ffmpeg
#   below and docs/operations/deploy-sudoers.md) — deployment only checks
#   for it and fails fast with an actionable message if it is missing; an
#   operator/root installs it out of band (`sudo dnf install -y ffmpeg` on
#   the real Alibaba Cloud Linux 3 production host).
#
#   The runtime user (e.g. wecomarchive) must be able to run
#       sudo systemctl restart wecom-archive-365.service
#   without a password prompt.  Add a sudoers drop-in file:
#
#       /etc/sudoers.d/wecomarchive
#       ─────────────────────────────
#       wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl restart wecom-archive-365.service
#
#   Step 10 (sync deploy/systemd/MANAGED_UNITS) additionally needs, but
#   degrades gracefully with a WARN (never fails the deploy) if these are
#   absent — see that step's comment below:
#       wecomarchive ALL=(root) NOPASSWD: \
#         /usr/bin/cp /srv/apps/wecom-archive-365/current/deploy/systemd/*.service /etc/systemd/system/, \
#         /usr/bin/cp /srv/apps/wecom-archive-365/current/deploy/systemd/*.timer /etc/systemd/system/, \
#         /usr/bin/cp /srv/apps/wecom-archive-365/current/deploy/systemd/*.path /etc/systemd/system/, \
#         /usr/bin/systemctl daemon-reload, \
#         /usr/bin/systemctl enable --now wecom-*.timer, \
#         /usr/bin/systemctl enable --now wecom-*.path
#
#   The `wecom-*.timer`/`wecom-*.path` grant above is a deliberately kept
#   wildcard, not a per-unit exact list — see
#   docs/operations/deploy-sudoers.md "systemd wildcard decision" for why,
#   and for the boundary it does NOT cross (it cannot match `nginx.service`,
#   `sshd.service`, any `qiniu-*` unit, or any other non-`wecom-*` name).
#
#   GH-104 Follow-up B: MANAGED_UNITS also lists qiniu-ssl-renew-wildcard.*
#   (not wecom-*). This script's own sync logic has no prefix restriction,
#   but the `enable --now` grant above is a literal `wecom-*` glob and does
#   NOT cover it. That needs one additional, EXACT (never a `qiniu-*`
#   glob — see docs/operations/wildcard-ssl-renewal.md for why) grant:
#       wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now qiniu-ssl-renew-wildcard.timer
#   Until that grant exists, this one unit degrades to the same WARN every
#   other missing-sudoers unit gets — it does not block or roll back the
#   rest of this step. #129 already added and production-verified this
#   exact grant — GH-133 does not touch it.
#
#   VERIFIED-ON-PRODUCTION CAVEAT (as of 2026-08-02, re-verified 2026-08-02):
#   /etc/sudoers.d/wecom-archive-365 grants ONLY systemctl restart/status.
#   /etc/sudoers.d/wecomarchive (created 2026-07-28) additionally grants
#   `/usr/bin/mkdir -p /var/www/*` and `/usr/bin/cp <src> <dst>` (EXACTLY
#   two arguments — no flags, and both sides must match the two-glob
#   shape). It also grants bare `/usr/bin/dnf` and `/usr/bin/apt-get` —
#   GH-133 found neither is used by any deploy/bootstrap logic in this
#   repository (apt-get isn't even installed on this dnf-based host) and
#   its production migration runbook (docs/operations/deploy-sudoers.md)
#   has Ops remove both.
#
#   The cp grant looks broader than it is: `sudo cp -a src/. dst/` does NOT
#   match it (three arguments including -a, and `dst/` is a single segment
#   while the glob expects two). Production proved this twice — 2026-08-02
#   `sudo cp -a` fell through to the password lecture and killed the deploy.
#   Treat sudoers matching as EXACT: changing a flag, the trailing slash, or
#   the argument count silently changes whether the whitelist applies. Prefer
#   solutions that need no new binary and no new sudo grant — see step 9's
#   static-homepage copy for the worked example (`sudo -n` probing + graceful
#   WARN degradation, never a bare `sudo` that can block on a TTY-less SSH
#   session).
#
# ── First-Time Server Setup ────────────────────────────────────────────────
#   1. Install git, python3, python3-venv, pip, curl, and ffmpeg as root/
#      operator, using the host's real package manager (production is
#      Alibaba Cloud Linux 3 / dnf: `sudo dnf install -y ffmpeg`; adjust for
#      your actual distro). This is a one-time HOST bootstrap step, not a
#      deploy-user sudo grant — see step 2 in "Behavior" below and
#      docs/operations/deploy-sudoers.md.
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
#   5. Create backend/.env (DATABASE_URL, etc. — see docs/DEPLOYMENT.md).
#      This script reads only its deployment keys as data; it never executes
#      the file as shell code.
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
#     back to fetch -> resolve target -> preflight -> checkout when
#     EXPECTED_SHA is unset entirely (manual/first-run use, no pinning at all).
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
# finished or even failed) — a floating pull would silently deploy Y, which
# THIS run never tested. Left unset (e.g. the documented manual first-run),
# the script fetches, resolves, preflights, and checks out one fetched target.
EXPECTED_SHA="${EXPECTED_SHA:-}"

# By default the deployment keeps its existing checkout-local configuration.
# The controlled non-production wrapper overrides this with a separate,
# operator-managed EnvironmentFile so runtime secrets never enter the Git
# checkout or GitHub Actions environment.
DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-}"

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
FFMPEG_BIN="${FFMPEG_BIN:-ffmpeg}"

# Step 10 (sync deploy/systemd/MANAGED_UNITS) target directory — overridable
# so tests never write to a real /etc/systemd/system.
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"

# Health gate endpoints. Internal uses /health/ready — the authoritative,
# localhost, real readiness check (DB + schema revision — see
# backend/app/main.py) this script gates the restart/rollback decision
# on. Public re-uses the compat-shaped /health (same checks) to confirm
# the request actually makes it through Nginx/DNS/TLS end-to-end; see
# the P0-D note below on why a public-only failure does not trigger a
# code rollback.
INTERNAL_HEALTH="${INTERNAL_HEALTH:-http://127.0.0.1:8035/health/ready}"
# GH-161: hostname the internal readiness probes (forward AND post-rollback
# — see _wait_for_internal_health) present as their explicit Host header.
# Derived from ADMIN_DOMAIN once backend/.env is loaded; defaulted empty
# here so the probe helpers stay total even before that point, and
# overridable so the bats suite can pin the construction directly.
INTERNAL_PROBE_HOST="${INTERNAL_PROBE_HOST:-}"
# ARCHIVE_DOMAIN — the public hostname this deployment serves the archive
# app on. Set the real value via backend/.env (loaded as data below) or
# the calling shell's environment; PUBLIC_HEALTH can also be set directly
# to override independently of ARCHIVE_DOMAIN.
ARCHIVE_DOMAIN="${ARCHIVE_DOMAIN:-archive.example.com}"
# PUBLIC_HEALTH is intentionally NOT defaulted here — its default depends on
# ARCHIVE_DOMAIN, which may be set in backend/.env (sourced later). Setting
# it here would freeze the placeholder domain. The default is computed after
# .env is sourced below.

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

# Load only the deployment keys required by this script, as literal data.
#
# backend/.env is application configuration, not a trusted shell script. In
# particular, a pasted multi-line PEM must never become a command during CD.
# The application still owns all other settings through its service manager;
# this deploy path needs only the database URL, the platform hostname
# (ADMIN_DOMAIN — the Host the internal readiness probe must present in
# production, GH-161) and public/static destinations.
# Malformed non-assignment lines are ignored with their line numbers only so
# neither secrets nor raw configuration values enter the deploy log.
_load_deploy_environment() {
    local env_file="$1"
    local line raw key value
    local line_no=0
    local ignored_lines=""

    while IFS= read -r raw || [ -n "$raw" ]; do
        line_no=$((line_no + 1))
        line="${raw%$'\r'}"
        case "$line" in
            ""|\#*|[[:space:]]\#*)
                continue
                ;;
            [A-Za-z_][A-Za-z0-9_]*=*)
                key="${line%%=*}"
                value="${line#*=}"
                # Support the common single-line quoted dotenv form without
                # evaluating expansions, command substitutions, or escapes.
                case "$value" in
                    \"*\")
                        value="${value#\"}"
                        value="${value%\"}"
                        ;;
                    \'*\')
                        value="${value#\'}"
                        value="${value%\'}"
                        ;;
                esac
                case "$key" in
                    DATABASE_URL)
                        DATABASE_URL="$value"
                        export DATABASE_URL
                        ;;
                    ARCHIVE_DOMAIN)
                        ARCHIVE_DOMAIN="$value"
                        export ARCHIVE_DOMAIN
                        ;;
                    ADMIN_DOMAIN)
                        ADMIN_DOMAIN="$value"
                        export ADMIN_DOMAIN
                        ;;
                    PUBLIC_HEALTH)
                        PUBLIC_HEALTH="$value"
                        export PUBLIC_HEALTH
                        ;;
                    STATIC_SITE_DIR_NAME)
                        STATIC_SITE_DIR_NAME="$value"
                        export STATIC_SITE_DIR_NAME
                        ;;
                esac
                ;;
            *)
                ignored_lines="${ignored_lines}${ignored_lines:+, }${line_no}"
                ;;
        esac
    done < "$env_file"

    if [ -n "$ignored_lines" ]; then
        echo "WARN: ignored non-KEY=value configuration line(s) in $env_file at line(s): $ignored_lines." >&2
        printf '%s\n' '  Encode PEM line breaks as literal \n characters in one KEY=value line; no configuration content was executed.' >&2
    fi
}

_git() { "$GIT_BIN" "$@"; }

# shellcheck source=deploy_preflight.sh
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/deploy_preflight.sh"
export DEPLOY_PREFLIGHT_GIT_BIN="$GIT_BIN"

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

# _check_ffmpeg — ffmpeg is a HOST PREREQUISITE (GH-133), not something this
# deploy installs. It is only ever invoked at runtime by
# backend/app/voice_transcode.py, which already treats a missing/failing
# ffmpeg as a soft per-message degradation (returns None; the original
# media archival is unaffected) — so ffmpeg is not required for the
# service itself to start or be healthy. But letting a deploy silently
# succeed onto a host missing it would defer that discovery to a user's
# first voice-message playback, long after this deploy reported success.
# So deployment fails fast here instead, with a message an operator can
# act on directly, rather than installing it: doing so would need root
# package-manager access for the deploy user, which is exactly the
# bare-`dnf`/bare-`apt-get` privilege GH-133 removes (see
# docs/operations/deploy-sudoers.md). Bootstrapping/repairing ffmpeg on
# the host is a root/operator action (see "First-Time Server Setup"
# above), independent of this deploy user's own privileges.
_check_ffmpeg() {
    if command -v "$FFMPEG_BIN" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# _publish_static_dir <src_dir> <dst_dir> — mirror <src_dir>'s contents
# into <dst_dir>, trying the least-privileged route that works and
# returning non-zero (silently) if none does. Used by step 9 to push the
# company homepage into Nginx's webroot.
#
# Why the tiered probing instead of just `sudo cp`: this host's runtime
# user has NO passwordless sudo beyond `systemctl restart <service>`.
# Production proved that twice — `sudo apt-get` (2026-08-01) and then
# `sudo mkdir` (2026-08-02) both fell through to sudo's password lecture
# and killed the deploy. So sudo is attempted only with `-n`
# (non-interactive), which fails instantly on a TTY-less SSH session
# instead of blocking on a prompt, and its stderr is suppressed because
# the lecture is noise, not a diagnostic.
#
# `cp -a src/. dst/` (not rsync): rsync is not in first-time setup's
# package list, is absent on this host, and cannot be installed without
# the sudo grant that does not exist. Like rsync without --delete, this
# does not remove files that disappeared from the source.
_publish_static_dir() {
    _psd_src="$1"
    _psd_dst="$2"

    # Tier 0 — source and destination are already the same directory
    # (the operator symlinked the webroot into shared/, per
    # DEPLOYMENT.md §9). `cp -a src/. dst/` would exit 1 with
    # "are identical (not copied)" — not a failure, the content is
    # already in place. Treat the symlinked state as the first-class
    # supported state it is, not an error.
    if [ -e "$_psd_src" ] && [ -e "$_psd_dst" ] && [ "$_psd_src" -ef "$_psd_dst" ]; then
        return 0
    fi

    # Tier 1 — the destination is already ours (operator chowned the
    # webroot, or symlinked it into shared/). No privilege needed.
    # The cp exit status is propagated: a real copy failure must NOT be
    # reported as "nginx root OK".
    if [ -d "$_psd_dst" ] && [ -w "$_psd_dst" ]; then
        cp -a "$_psd_src/." "$_psd_dst/" || return 1
        return 0
    fi

    # Tier 2 — destination absent but its parent is ours: create it.
    if [ ! -e "$_psd_dst" ] && [ -w "$(dirname "$_psd_dst")" ]; then
        if ! mkdir -p "$_psd_dst" || ! cp -a "$_psd_src/." "$_psd_dst/"; then
            return 1
        fi
        return 0
    fi

    # Tier 3 — needs root. Probe with the real command rather than a
    # `sudo -n true` canary: a whitelist can grant cp/mkdir without
    # granting `true`, and the canary would produce a false negative.
    # mkdir is only attempted when the destination is actually absent —
    # a failed publish must not leave a new root-owned empty webroot
    # behind (the granted mkdir would otherwise succeed even though the
    # ungranted cp fails, mutating production state on a failed step).
    if [ -n "$SUDO_BIN" ]; then
        if [ ! -e "$_psd_dst" ]; then
            "$SUDO_BIN" -n mkdir -p "$_psd_dst" 2>/dev/null || return 1
        fi
        if "$SUDO_BIN" -n cp -a "$_psd_src/." "$_psd_dst/" 2>/dev/null; then
            return 0
        fi
    fi

    return 1
}

# _sync_managed_systemd_units — installs and enables the explicit allowlist
# of background-job units in deploy/systemd/MANAGED_UNITS (RND-410-adjacent
# fix for issue #46 bug 3: an avatar-sync timer sat in the repo, never
# installed on the host, because installing a NEW systemd unit has always
# been a separate manual runbook step — see docs/DEPLOYMENT.md — that this
# CD pipeline never performed).
#
# Deliberately reads an explicit manifest rather than scanning
# deploy/systemd/*.timer: that directory also holds a templated unit
# (qiniu-ssl-renew@.timer, needs a per-instance argument to enable) and
# manual/one-off units (e.g. wecom-thumbnail-backfill.service) that must
# never be auto-enabled just because they exist on disk.
#
# Non-fatal by design, like step 9's static-site publish: this runs after
# the backend is already restarted and health-gated, so a missing sudoers
# grant here must WARN, never fail or roll back a deploy whose actual
# application code is already live and healthy.
_sync_managed_systemd_units() {
    local manifest="$DEPLOY_DIR/deploy/systemd/MANAGED_UNITS"
    if [ ! -f "$manifest" ]; then
        echo "  → no deploy/systemd/MANAGED_UNITS manifest — nothing to sync."
        return 0
    fi
    if [ -z "$SUDO_BIN" ]; then
        echo "  → SUDO_BIN unset — skipping (no privilege to write $SYSTEMD_UNIT_DIR)."
        return 0
    fi

    local changed=0 unit src
    while IFS= read -r unit; do
        case "$unit" in
            ''|'#'*) continue ;;
        esac
        src="$DEPLOY_DIR/deploy/systemd/$unit"
        if [ ! -f "$src" ]; then
            echo "  WARN: MANAGED_UNITS lists $unit but $src does not exist — skipping." >&2
            continue
        fi
        if [ -f "$SYSTEMD_UNIT_DIR/$unit" ] && cmp -s "$src" "$SYSTEMD_UNIT_DIR/$unit" 2>/dev/null; then
            continue # already installed and unchanged — nothing to do
        fi
        if "$SUDO_BIN" -n cp "$src" "$SYSTEMD_UNIT_DIR/" 2>/dev/null; then
            echo "  → installed/updated $unit"
            changed=1
        else
            echo "  WARN: could not install $unit into $SYSTEMD_UNIT_DIR — no non-interactive sudo grant for this exact cp invocation (see the Server (sudo) Prerequisites comment at the top of this script)." >&2
        fi
    done <"$manifest"

    if [ "$changed" -eq 1 ]; then
        if ! "$SUDO_BIN" -n "$SYSTEMCTL_BIN" daemon-reload 2>/dev/null; then
            echo "  WARN: systemctl daemon-reload failed or was not permitted — newly installed/updated units may not take effect yet." >&2
        fi
    fi

    while IFS= read -r unit; do
        case "$unit" in
            ''|'#'*) continue ;;
        esac
        case "$unit" in
            *.timer|*.path) ;;
            *) continue ;; # only trigger units are enabled directly; the oneshot .service they target is started BY the trigger, never enabled itself
        esac
        if [ ! -f "$SYSTEMD_UNIT_DIR/$unit" ]; then
            continue # install above failed or was skipped for this unit — nothing to enable
        fi
        if ! "$SUDO_BIN" -n "$SYSTEMCTL_BIN" enable --now "$unit" 2>/dev/null; then
            echo "  WARN: could not enable/start $unit — no non-interactive sudo grant for this exact systemctl invocation." >&2
        fi
    done <"$manifest"
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

# _config_hostname <value> — reduce an ADMIN_DOMAIN-style configuration
# value to the bare hostname, as literal data only. Accepts the two forms
# the application accepts for this setting ("admin.example.com" and
# "https://admin.example.com", optionally with a port), lowercased.
# Deliberately no smarter than that: a value this cannot cleanly reduce
# is passed through, the app's Host validation then rejects the probe
# with a loud 421 at the gate (see _wait_for_health) rather than this
# script silently second-guessing the configuration.
_config_hostname() {
    local raw="$1"
    raw="${raw#*://}"   # scheme, if present
    raw="${raw%%\?*}"   # query, if any
    raw="${raw%%#*}"    # fragment, if any
    raw="${raw%%/*}"    # path, if any
    raw="${raw%%:*}"    # port, if any
    printf '%s' "$raw" | tr '[:upper:]' '[:lower:]'
}

# _wait_for_health <label> <url> <retries> <interval-seconds> [extra-curl-args…]
# Prints one line per attempt (status only — never response body on
# failure) and returns 0 the first time the endpoint responds 2xx,
# non-zero once every attempt is exhausted. A single flaky attempt
# (nginx/DNS/TLS/service warm-up) must never fail the whole deploy.
#
# GH-161: a 421 response is reported as its own probe/config error, not
# as "not ready yet" — production Host validation (BrandingHostMiddleware)
# rejects the request before the health handler ever runs, so waiting
# cannot resolve it and the misconfiguration must be diagnosable from
# the deploy log alone.
_wait_for_health() {
    local label="$1" url="$2" retries="$3" interval="$4" attempt status
    shift 4
    echo "  → $label: $url (up to $retries attempts, ${interval}s apart)"
    for attempt in $(seq 1 "$retries"); do
        # %{http_code} is still written when -f aborts on an HTTP error
        # (000 on connection failure), so every failure carries a status.
        if status="$("$CURL_BIN" -fsS -o /dev/null -w '%{http_code}' --max-time "$HEALTH_CURL_TIMEOUT_SECONDS" "$@" "$url" 2>/dev/null)"; then
            echo "    attempt $attempt/$retries — OK"
            return 0
        fi
        if [ "$status" = "421" ]; then
            echo "    attempt $attempt/$retries — REJECTED (HTTP 421 Misdirected Request: the probe's Host was not accepted — probe/configuration error, waiting will not fix it; check ADMIN_DOMAIN and the internal probe Host)" >&2
        else
            echo "    attempt $attempt/$retries — not ready yet"
        fi
        if [ "$attempt" -lt "$retries" ]; then
            sleep "$interval"
        fi
    done
    echo "    all $retries attempts failed" >&2
    return 1
}

# _wait_for_internal_health <label> — the ONE construction of the internal
# readiness probe (GH-161). Both gates that target INTERNAL_HEALTH — the
# forward gate and the post-rollback gate in _rollback_and_restart_old —
# call this so their probes can never diverge. The probe keeps connecting
# to the loopback INTERNAL_HEALTH URL (no public DNS/TLS dependency), but
# since APP_ENV=production (GH-148 cutover) BrandingHostMiddleware accepts
# only configured platform hosts and rejects a default
# "Host: 127.0.0.1:<port>" with 421 before the health handler runs. The
# configured platform hostname is therefore presented as an explicit Host
# header — loaded as literal data from backend/.env (ADMIN_DOMAIN, the
# same value the application itself validates Hosts against; the file is
# never executed, see _load_deploy_environment). Empty INTERNAL_PROBE_HOST
# (unset/unparseable) keeps the legacy header-less probe: fine where the
# app runs with development Host leniency, and a loud 421 at the gate
# where it does not.
_wait_for_internal_health() {
    local host_args=()
    if [ -n "$INTERNAL_PROBE_HOST" ]; then
        host_args=(-H "Host: $INTERNAL_PROBE_HOST")
    fi
    _wait_for_health "$1" "$INTERNAL_HEALTH" "$HEALTH_RETRIES" "$HEALTH_RETRY_INTERVAL_SECONDS" ${host_args[@]+"${host_args[@]}"}
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

    if ! _wait_for_internal_health "Rollback readiness"; then
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
echo "[1/10] Pulling latest code from $GIT_REMOTE/$GIT_BRANCH …"

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
# not block deployment (the target-tree preflight and final checkout will
# still refuse an incoming tracked-file collision).
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
    if ! deploy_preflight_check_untracked_collisions "$EXPECTED_SHA"; then
        exit 1
    fi
    if ! _git checkout -B "$GIT_BRANCH" "$EXPECTED_SHA" >/dev/null 2>&1; then
        echo "ERROR: could not check out EXPECTED_SHA ($EXPECTED_SHA)." >&2
        exit 1
    fi
else
    # Resolve one fetched target before the preflight and checkout so a manual
    # deploy cannot inspect one revision then pull a newer, unchecked one.
    _git fetch --quiet "$GIT_REMOTE" "$GIT_BRANCH"
    TARGET_SHA=$(_git rev-parse FETCH_HEAD)
    if ! _git merge-base --is-ancestor "$PREV_SHA" "$TARGET_SHA"; then
        echo "ERROR: fetched target ($TARGET_SHA) is not a fast-forward from the current commit ($PREV_SHA) — refusing to deploy a non-fast-forward or unknown commit." >&2
        exit 1
    fi
    if ! deploy_preflight_check_untracked_collisions "$TARGET_SHA"; then
        exit 1
    fi
    if ! _git checkout -B "$GIT_BRANCH" "$TARGET_SHA" >/dev/null 2>&1; then
        echo "ERROR: could not check out fetched target ($TARGET_SHA)." >&2
        exit 1
    fi
fi

CURRENT_SHA=$(_git rev-parse HEAD)
echo "  Current commit:  $CURRENT_SHA"
if [ -n "$EXPECTED_SHA" ] && [ "$CURRENT_SHA" != "$EXPECTED_SHA" ]; then
    echo "ERROR: HEAD ($CURRENT_SHA) does not match EXPECTED_SHA ($EXPECTED_SHA) after checkout." >&2
    exit 1
fi
echo ""

cd backend

# ── 2. Check media-transcoding host prerequisite (GH-133) ──────────────────
echo "[2/10] Checking ffmpeg host prerequisite …"
if ! _check_ffmpeg; then
    echo "ERROR: ffmpeg is required on the production host but is not installed." >&2
    echo "  Install it using the host bootstrap/runbook (see docs/operations/deploy-sudoers.md" >&2
    echo "  and the 'First-Time Server Setup' comment at the top of this script), then rerun" >&2
    echo "  deployment. This deploy user does not have package-manager sudo, by design." >&2
    _restore_worktree_only
    exit 1
fi

# ── 3. Install / update Python dependencies ────────────────────────────────
echo "[3/10] Installing Python dependencies …"
if [ ! -d .venv ]; then
    echo "ERROR: Virtual environment not found at $PWD/.venv. Run first-time setup." >&2
    _restore_worktree_only
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Load only deployment-specific values as literal data so `alembic upgrade
# head` below runs against the SAME database the systemd service uses. Never
# source backend/.env: it may contain private keys and must not execute code.
# Production retains the checkout-local .env default; RND-392 passes an
# operator-managed external file through DEPLOY_ENV_FILE.
DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-$PWD/.env}"
if [ -f "$DEPLOY_ENV_FILE" ]; then
    _load_deploy_environment "$DEPLOY_ENV_FILE"
    # PUBLIC_HEALTH was not defaulted at the top of this script (see config
    # section) because it depends on ARCHIVE_DOMAIN which may be set in .env.
    # Default it now that deployment values have been loaded.
    PUBLIC_HEALTH="${PUBLIC_HEALTH:-https://${ARCHIVE_DOMAIN}/health}"
    # GH-161: hostname the internal readiness probe presents as its Host
    # header (see _wait_for_internal_health). Derived from the ADMIN_DOMAIN
    # loaded above, as literal data only — the file is never executed.
    INTERNAL_PROBE_HOST="${INTERNAL_PROBE_HOST:-$(_config_hostname "${ADMIN_DOMAIN:-}")}"
else
    echo "ERROR: deployment configuration file not found — required to supply DATABASE_URL for Alembic." >&2
    _restore_worktree_only
    exit 1
fi

if ! _install_deps; then
    echo "ERROR: Dependency installation failed." >&2
    _restore_worktree_only
    exit 1
fi

# ── 4. Compile-check Python code ───────────────────────────────────────────
echo "[4/10] Checking Python code compilation …"
if ! "$PYTHON_BIN" -m compileall app scripts; then
    echo "ERROR: Compile check failed." >&2
    _restore_worktree_only
    exit 1
fi

# ── 5. Alembic migration (P0-A) ─────────────────────────────────────────────
echo "[5/10] Running Alembic migrations (alembic upgrade head) …"
if ! "$PYTHON_BIN" -m alembic upgrade head 2>&1 | _redact; then
    echo "ERROR: Alembic migration failed. Service was NOT restarted; the old process is still running the old code." >&2
    _restore_worktree_only
    exit 1
fi

# ── 5. Verify DB revision == repository head (P0-B) ─────────────────────────
echo "[6/10] Verifying database revision matches repository head …"
if ! "$PYTHON_BIN" scripts/verify_alembic_head.py 2>&1 | _redact; then
    echo "ERROR: Database revision does not match repository head. Service was NOT restarted." >&2
    _restore_worktree_only
    exit 1
fi

# ── 6. Restart systemd service ──────────────────────────────────────────────
echo "[7/10] Restarting systemd service ($SERVICE) …"
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
echo "[8/10] Readiness health gate …"
if ! _wait_for_internal_health "Internal"; then
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
echo "[9/10] Deploying company homepage static files …"
STATIC_SRC="$DEPLOY_DIR/static_site/company_homepage"
# STATIC_SITE_DIR_NAME — the directory name (under shared/www and nginx's
# webroot) this deployment's static homepage is copied to. Set the real
# value via backend/.env or the calling shell's environment.
STATIC_SITE_DIR_NAME="${STATIC_SITE_DIR_NAME:-site}"
# Both overridable so the bats suite can point them at a temp tree — this
# step ran untested for a month (the fixture never created $STATIC_SRC, so
# every test silently took the "source not found" branch below) and shipped
# three separate production breakages in a row as a result.
SHARED_DST="${SHARED_DST:-/srv/apps/wecom-archive-365/shared/www/$STATIC_SITE_DIR_NAME}"
NGINX_DST="${NGINX_DST:-/var/www/$STATIC_SITE_DIR_NAME}"

if [ -d "$STATIC_SRC" ]; then
    # Ensure target directories exist
    mkdir -p "$SHARED_DST"

    # Copy the whole source directory (not just index.html/style.css) so
    # assets referenced by the page — brand/, assets/, site.webmanifest,
    # etc. — actually reach the served root. A prior version of this step
    # copied only two files, which silently left every other referenced
    # asset 404ing in production. See _publish_static_dir's header for
    # why this is `cp -a` and not `rsync`.
    cp -a "$STATIC_SRC/." "$SHARED_DST/"
    # README.md documents the source tree for contributors and has no
    # business being served. cp has no --exclude, so it is dropped after
    # the copy — and dropped HERE, before the webroot copy below, so that
    # copy needs no exclusion (and therefore no `sudo rm`) of its own.
    rm -f "$SHARED_DST/README.md"
    echo "  → shared OK ($SHARED_DST)"

    # Then publish to the Nginx webroot. Deliberately NOT fatal: by this
    # point the backend has already been restarted, health-gated and
    # confirmed serving, so exiting here would report a failed deploy for
    # code that is live — and would also skip _record_known_good below,
    # leaving the NEXT deploy with no rollback target. A stale homepage is
    # cosmetic and separately fixable; a missing rollback record is not.
    # The warning is loud and names the one-time operator fix precisely,
    # so this cannot decay into the silent no-op the docs warn about.
    #
    # RND-263 (2026-08-03): cp -a preserves the source tree's mode, and
    # the source is checked out under the runtime user's umask (027 on
    # this host), which turns the git-tracked 100644 files into 0640
    # owned by the deploy user. Nginx's worker runs as `nginx` — NOT a
    # member of the deploy user's group — so a 0640 webroot yields
    # HTTP 403 ("stat() ... Permission denied") for the whole homepage.
    # The publish step therefore normalises the webroot to world-readable
    # (o+rX: files readable, dirs searchable) after copying, so the
    # deployed site is actually servable by Nginx. chmod is folded into
    # the success condition: if the copy worked but the chmod did not,
    # reporting "nginx root OK" would be a lie (the site would 403).
    if _publish_static_dir "$SHARED_DST" "$NGINX_DST" && chmod -R o+rX "$NGINX_DST"; then
        echo "  → nginx root OK ($NGINX_DST)"
    else
        echo "  WARN: could not publish the homepage to $NGINX_DST — no plain write access there, and the sudoers whitelist does not cover this exact cp/mkdir invocation (see the VERIFIED-ON-PRODUCTION CAVEAT at the top of this script)." >&2
        echo "  WARN: the backend deploy is UNAFFECTED and this deploy still counts as successful; the current homepage is staged at $SHARED_DST." >&2
        echo "  WARN: one-time operator fix (pick one, needs root; see docs/DEPLOYMENT.md §9 for the full runbook):" >&2
        echo "  WARN:   a) point the Nginx 'root' for this site at $SHARED_DST, or" >&2
        echo "  WARN:   b) mv $NGINX_DST $NGINX_DST.bak.$(date +%Y%m%d) && ln -s $SHARED_DST $NGINX_DST, or" >&2
        echo "  WARN:   c) chown -R $(id -un): $NGINX_DST" >&2
    fi
else
    echo "  WARN: static site source not found at $STATIC_SRC — skipping"
fi

# ── 9. Sync managed systemd units ──────────────────────────────────────────
# Same non-fatal philosophy as step 9 above: this runs after the backend is
# already restarted and health-gated, so a missing sudoers grant here must
# never fail or roll back a deploy whose application code is already live.
echo "[10/10] Syncing managed systemd units …"
_sync_managed_systemd_units

if ! _record_known_good "$CURRENT_SHA"; then
    echo "ERROR: the service is healthy on $CURRENT_SHA, but the last-known-good rollback record could not be persisted — the next deploy could not reliably roll back if it fails. Treating this deploy as failed." >&2
    exit 1
fi
echo "=== Deploy complete ($PREV_SHA -> $CURRENT_SHA) ==="
