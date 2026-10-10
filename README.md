# wecom-archive

A complete archive and review console for WeCom conversation data — not an
SDK wrapper. It retrieves archive records through the WeCom Conversation
Archive API, decrypts and stores them, and gives authorized administrators a
full web-based review console with search, media access, export approval, and
audit logging.

## 项目定位 / Project positioning

**中文：** `wecom-archive` 是由康冠时代（深圳康冠时代科技有限公司，Crowntime）
发布的开源企业微信会话存档项目。基于 AGPL-3.0 的自部署版本完整可用，不依赖康冠时代
官方云服务；康冠时代官方云版提供托管与运维便利。自部署版与云版使用同一代码库，云版代码
不会从本仓库中排除。

**English:** `wecom-archive` is an open-source WeCom conversation-archive project
published by Crowntime (康冠时代), Shenzhen Crowntime Technology Co., Ltd. Its
AGPL-3.0 self-hosted edition is complete and usable without depending on the
official cloud service. The official Crowntime cloud edition offers managed
hosting and operational convenience. Both editions use this unified codebase;
cloud-specific source is not withheld from this repository.

> “WeCom” and “企业微信” are Tencent trademarks. This independent project is
> not affiliated with or endorsed by Tencent.

## Features

- SDK-backed archive synchronization and message decryption
- Tenant-aware data model and administrator authentication
- Conversation review, search, reachability diagnostics, and media access
- Local filesystem or Qiniu Kodo media storage
- One-shot worker scripts and systemd timer units for archive and media work

## Architecture

```text
WeCom Conversation Archive API
             │ (C SDK)
             ▼
FastAPI application and worker scripts ──► PostgreSQL
             │
             ├── server-rendered administrator console
             └── local filesystem or Qiniu Kodo media storage
```

The application code is under `backend/app/`; migrations and operational
scripts are under `backend/alembic/` and `backend/scripts/`. See the
[Wiki architecture overview](https://github.com/zuohaisu/wecom-archive/wiki/Architecture-Overview)
for the component and data-flow reference. See
[contact-avatar privacy and update behavior](https://github.com/zuohaisu/wecom-archive/wiki/Contact-Avatars)
for the controlled profile-image contract.

## Documentation

The [GitHub Wiki](https://github.com/zuohaisu/wecom-archive/wiki) is the
complete documentation collection and editorial source of truth for
installation, configuration, usage, API and data-model references, engineering
decisions, and project history. Documents required by GitHub, contributors, or
the running application also remain in this repository as embedded copies.

## Quick start

### Five-minute self-host quickstart / 五分钟自部署

**English:** This Compose path is for a complete self-hosted installation. It
uses PostgreSQL 16, applies migrations, starts the web app, and runs the
self-host worker schedule. It does not require the official cloud service or a
paid cloud subscription.

**中文：** Compose 路径用于完整自部署，包含 PostgreSQL 16、自动数据库迁移、Web
应用和自部署 worker 调度；不依赖康冠时代官方云服务，也不要求云版订阅。

**Prerequisites / 前置条件:** Docker Engine with the Docker Compose v2 plugin;
a Linux host compatible with the Tencent WeCom C SDK; a WeCom organization
with Conversation Archive enabled; and the SDK obtained directly from Tencent
under its terms. The SDK is proprietary and is not included in this repository
or image. Real WeCom callbacks also require a public HTTPS endpoint and an
operator-managed reverse proxy; Compose binds the app to localhost and does not
install DNS, TLS, or a proxy.

1. **Create local configuration / 创建本地配置.** Copy the example and set a
   URL-safe database password (hex works), plus two distinct Fernet keys:
   `FIELD_ENCRYPTION_KEY` and `SETTINGS_ENCRYPTION_KEY`.

   ```bash
   cp .env.example .env
   openssl rand -hex 32
   openssl rand -base64 32 | tr '+/' '-_'  # generate one Fernet key; run twice
   ```

   Paste the outputs into the corresponding values in `.env`. The Compose
   stack overrides the local `DATABASE_URL` host with its private `db` service.

2. **Provide the SDK and start the stack / 放置 SDK 并启动.** Put Tencent's
   `libWeWorkFinanceSdk_C.so` at the path shown below, then start the services.
   The migration service must succeed before web and worker start.

   ```bash
   mkdir -p backend/vendor/wecom_sdk
   # Copy the SDK library to backend/vendor/wecom_sdk/libWeWorkFinanceSdk_C.so
   docker compose up -d --build
   docker compose ps
   ```

3. **Initialize and configure / 初始化并配置.** Open
   `http://127.0.0.1:8035/admin/settings/init` and create the local
   administrator. The example uses `AUTH_MODE=password`, so sign in at
   `/admin/login` with that account; global WeCom OAuth settings are optional
   for this local login. Then open
   `http://127.0.0.1:8035/admin/provisioning/settings` (the S2 setup wizard) to
   enter and test the tenant-scoped archive credentials.

4. **Run the first sync / 执行首次同步.** With a test organization and SDK
   configured, run the existing one-shot worker and then refresh the review
   console:

   ```bash
   docker compose exec worker python scripts/run_archive_worker_once.py
   docker compose logs --tail=100 worker
   ```

   The first archived message appears after WeCom returns an eligible record;
   message availability and WeCom-side permissions are external prerequisites.
   Email is not required for sync; configure your own SMTP transport in `.env`
   if you want outbound notifications.

The app port is bound to `127.0.0.1` by default. Before enabling remote access
or WeCom callbacks, set `ADMIN_DOMAIN` to the public hostname, set
`APP_ENV=production`, and place an operator-managed TLS reverse proxy in front
of the app; Compose does not provision DNS, certificates, or a proxy.
`docker compose down` preserves the named database and media volumes; **do not
use `docker compose down -v` unless you intend to delete that data**. The
Compose worker runs only self-host jobs; cloud billing/payment jobs and
deferred destructive cleanup/purge jobs are not started.

### Prerequisites

- Python 3.11+
- PostgreSQL 14+

### WeCom SDK requirement

`backend/vendor/wecom_sdk/` is reserved for the Tencent proprietary WeCom C
SDK. It is excluded by `.gitignore`, and this repository does **not** include
its binary or source distribution. If you need archive synchronization,
decryption, or media download, obtain the SDK yourself from Tencent under its
applicable terms and set `WECOM_SDK_LIB_PATH` to the resulting shared library.

You can run the local password-authenticated console and mock-data workflow
without a working SDK or live WeCom credentials.

### Local console

```bash
# 1. Clone and prepare an environment
cd <repository-directory>
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
# Existing root .venv: rerun this command after pulling dependency changes;
# the requirements pin Ruff, so do not rely on a global Ruff installation.
# Root make lint, make lint-diff, and make verify use .venv/bin/python -m ruff.

# 2. Create local configuration (never commit it)
cp .env.example backend/.env

# 3. Configure backend/.env, then initialize the database
cd backend
alembic upgrade head
python scripts/bootstrap_default_tenant.py

# 4. Start the application
uvicorn app.main:app --reload --host 127.0.0.1 --port 8035
```

For a local password login, set at least `DATABASE_URL`, `AUTH_MODE=password`,
`ADMIN_USERNAME`, and `ADMIN_PASSWORD_HASH` in `backend/.env`. The
[`.env.example`](.env.example) file is the configuration reference and uses
placeholders only.

After startup, use `http://127.0.0.1:8035/health` for the health check and
`http://127.0.0.1:8035/docs` for the interactive API documentation. You can
load non-production sample records with `python scripts/mock_ingest.py` from
`backend/`.

## Running tests

Install the development dependencies on top of the runtime set, then run
the suite from the repository root:

```bash
python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
make test
```

Most of the suite is offline: it builds its own SQLite engines and uses a
fake WeCom SDK, so neither PostgreSQL, the proprietary SDK, nor live
credentials are needed. Tests that genuinely require PostgreSQL skip
themselves when `DATABASE_URL` is unset — a run with skips reported is
the expected local result, not a failure.

To exercise the PostgreSQL-only tests as well, point `DATABASE_URL` at a
disposable database (never a production one) and run `alembic upgrade
head` from `backend/` first.

`make verify` runs the full developer acceptance chain — lint on the
current diff, type check, build, then tests. Pull requests run the same
gates through [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Configuration and deployment

Keep secrets only in an ignored `.env` file; do not replace placeholder values
in `.env.example`. WeCom-backed archive processing additionally requires the
SDK, `WECOM_SDK_LIB_PATH`, and the applicable WeCom credentials.

For a production deployment, set `ARCHIVE_DOMAIN` to the public archive
hostname. It must match the hostname configured in the reverse proxy. The
repository contains worker units and deployment guidance; the web-service unit,
reverse-proxy configuration, and TLS certificates are operator-managed. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full deployment procedure.

## Releases

- **[v1.1.0 — First public open-source release](https://github.com/zuohaisu/wecom-archive/releases/tag/v1.1.0)** (2026-10-09)
  - Public release under AGPL-3.0, with complete self-hosted deployment support,
    shared cloud/self-hosted source code, and public engineering documentation.

- **[v1.0.0 — First complete product baseline](https://github.com/zuohaisu/wecom-archive/releases/tag/v1.0.0)** (product baseline: 2026-08-07; GitHub Release: 2026-10-09)
  - First complete product baseline covering conversation archiving, review,
    access control, audit, operations, and organization onboarding.

## FAQ

**Why do sync or decryption commands fail without the SDK?** The archive
cryptography and media APIs are provided by Tencent’s proprietary C SDK. Obtain
it from Tencent, place it outside version control (the ignored
`backend/vendor/wecom_sdk/` location is available if useful), and point
`WECOM_SDK_LIB_PATH` at its shared library.

## License

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

This project is licensed under the GNU Affero General Public License, version
3.0 (AGPL-3.0); see [LICENSE](LICENSE) for the full license text and
[NOTICE](NOTICE) for project attribution and trademark information. The
project's own core and cloud code use the same license. Third-party components
remain subject to their own terms; Tencent's proprietary WeCom SDK is not
included or redistributed by this project.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, contribution
workflow, validation chain, and current codebase boundaries.

Security issues follow [SECURITY.md](SECURITY.md) and must never be filed in a
normal issue.

## Settings configuration center

Crowntime WeCom Archive lets an administrator manage supported deployment
settings in the Settings UI. The UI is not a secret store by itself: protect
its database and the encryption key as deployment secrets.

### Encryption key for saved secrets

`SETTINGS_ENCRYPTION_KEY` is the Fernet key used to encrypt secret-valued
Settings entries before they are written to the database. Generate it once on
the host, from the activated Python environment:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the resulting value as `SETTINGS_ENCRYPTION_KEY` in the application
process environment (or in `backend/.env` when the service manager loads that
file). Keep it private and stable: changing or losing it prevents the service
from decrypting secrets already saved in Settings. Do not commit it. If the key
is missing or invalid, the application fails closed and refuses to save
secret-valued configuration rather than storing it in plaintext.

### Environment variables, database values, and restarts

For every setting managed by the UI, resolution order is **database >
environment > default**. Existing environment-based deployment remains
supported: changing `.env` is still effective when that file is loaded into
the application environment, but a value saved in Settings takes precedence.
Delete or replace the saved UI value if an environment change appears to have
no effect.

Some changes need a process restart. The runtime source of truth is
[`RESTART_REQUIRED_KEYS`](backend/app/config/constants.py), not a copied list
in this guide; follow the restart-required notice shown by the Settings UI for
the value you change, then restart the web service and any affected workers.

### First-run setup: from clone to the first message

1. After `git clone`, complete the database and service startup steps in
   [Local console](#local-console). From `backend/`, run
   `python scripts/bootstrap_default_tenant.py` after providing its required WeCom
   values in the loaded environment as described in [`.env.example`](.env.example).
   For a real archive sync, configure the WeCom SDK and complete encrypted
   tenant-scoped archive credentials; the worker does not read a global
   `WECOM_ARCHIVE_SECRET`.
2. With the service running, open
   `http://127.0.0.1:8035/admin/settings/init` (replace the host and port for
   your deployment). This public first-run page is available only until setup
   completes; after that it redirects to the login page.
3. For the password-mode setup in `.env.example`, create the administrator
   username and password. Fill in the WeCom three-part connection details:
   corporate ID, application Agent ID, and OAuth Secret. Select **Save and
   continue**.
4. Sign in at the redirected login page and open the conversation console. To
   bring in the first real archived message, run the configured archive worker
   (or, from `backend/`, run `python scripts/sync_wecom_archive_once.py`);
   then refresh the console. The sync command requires the SDK path, corporate
   ID, archive secret, and an initialized default tenant from step 1.

After first-run setup, use the authenticated Settings page for subsequent
changes; use the UI restart indication rather than guessing which processes
must be restarted.




END
