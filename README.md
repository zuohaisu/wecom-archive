# 365 WeCom Archive

Internal WeCom conversation archive, media storage, and admin search system for 365.

---

## Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | Python FastAPI | REST API |
| Database | PostgreSQL | Primary store |
| Media storage | Local disk (phase 1) → Alibaba Cloud OSS | Configurable via `STORAGE_BACKEND` |
| UI | Server-rendered HTML | Not implemented yet |
| Deploy | Alibaba Cloud ECS + systemd + Nginx | Phase 2 |

---

## Local Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ running locally (or via Docker)

### Steps

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
# Edit .env and fill in your local values (DB password, etc.)
```

### Run

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8035
```

The API is available at `http://127.0.0.1:8035`.
Health check: `http://127.0.0.1:8035/health` → `{"status": "ok"}`

Interactive docs: `http://127.0.0.1:8035/docs`

---

## Secrets Warning

> **Never commit `.env` or any file containing real secrets.**
>
> `.env` is gitignored. Use `.env.example` as the template.
> See [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) §4 for the full secrets policy.

---

## Project Structure

```
wecom-archive-365/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   └── main.py        # FastAPI app entry point
│   └── requirements.txt
├── docs/
│   └── AGENTS.md          # Agent roster and handoff protocol
├── .env.example           # Environment variable template
├── DEV_AGENT_RULES.md     # AI agent working rules
└── README.md
```

---

## Contributing

This project is built by AI agents under human review.
Read [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) and [docs/AGENTS.md](docs/AGENTS.md) before making any change.
