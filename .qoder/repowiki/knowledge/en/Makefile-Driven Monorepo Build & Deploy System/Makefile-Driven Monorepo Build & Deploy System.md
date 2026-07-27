---
kind: build_system
name: Makefile-Driven Monorepo Build & Deploy System
category: build_system
scope:
    - '**'
source_files:
    - Makefile
    - .github/workflows/deploy.yml
    - scripts/deploy_server.sh
    - pyproject.toml
    - ssl-renew/Dockerfile
    - backend/requirements.txt
    - deploy/systemd/wecom-archive-worker.service
---

This repository uses a single top-level `Makefile` as the central orchestrator for all build, lint, test, and deployment tasks across its Python backend, Bash-based SSL renewal tooling, systemd service units, and CI pipeline. The system is designed to be offline-safe (no network calls during development), secret-free by default, and deterministic across local and CI environments.

**Build System Architecture**

The Makefile defines two parallel subsystems: backend (Python/FastAPI) and ssl-renew (Bash/Python tooling). Backend targets use a repo-root `.venv/` virtual environment so a single interpreter covers ruff linting, compile checks, pytest, and Alembic migrations without needing separate venvs. The `verify` target chains `lint-diff → typecheck → build → test` in strict order, stopping at first failure — this is the developer acceptance gate.

Backend build steps include:
- Ruff static analysis over `backend/app`, `backend/scripts`, `backend/tests`
- Python `compileall` syntax pass on app and scripts
- Node.js `--check` validation of embedded JS assets (`backend/app/assets/i18n.js` and `backend/app/web/static/*.js`)
- FastAPI app construction check that imports `app.main` and asserts routes are registered

SSL renewal tooling has its own `ssl-lint` (shellcheck + shfmt + py_compile), `ssl-test` (bats + pytest against qiniu_helper.py), `ssl-dry-run` (dry-run mode with throwaway HOME), and `ssl-verify-systemd` (systemd-analyze verify with Docker fallback).

**CI/CD Pipeline**

GitHub Actions (`.github/workflows/deploy.yml`) runs on pushes to `main` with a two-stage job model:

1. **Test job**: Sets up Python 3.11, Node.js 22, PostgreSQL 16 service, and bats/shellcheck. Executes compile checks, app import verification, shellcheck on deploy scripts, bats integration tests for `deploy_server.sh`, then runs SQLite-compatible tests (offline) followed by PostgreSQL-specific tests (migration tests, schema drift checks via `alembic check`, tenant/auth tests).

2. **Deploy job**: Uses `appleboy/ssh-action@v1.2.0` to SSH into production ECS, performs a fast-forward checkout to `EXPECTED_SHA` (the exact commit GitHub tested), then invokes `scripts/deploy_server.sh` with lock inheritance (`DEPLOY_LOCK_ALREADY_HELD=1`). Concurrency is serialized via GitHub's `concurrency.group: production-deploy` with `cancel-in-progress: false`.

**Production Deployment Script**

`scripts/deploy_server.sh` implements a safe push-to-deploy workflow with automatic rollback:
- Clean-tree guard prevents deploying modified tracked files
- EXPECTED_SHA pinning ensures deploying exactly what CI tested
- Non-blocking flock prevents concurrent deployments
- Alembic migration with post-migration revision verification
- Health gating via `/health/ready` endpoint (DB connectivity + schema revision)
- Automatic rollback to last-known-good SHA if restart/readiness fails
- Never runs `alembic downgrade` — database rollbacks are explicit, not code-driven
- Secret redaction in all output via `_redact()` sed filter

**Systemd Service Units**

Production services are managed via systemd units in `deploy/systemd/`: `wecom-archive-worker.service/timer`, `wecom-archive-media-download.service/timer`, `wecom-thumbnail-backfill.service`, and `qiniu-ssl-renew@.service/.timer`. The Makefile's `ssl-verify-systemd` target validates these units using native `systemd-analyze verify` or a Docker container when unavailable.

**Development Conventions**

- All development tools live in repo-root `.venv/` — never use system Python
- `make lint-diff` is preferred over `make lint` for PRs (only checks changed files)
- `make verify` is the single entry point for developer acceptance
- Tests are self-contained: SQLite-compatible tests run without PostgreSQL, PG-specific tests self-skip when no DB is available
- No frontend build step exists — admin console is server-rendered HTML with embedded JS checked via Node.js
- SSL renewal scripts require shellcheck, shfmt, and bats installed locally or via the provided Docker image