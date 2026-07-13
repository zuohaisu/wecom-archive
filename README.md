# 365 WeCom Archive

Internal WeCom (企业微信) conversation archive, media storage, and admin review system. Pulls messages from the WeCom Conversation Archive API, decrypts and stores them, and provides a conversation review console for authorized administrators.

---

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ running locally (or via Docker)

### Local Console Bring-Up

This path is for local admin-console development. It does **not** require a
working WeCom SDK or live WeCom credentials, but it **does** require:

- a PostgreSQL database
- a bootstrapped default tenant row
- password auth mode for local login

### Setup

```bash
# 1. Clone the repo
git clone <repo-url>
cd wecom-archive-365

# 2. Enter backend directory
cd backend

# 3. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Configure environment
cp ../.env.example .env

# 6. Generate a local password hash
python -c "from app.auth import hash_password; print(hash_password('change-me'))"

# 7. Edit backend/.env
# Required for local bring-up:
#   DATABASE_URL=postgresql://postgres:change-me@localhost:5432/wecom_archive
#   AUTH_MODE=password
#   ADMIN_USERNAME=admin
#   ADMIN_PASSWORD_HASH=<paste the generated hash>
#
# Still required by bootstrap_default_tenant.py even in password mode:
#   WECOM_CORP_ID=dev-corp
#   WECOM_AGENT_ID=dev-agent
#   WECOM_OAUTH_SECRET=dev-oauth-secret
#
# Optional for local bootstrap:
#   ADMIN_DOMAIN=localhost

# 8. Initialize schema + default tenant
alembic upgrade head
python scripts/bootstrap_default_tenant.py
```

### Run

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8035
```

Health check: `http://127.0.0.1:8035/health` → `{"status": "ok"}`

Interactive API docs: `http://127.0.0.1:8035/docs`

Admin login: `http://127.0.0.1:8035/admin/login`

Admin console: `http://127.0.0.1:8035/admin/conversations`

### Optional: Load Mock Archive Data

```bash
python scripts/mock_ingest.py
```

This is the fastest way to exercise the review console locally without real
WeCom traffic.

### What Needs Real WeCom Integration

The following features require real WeCom credentials and, for SDK-backed
paths, a valid `WECOM_SDK_LIB_PATH`:

- `AUTH_MODE=wecom`
- `sync_wecom_archive_once.py`
- `decrypt_wecom_messages_once.py`
- `download_wecom_media_once.py`
- `sync_contact_display_names_once.py`

---

## Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | Python FastAPI | REST API + server-rendered HTML |
| Database | PostgreSQL 14+ | Primary store |
| Auth | WeCom OAuth (`snsapi_base`) or password fallback | Switchable via `AUTH_MODE` |
| Media storage | Pluggable provider — local disk (Phase 1) | `MEDIA_STORAGE_PROVIDER=local` |
| Deploy | Alibaba Cloud ECS + systemd + reverse proxy | Worker timers are versioned here; web service/proxy assets are documented separately |
| UI | Server-rendered HTML + JS | Conversation review console |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         WeCom Platform                          │
│   Conversation Archive API  │  OAuth 2.0 (corp identity)        │
└──────────────┬──────────────┴──────────────────────────────────┘
               │ HTTPS (SDK)
┌──────────────▼──────────────────────────────────────────────────┐
│                     FastAPI Backend (Python)                     │
│                                                                  │
│  ┌──────────────┐  ┌─────────────────┐  ┌────────────────────┐  │
│  │  Sync Worker  │  │   REST API      │  │   Auth Middleware   │  │
│  │  (scheduler)  │  │  (admin routes) │  │   (WeCom OAuth)    │  │
│  └──────┬───────┘  └────────┬────────┘  └────────────────────┘  │
│         │                   │                                    │
│  ┌──────▼───────────────────▼────────────────────────────────┐  │
│  │               PostgreSQL (primary store)                   │  │
│  │  messages · rooms · media_refs · sync_cursors             │  │
│  │  tenants · wecom_configs · admin_users · sessions         │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Media Storage Provider (pluggable)           │   │
│  │   local disk (implemented)  │  OSS/S3 (future)            │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────────────┐
│              Admin UI (server-rendered HTML + JS)                │
│              Conversation Review Console (three-column)         │
│              System Diagnostics page (message reachability)     │
└─────────────────────────────────────────────────────────────────┘
```

For a detailed architecture reference, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Capabilities

### Implemented

| Capability | Status | Details |
|-----------|--------|---------|
| WeCom Archive API integration | ✅ | SDK-based encrypted message pull |
| Message decryption | ✅ | RSA + AES decryption pipeline |
| Tenant foundation | ✅ | Multi-tenant data model (RND-156) |
| Auth — WeCom OAuth | ✅ | Employee login via WeCom (RND-110) |
| Auth — Password fallback | ✅ | Temporary mode for dev (RND-112) |
| Conversation review console | ✅ | Three-column UI (RND-154/157) |
| Message timeline | ✅ | Ordered timeline with auto-load older |
| Auto-refresh | ✅ | Periodic refresh (RND-153) |
| i18n / language switching | ✅ | Chinese + English (RND-157) |
| Media download worker | ✅ | Scheduled image media download (RND-151/168) |
| Media storage abstraction | ✅ | Pluggable provider contract (RND-185) |
| System diagnostics | ✅ | Message reachability audit page (RND-180) |
| Message type display | ✅ | Named placeholders for all WeCom types (RND-173/177) |
| WeCom chat bubble style | ✅ | RND-154 matching WeCom appearance |
| Corp ID uniqueness | ✅ | RND-184 constraint |
| Company homepage | ✅ | For ICP beian filing (RND-171) |

### In Progress / Recent

- (See [docs/ai/current-status.md](docs/ai/current-status.md) for the latest status)

---

## Project Structure

```
wecom-archive-365/
├── backend/
│   ├── app/
│   │   ├── main.py                   # FastAPI app entry point + HTML routes
│   │   ├── auth.py                   # Auth dependencies, session handling
│   │   ├── media_storage.py          # Media storage provider abstraction
│   │   ├── media_classification.py   # Media type detection
│   │   ├── reachability_audit.py     # Message reachability audit logic
│   │   ├── wecom_contacts.py         # WeCom contact sync
│   │   ├── i18n_assets.py            # i18n JS asset loader
│   │   ├── display_names.py          # Display name resolution
│   │   ├── db/
│   │   │   ├── models.py             # SQLAlchemy ORM models
│   │   │   ├── session.py            # DB session management
│   │   │   ├── base.py               # Declarative base
│   │   │   └── contacts.py           # Contact DB operations
│   │   ├── routers/
│   │   │   ├── auth.py               # Auth endpoints (OAuth + password)
│   │   │   ├── conversations.py      # Conversation aggregation API
│   │   │   ├── reachability_audit.py # Reachability audit API
│   │   │   └── wecom_events.py       # WeCom event callback
│   │   ├── sdk/
│   │   │   └── wecom_sdk.py          # WeCom C SDK wrapper
│   │   └── assets/
│   │       └── i18n.js               # Locale registry + i18n helpers
│   ├── scripts/
│   │   ├── run_archive_worker_once.py     # Worker entrypoint (sync + decrypt)
│   │   ├── sync_wecom_archive_once.py     # Archive pull
│   │   ├── decrypt_wecom_messages_once.py # Decryption pipeline
│   │   ├── download_wecom_media_once.py   # Unified media download (image/voice/video/file/emotion)
│   │   ├── bootstrap_default_tenant.py    # First-time tenant setup
│   │   ├── backfill_missing_seqs_once.py  # Data repair
│   │   ├── sync_contact_display_names_once.py  # Contact name sync
│   │   ├── mock_ingest.py                 # Mock data for dev
│   │   └── smoke_*.py                     # Smoke tests
│   ├── alembic/                       # DB migrations
│   └── requirements.txt
├── docs/
│   ├── ARCHITECTURE.md               # Architecture reference
│   ├── API.md                        # Route catalog + request/response notes
│   ├── DEPLOYMENT.md                 # Local/prod deployment guidance
│   ├── DATA_MODEL.md                  # Database schema documentation
│   ├── AGENTS.md                      # AI agent roster and handoff
│   ├── CONVERSATION_REVIEW_CONSOLE_PRD.md   # PRD (spec)
│   ├── wecom_archive_worker_runbook.md      # Worker deployment runbook
│   ├── wecom_archive_media_download_runbook.md  # Media download runbook
│   ├── ai/                            # AI context docs (see docs/ai/)
│   └── research/                      # Research documents
├── deploy/
│   └── systemd/                       # systemd service/timer units
├── scripts/
│   └── deploy_server.sh               # Deployment automation
├── static_site/
│   └── company_homepage/              # Beian ICP filing homepage
├── .env.example                       # Environment variable template
├── DEV_AGENT_RULES.md                 # AI agent working rules
└── README.md
```

---

## Secrets Warning

> **Never commit `.env` or any file containing real secrets.**
>
> `.env` is gitignored. Use `.env.example` as the template.
> See [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) §4 for the full secrets policy.

---

## Documentation

| Document | Audience | Purpose |
|----------|----------|---------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Developers | System architecture, data flow, auth, deployment |
| [docs/API.md](docs/API.md) | Developers | HTTP route catalog and auth expectations |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Developers / Ops | Local bring-up, server bootstrap, repo-owned vs operator-managed deploy assets |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Developers | PostgreSQL schema and design decisions |
| [docs/AGENTS.md](docs/AGENTS.md) | AI Agents | Agent roster, responsibilities, handoff protocol |
| [docs/ai/project-overview.md](docs/ai/project-overview.md) | AI Agents | Project summary for first-time AI agents |
| [docs/ai/architecture-summary.md](docs/ai/architecture-summary.md) | AI Agents | Concise architecture for AI consumption |
| [docs/ai/business-terms.md](docs/ai/business-terms.md) | AI Agents | Domain terminology |
| [docs/ai/current-status.md](docs/ai/current-status.md) | Everyone | What's done, what's next |
| [docs/ai/onboarding.md](docs/ai/onboarding.md) | AI Agents | Recommended reading order |
| [docs/ai/known-pitfalls.md](docs/ai/known-pitfalls.md) | Developers | Common traps and rules |
| [docs/wecom_archive_worker_runbook.md](docs/wecom_archive_worker_runbook.md) | Ops | Archive worker deployment and operation |
| [docs/wecom_archive_media_download_runbook.md](docs/wecom_archive_media_download_runbook.md) | Ops | Media download timer deployment |
| [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) | AI Agents | Binding working rules |

---

## Contributing

This project is built by AI agents under human review.
Read [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) and [docs/AGENTS.md](docs/AGENTS.md) before making any change.
