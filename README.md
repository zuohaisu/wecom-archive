# WeCom Archive

A self-hosted archive and review console for WeCom conversation data. It
retrieves archive records through the WeCom Conversation Archive API, decrypts
and stores them, and gives authorized administrators a web-based review
console.

> This is an independent project. “WeCom” and “企业微信” are trademarks of
> Tencent and are used here only to describe compatibility.

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
pip install -r backend/requirements.txt

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

## Configuration and deployment

Keep secrets only in an ignored `.env` file; do not replace placeholder values
in `.env.example`. WeCom-backed archive processing additionally requires the
SDK, `WECOM_SDK_LIB_PATH`, and the applicable WeCom credentials.

For a production deployment, set `ARCHIVE_DOMAIN` to the public archive
hostname. It must match the hostname configured in the reverse proxy. The
repository contains worker units and deployment guidance; the web-service unit,
reverse-proxy configuration, and TLS certificates are operator-managed. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full deployment procedure.

## FAQ

**Why do sync or decryption commands fail without the SDK?** The archive
cryptography and media APIs are provided by Tencent’s proprietary C SDK. Obtain
it from Tencent, place it outside version control (the ignored
`backend/vendor/wecom_sdk/` location is available if useful), and point
`WECOM_SDK_LIB_PATH` at its shared library.

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(AGPL-3.0). Third-party components, including the WeCom SDK, remain subject to
their own terms.

## Contributing

Contributions are welcome, but every external contributor must sign the
[Contributor License Agreement](CLA.md) before a pull request can be accepted.
The signing mechanism will be published separately; before submitting a PR,
ask the project maintainers for signing instructions through the repository.

Please also read the [security policy](SECURITY.md) before reporting a
vulnerability.
