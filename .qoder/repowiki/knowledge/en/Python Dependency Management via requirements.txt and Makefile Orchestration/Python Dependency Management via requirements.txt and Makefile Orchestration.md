---
kind: dependency_management
name: Python Dependency Management via requirements.txt and Makefile Orchestration
category: dependency_management
scope:
    - '**'
source_files:
    - backend/requirements.txt
    - ssl-renew/requirements.txt
    - ssl-renew/requirements-dev.txt
    - pyproject.toml
    - Makefile
---

This monorepo manages Python dependencies using a flat `requirements.txt` approach per component, with no lockfiles, virtual environments committed to the repo, and no private registries. Dependencies are declared as pinned or constrained versions in two locations: `backend/requirements.txt` for the FastAPI service and `ssl-renew/requirements.txt` (plus `ssl-renew/requirements-dev.txt`) for the SSL certificate renewal tool.

**System used**: Standard `pip install -r requirements.txt` with Python virtual environments created locally (`.venv/` at the repo root). The top-level `Makefile` is the single entry point that orchestrates dependency installation, linting (`ruff`), testing (`pytest`), and build verification across both Python components. There is no Poetry, Pipenv, pip-tools, or vendoring strategy — each `requirements.txt` lists exact runtime pins (e.g. `fastapi==0.115.6`, `sqlalchemy==2.0.36`, `qiniu==7.18.0`) or version ranges (e.g. `httpx>=0.24.0`, `qiniu>=7.9,<8`).

**Key files**:
- `backend/requirements.txt` — all runtime dependencies for the FastAPI backend (FastAPI, SQLAlchemy, Alembic, psycopg2-binary, qiniu SDK, Pillow, cryptography, pydantic-settings)
- `ssl-renew/requirements.txt` — runtime dependency for the Qiniu helper script (official `qiniu` SDK, constrained to major version 7.x)
- `ssl-renew/requirements-dev.txt` — test-only dependencies for the SSL tool (`pytest`, includes `-r requirements.txt`)
- `pyproject.toml` — only contains Ruff linter configuration; no `[project]` or dependency declarations
- `Makefile` — central orchestration that installs ruff and pytest into the shared `.venv/`, runs `pip install -r backend/requirements.txt`, and validates both Python components

**Architecture and conventions**:
- Each sub-project declares its own `requirements.txt`; there is no shared base file between `backend/` and `ssl-renew/`.
- Dependencies are pinned to specific versions where stability matters (SDKs like `qiniu==7.18.0`, `psycopg2-binary==2.9.9`) but allow flexibility for libraries with stable APIs (`httpx>=0.24.0`, `qiniu>=7.9,<8`).
- Development tools (`ruff`, `pytest`) are installed alongside runtime deps through the Makefile's shared `.venv/` rather than separate venvs.
- No `Pipfile.lock`, `poetry.lock`, or `requirements.lock` exists — dependency resolution happens at install time on each developer's machine.
- No private PyPI registry or `--index-url` configuration is used; all packages come from the public PyPI.
- The `ssl-renew/requirements-dev.txt` uses `-r requirements.txt` inheritance to avoid duplicating runtime dependencies.

**Rules developers should follow**:
- Add new dependencies to the appropriate `requirements.txt` file (backend vs ssl-renew) with explicit version pins when the package is an external SDK or has breaking release cycles.
- Use `make verify` (which runs `lint-diff typecheck build test`) before committing — this ensures the dependency graph still resolves and imports cleanly.
- Do not commit `.venv/` directories or any generated lockfiles; create fresh venvs per environment.
- When adding optional dependencies (like `qiniu` for media storage), keep them in `requirements.txt` but gate their usage behind feature flags since the app runs without them in local mode.
- Test-only dependencies go in `requirements-dev.txt` using the `-r requirements.txt` include pattern to stay in sync with runtime deps.