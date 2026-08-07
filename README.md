# Crowntime WeCom Archive

A complete archive and review console for WeCom conversation data — not an
SDK wrapper. It retrieves archive records through the WeCom Conversation
Archive API, decrypts and stores them, and gives authorized administrators a
full web-based review console with search, media access, export approval, and
audit logging.

Crowntime WeCom Archive is a proprietary product of 深圳康冠时代科技有限公司
(Crowntime), **operated as a hosted service**. It is not open source, and no
self-hosting or on-premises license is offered. This repository is private and
its contents are confidential.

> **This README is internal documentation.** The setup instructions below are
> for developing and operating the service ourselves, not a customer-facing
> installation guide. For the product strategy behind that, see
> [ADR-0003](docs/adr/0003-product-strategy-hosted-only.md) and the
> [first-10-customers roadmap](deliverables/roadmap-first-10-customers-2026-08-07.md).

> This is an independent project. “WeCom” and “企业微信” are trademarks of
> Tencent and are used here only to describe compatibility. This software is
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
scripts are under `backend/alembic/` and `backend/scripts/`. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the component and data-flow
reference.

## Quick start

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

- [v1.0.0 — the first complete product baseline](docs/releases/v1.0.0.md)

## FAQ

**Why do sync or decryption commands fail without the SDK?** The archive
cryptography and media APIs are provided by Tencent’s proprietary C SDK. Obtain
it from Tencent, place it outside version control (the ignored
`backend/vendor/wecom_sdk/` location is available if useful), and point
`WECOM_SDK_LIB_PATH` at its shared library.

## License

Proprietary and confidential — see [LICENSE](LICENSE). All rights reserved by
深圳康冠时代科技有限公司. Third-party components, including Tencent's WeCom SDK
and the open-source dependencies in the requirements files, remain subject to
their own terms.

## Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the development setup, the
acceptance chain a pull request has to pass, and the codebase invariants worth
knowing before you change anything.

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
   For a real archive sync, also configure the WeCom SDK and
   `WECOM_ARCHIVE_SECRET`; the worker requires both.
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
