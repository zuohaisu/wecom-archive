#!/usr/bin/env bash
#
# deploy_server.sh — Safe push-to-deploy for wecom-archive-365
#
# Run this script on the production ECS server to pull the latest code
# from GitHub main, install dependencies, restart the service, and verify
# that both the internal and public health endpoints respond.
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
#   5. Install the systemd service unit (not included in this repo — out of scope).
#   6. Ensure the runtime user has passwordless sudo for `systemctl restart wecom-archive-365.service`
#      as described in the sudoers section above.
#   7. Verify the deployment manually once:
#          sudo -u wecomarchive bash scripts/deploy_server.sh
#
# ── Behavior ────────────────────────────────────────────────────────────────
#   - Fails fast on any error (set -euo pipefail).
#   - Pulls the latest code from origin/main with --ff-only
#     (safe for automation; fails if history diverges).
#   - Installs/updates Python dependencies via pip.
#   - Runs compileall to catch syntax / import errors before restart.
#   - Restarts the systemd service.
#   - Verifies health on both internal and public endpoints.
#   - Exits non-zero if any step fails — the workflow will report the failure.
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

DEPLOY_DIR="/srv/apps/wecom-archive-365/current"
SERVICE="wecom-archive-365.service"
INTERNAL_HEALTH="http://127.0.0.1:8035/health"
PUBLIC_HEALTH="https://qwhhcd.crowntime.cn/health"

echo "=== Deploying wecom-archive-365 ==="
DEPLOY_TIME=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
echo "  Time:   $DEPLOY_TIME"

# ── 1. Navigate to deploy directory ────────────────────────────────────────
if [ ! -d "$DEPLOY_DIR" ]; then
    echo "ERROR: Deploy directory $DEPLOY_DIR does not exist." >&2
    exit 1
fi
cd "$DEPLOY_DIR"

# ── 2. Pull latest code ────────────────────────────────────────────────────
echo "[1/6] Pulling latest code from origin/main …"

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

git pull --ff-only origin main

GIT_SHA=$(git rev-parse HEAD)
echo "  Commit: $GIT_SHA"
echo ""

# ── 3. Install / update Python dependencies ────────────────────────────────
echo "[2/6] Installing Python dependencies …"
cd backend
if [ ! -d .venv ]; then
    echo "ERROR: Virtual environment not found at $PWD/.venv. Run first-time setup." >&2
    exit 1
fi
source .venv/bin/activate
python -m pip install -r requirements.txt --quiet

# ── 4. Compile-check Python code ───────────────────────────────────────────
echo "[3/6] Checking Python code compilation …"
python -m compileall app scripts

# ── 5. Restart systemd service ─────────────────────────────────────────────
echo "[4/6] Restarting systemd service ($SERVICE) …"
sudo /usr/bin/systemctl restart "$SERVICE"
if ! systemctl is-active "$SERVICE" >/dev/null 2>&1; then
    echo "ERROR: Service $SERVICE is not active after restart." >&2
    exit 1
fi

# ── 6. Deploy static site (company homepage) ───────────────────────────────
echo "[5/6] Deploying company homepage static files …"
STATIC_SRC="$DEPLOY_DIR/static_site/company_homepage"
SHARED_DST="/srv/apps/wecom-archive-365/shared/www/crowntime"
NGINX_DST="/var/www/crowntime"

if [ -d "$STATIC_SRC" ]; then
    # Copy to shared (wecomarchive-owned) first
    cp "$STATIC_SRC/index.html" "$SHARED_DST/index.html"
    cp "$STATIC_SRC/style.css" "$SHARED_DST/style.css"
    echo "  → shared OK ($SHARED_DST)"

    # Then copy to nginx root (needs sudo)
    sudo cp "$SHARED_DST/index.html" "$NGINX_DST/index.html"
    sudo cp "$SHARED_DST/style.css" "$NGINX_DST/style.css"
    echo "  → nginx root OK ($NGINX_DST)"
else
    echo "  WARN: static site source not found at $STATIC_SRC — skipping"
fi

# ── 7. Verify health endpoints ─────────────────────────────────────────────
echo "[6/6] Verifying health …"

# Retry internal health up to 10 times with 2-second interval
echo "  → Internal: $INTERNAL_HEALTH (up to 10 retries, 2s apart)"
_INTERNAL_OK=false
for i in $(seq 1 10); do
    if curl -fsS "$INTERNAL_HEALTH" >/dev/null 2>&1; then
        _INTERNAL_OK=true
        break
    fi
    echo "    (attempt $i/10 — not ready yet, waiting 2s …)"
    sleep 2
done
if [ "$_INTERNAL_OK" = false ]; then
    echo "ERROR: Internal health check failed after 10 attempts." >&2
    exit 1
fi
curl -fsS "$INTERNAL_HEALTH"
echo ""

echo "  → Public:   $PUBLIC_HEALTH"
curl -fsS "$PUBLIC_HEALTH"
echo ""

echo "=== Deploy complete ==="