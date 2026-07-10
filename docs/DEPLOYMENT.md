# Deployment Guide — 365 WeCom Archive

This document separates:

- what is versioned in this repository
- what must still be supplied by the operator

That boundary matters: the repo contains worker/media timer units and a deploy
script, but not every production asset.

---

## 1. Repo-Owned Deployment Assets

Versioned in this repository:

| Asset | Path | Purpose |
|------|------|---------|
| Deploy script | `scripts/deploy_server.sh` | Pull latest code, install deps, restart app service, verify health |
| Worker unit | `deploy/systemd/wecom-archive-worker.service` | One-shot sync + decrypt |
| Worker timer | `deploy/systemd/wecom-archive-worker.timer` | Runs worker every 5 minutes |
| Media unit | `deploy/systemd/wecom-archive-media-download.service` | One-shot image download |
| Media timer | `deploy/systemd/wecom-archive-media-download.timer` | Runs media download every 5 minutes |
| GitHub Actions workflow | `.github/workflows/deploy.yml` | Triggers deploy script on `main` push |

Not versioned in this repository:

| Asset | Status |
|------|--------|
| Main web service unit (`wecom-archive-365.service`) | Operator-managed |
| Reverse proxy config (Nginx / equivalent) | Operator-managed |
| TLS certificates | Operator-managed |

---

## 2. Local / New-Server Bootstrap

Run from `backend/` after creating `.env`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python scripts/bootstrap_default_tenant.py
```

Why both init steps are required:

- `alembic upgrade head` creates the schema
- `bootstrap_default_tenant.py` creates the default tenant and tenant config rows required by auth and sync paths

Without the bootstrap step, password-mode login and tenant-scoped APIs will not
work.

**Qiniu Kodo storage (RND-174, optional).** `requirements.txt` pins the
`qiniu` SDK, so a normal `pip install -r requirements.txt` (including on
an upgrade — re-run it before enabling Qiniu on an existing deployment)
is sufficient; nothing extra to install. Qiniu is only used if
`MEDIA_STORAGE_PROVIDER=qiniu_kodo` is set (for new writes) or an
existing `media_files` row has `storage_backend=qiniu_kodo` (for reads) —
local-only deployments never need `QINIU_*` configured. See
`.env.example` for the required variables (`QINIU_ACCESS_KEY`,
`QINIU_SECRET_KEY`, `QINIU_BUCKET`, `QINIU_DOMAIN` — must be a full
`https://` URL) and
[research/rnd_174_qiniu_kodo_provider.md](research/rnd_174_qiniu_kodo_provider.md)
for the per-row storage model and rollback behavior.

---

## 3. Required Environment Variables

The current source of truth is [../.env.example](../.env.example).

Minimum local app bring-up:

- `DATABASE_URL`
- `AUTH_MODE=password`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD_HASH`
- `WECOM_CORP_ID`
- `WECOM_AGENT_ID`
- `WECOM_OAUTH_SECRET`

Additional variables are required for:

- WeCom OAuth: `ADMIN_DOMAIN`
- Sync/decrypt/media scripts: `WECOM_SDK_LIB_PATH`, `WECOM_ARCHIVE_SECRET`, `WECOM_PRIVATE_KEY_PATH`, `WECOM_PUBLIC_KEY_VERSION`
- Media serving/download, local-backed rows only: `STORAGE_LOCAL_PATH`
- Media serving/download, Qiniu-backed rows only (optional — see below): `QINIU_ACCESS_KEY`, `QINIU_SECRET_KEY`, `QINIU_BUCKET`, `QINIU_DOMAIN` (full `https://` URL), `QINIU_REGION` (optional)

---

## 4. Main Web Service

The repository does **not** contain the production `wecom-archive-365.service`
unit file. Whatever service manager you use must run the FastAPI app with an
equivalent working directory and environment to the local command:

```bash
cd /srv/apps/wecom-archive-365/current/backend
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8035
```

Production typically omits `--reload`.

Health endpoint:

- internal: `http://127.0.0.1:8035/health`

---

## 5. Worker / Timer Installation

Archive worker:

```bash
sudo cp deploy/systemd/wecom-archive-worker.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-worker.timer
```

Media download worker:

```bash
sudo cp deploy/systemd/wecom-archive-media-download.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-media-download.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-media-download.timer
```

Detailed operational behavior lives in:

- [wecom_archive_worker_runbook.md](wecom_archive_worker_runbook.md)
- [wecom_archive_media_download_runbook.md](wecom_archive_media_download_runbook.md)

---

## 6. Reverse Proxy

The repo currently assumes a reverse proxy in front of the app for production
HTTP/TLS, but no Nginx config is versioned here.

Whatever proxy you use must route:

- public HTTPS traffic to the FastAPI app on `127.0.0.1:8035`
- `/health` to the same backend

For `AUTH_MODE=wecom`, the externally reachable admin domain must exactly match
the trusted domain configured in WeCom Admin.

---

## 7. GitHub Actions Deploy Path

Current automation:

1. Push to `main`
2. `.github/workflows/deploy.yml` runs compile-check
3. Same workflow SSHes to the ECS host
4. Remote host executes `bash /srv/apps/wecom-archive-365/current/scripts/deploy_server.sh`

`deploy_server.sh` then:

1. `git pull --ff-only origin main`
2. installs Python dependencies
3. runs `python -m compileall app scripts`
4. restarts `wecom-archive-365.service`
5. checks internal and public health endpoints

Current public health URL hardcoded in the script:

- `https://qwhhcd.crowntime.cn/health`

If the deployed domain changes, update the script in the same change.

---

## 8. Known Gaps

These are documentation truths, not hidden assumptions:

- the main web service unit is not stored in this repo
- reverse-proxy config is not stored in this repo
- live production state cannot be proven from git alone

Keep this document honest if that boundary changes.
