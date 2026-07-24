---
kind: configuration_system
name: Environment-Based Configuration System
category: configuration_system
scope:
    - '**'
source_files:
    - .env.example
    - backend/.env
    - backend/app/db/session.py
    - backend/app/auth.py
    - backend/app/media_storage.py
    - backend/app/media_thumbnails.py
    - backend/alembic/env.py
    - backend/alembic.ini
    - ssl-renew/examples/domain.env.example
---

The WeCom Archive 365 monorepo uses a pure environment-variable-based configuration system with no dedicated config files, YAML/JSON loaders, or configuration frameworks. All runtime settings are consumed directly from `os.environ` / `os.getenv()` at the point of use throughout the codebase.

**What system/approach is used**
- Pure `os.environ` / `os.getenv()` reads — no `python-dotenv`, Pydantic Settings, or config parsers
- `.env.example` serves as the single source-of-truth documentation for all available variables
- `.env` files are gitignored and only used locally; production relies on process/environment injection (systemd, container env, etc.)
- Alembic migrations read `DATABASE_URL` plus optional `DB_*` compatibility aliases

**Key files and packages**
- `.env.example` — comprehensive template documenting every supported variable with defaults, ranges, and usage notes
- `backend/.env` — local smoke-test example (gitignored)
- `backend/app/db/session.py` — reads `DATABASE_URL` to construct SQLAlchemy engine
- `backend/app/auth.py` — reads `AUTH_MODE`, `APP_ENV`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`
- `backend/app/media_storage.py` — reads `MEDIA_STORAGE_PROVIDER`, `STORAGE_BACKEND`, `STORAGE_LOCAL_PATH`, all `QINIU_*` vars, `MEDIA_SIGNED_URL_TTL_SECONDS`, `MEDIA_SIGNED_URL_WINDOW_SECONDS`
- `backend/app/media_thumbnails.py` — reads `MEDIA_THUMBNAIL_ENABLED`, `MEDIA_THUMBNAIL_MAX_EDGE`, `MEDIA_THUMBNAIL_JPEG_QUALITY`
- `backend/alembic/env.py` — reads `DATABASE_URL` and `DB_*` fallbacks for migrations
- `backend/alembic.ini` — Alembic logging configuration
- `ssl-renew/examples/domain.env.example` — separate env template for the SSL renewal tool

**Architecture and conventions**
- **Per-feature accessor functions**: Each module defines small helper functions that read and validate specific env vars (e.g., `get_signed_url_ttl_seconds()`, `get_signed_url_window_seconds()`, `get_auth_mode()`) rather than reading `os.environ` inline. This centralizes validation, defaults, and error messages.
- **Strict validation with explicit errors**: Invalid values raise domain-specific exceptions (`MediaStorageConfigurationError`, `QiniuConfigurationError`, `UnsupportedMediaStorageProvider`) instead of silently falling back or crashing later.
- **No lazy loading of secrets**: Qiniu credentials are only validated when `qiniu_kodo` is selected as the provider — importing the module does not load them.
- **Per-row storage resolution**: Media rows carry their own `storage_backend` column, so existing data is served through the correct provider regardless of the deployment-wide `MEDIA_STORAGE_PROVIDER` setting — switching providers never reinterprets historical rows.
- **Typed defaults and bounds**: Numeric env vars are parsed with explicit min/max validation (e.g., TTL window bounded to [60, 3600] seconds) and fail loudly on out-of-range values.
- **Production detection via `APP_ENV`**: A simple `_is_production()` check in auth.py distinguishes environments.

**Rules developers should follow**
- Add new configuration variables to `.env.example` with clear comments explaining purpose, valid values, and defaults
- Create a dedicated accessor function in the relevant module to read and validate the env var — never call `os.getenv` directly in business logic
- Fail fast with descriptive errors for missing required configuration (especially secrets like `QINIU_ACCESS_KEY`, `QINIU_SECRET_KEY`, `QINIU_BUCKET`, `QINIU_DOMAIN`)
- Never log or return raw secret values from environment variables
- Use `os.environ.get(...).strip()` consistently to handle whitespace-only values
- For boolean-like flags, normalize with `.lower()` and compare against known sets
- Lock file paths and other filesystem paths should be validated but treated as optional where appropriate (e.g., media root can be unset without failing startup)