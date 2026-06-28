# Architecture — 365 WeCom Archive

Reference document for contributors before SDK integration and data model work.
Covers product boundary, components, data flow, auth, storage, deployment, and explicit non-goals.

Related issues: RND-87 (this doc), RND-75 (data model and migration).

---

## 1. Product Boundary

365 WeCom Archive is an **internal, admin-only** system that:

- Pulls conversation messages and media from the WeCom Conversation Archive API on a scheduled basis.
- Stores messages (text, attachments, metadata) in a company-controlled database and media store.
- Exposes a search and review interface to authorized 365 administrators.

It is **not** a public-facing product. Access is restricted to employees authenticated via WeCom OAuth (self-built app, `snsapi_base` scope). Any active internal employee can log in; there is no separate allow-list.

---

## 2. System Components

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
│  │  Sync Worker │  │   REST API      │  │   Auth Middleware   │  │
│  │  (scheduler) │  │  (admin routes) │  │   (WeCom OAuth)    │  │
│  └──────┬───────┘  └────────┬────────┘  └────────────────────┘  │
│         │                   │                                    │
│  ┌──────▼───────────────────▼────────────────────────────────┐  │
│  │               PostgreSQL (primary store)                   │  │
│  │  messages · users · rooms · media_refs · sync_cursors     │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Media Storage (pluggable)                    │   │
│  │   Phase 1: local disk  │  Later: Alibaba Cloud OSS        │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────────────┐
│                     Admin UI (server-rendered HTML)              │
│                     Served by FastAPI (Phase 2)                  │
└─────────────────────────────────────────────────────────────────┘
```

### Component summary

| Component | Technology | Status |
|---|---|---|
| REST API | Python 3.11 + FastAPI | Stub exists |
| Database | PostgreSQL 14+ | Schema not yet defined (RND-75) |
| Media storage | Local disk → OSS (configurable) | Not yet implemented |
| Sync worker | Python scheduler (to be chosen) | Not yet implemented |
| Admin UI | Server-rendered HTML via FastAPI | Phase 2 |
| WeCom SDK integration | `wework-sdk` or equivalent | Not yet integrated |

---

## 3. Data Flow

### 3.1 Message Sync (pull)

```
[Scheduled trigger]
        │
        ▼
Sync Worker reads cursor from DB
        │
        ▼
WeCom Archive API → paginated message batch (encrypted)
        │
        ▼
Decrypt with private key (RSA, `WECOM_PRIVATE_KEY_PATH`)
        │
        ├──▶ Text / structured messages → PostgreSQL (messages table)
        │
        └──▶ Media attachments → download → media store
                                         → media_refs row in PostgreSQL
```

Cursor is persisted after each successful batch so restarts are safe and non-duplicating.

### 3.2 Admin Search (read)

```
Browser (admin user)
        │  HTTP GET /api/messages?q=...
        ▼
FastAPI → verify session (WeCom OAuth token)
        │
        ▼
PostgreSQL full-text or keyword query
        │
        ▼
JSON response → rendered HTML page
```

Media files are served via a signed URL (local path in Phase 1, OSS pre-signed URL later).

---

## 4. Auth Flow

Two separate credential domains:

### 4.1 Archive Credential (server-to-WeCom)

- Used by the sync worker to call the WeCom Conversation Archive API.
- Configured via: `WECOM_CORP_ID`, `WECOM_ARCHIVE_SECRET`, `WECOM_PRIVATE_KEY_PATH`, `WECOM_PUBLIC_KEY_VERSION`.
- Never exposed to the browser. Lives only on the server.

### 4.2 Admin Login (human-to-system) — RND-110

- WeCom self-built app OAuth 2.0 (`snsapi_base` scope, silent authorization).
- Env vars: `WECOM_CORP_ID`, `WECOM_AGENT_ID`, `WECOM_OAUTH_SECRET`, `ADMIN_DOMAIN`.
- Any active WeCom internal employee (status=1, enable=1) may log in.
- The backend resolves `tenant_id` from the `TenantWecomConfig` row matching `corp_id`.
- A server-side session (`admin_sessions` table) is created; HTTP-only cookie (`session_id`), 8-hour TTL, SameSite=Lax.
- All archive API and HTML admin routes scope every DB query by `session.tenant_id`.
- CSRF protection: random single-use state token with 5-minute TTL.
- No password authentication. No separate user accounts. No allow-list file.

```
Desktop browser → GET /admin/login (login page with WeCom QR button)
        → GET /api/auth/wecom/login
        → redirect to open.weixin.qq.com/connect/oauth2/authorize
        → user scans QR / approves in WeCom
        → GET /api/auth/wecom/callback?code=...&state=...
        → validate state (single-use CSRF check)
        → GET /cgi-bin/gettoken → access_token (cached ≤7000s)
        → GET /cgi-bin/user/getuserinfo → UserId
        → GET /cgi-bin/user/get → verify active employee
        → resolve tenant_id from TenantWecomConfig by corp_id
        → upsert admin_users, create admin_sessions row
        → set session_id cookie (HttpOnly, SameSite=Lax)
        → redirect to /admin/conversations
```

`/api/wecom/archive/events` is a WeCom server-to-server callback and is explicitly **not** protected by session auth.

---

## 5. Storage Strategy

| Phase | Backend | Config value | Where |
|---|---|---|---|
| Phase 1 | Local disk | `STORAGE_BACKEND=local` | `STORAGE_LOCAL_PATH` (default `./data/media`) |
| Phase 2+ | Alibaba Cloud OSS | `STORAGE_BACKEND=oss` | `OSS_ENDPOINT`, `OSS_BUCKET`, key pair |

The storage layer will be abstracted behind a thin interface so the sync worker and API routes are not coupled to a specific backend. Switching backends requires only a config change, not code changes.

`STORAGE_BACKEND=s3` is reserved as a placeholder for other S3-compatible stores.

---

## 6. Deployment Target

| Concern | Choice |
|---|---|
| Server | Alibaba Cloud ECS (single instance, Phase 1) |
| Process manager | systemd (one service unit for the FastAPI app + worker) |
| Reverse proxy | Nginx (TLS termination, static file serving, port 443 → 8035) |
| Database | PostgreSQL on the same ECS instance (Phase 1) or managed RDS (later) |
| Media | Local disk on ECS (Phase 1), OSS bucket (Phase 2) |
| Secrets | Environment variables injected by systemd `EnvironmentFile` — never in code |

Phase 1 intentionally avoids Kubernetes, container orchestration, or managed container services to keep operational complexity low.

---

## 7. Phase Scope

### Phase 1 (current focus)

- FastAPI app with health endpoint (done).
- PostgreSQL schema for messages, users, rooms, media refs, sync cursors (RND-75).
- WeCom SDK integration for archive pull (after RND-75).
- Sync worker: pull, decrypt, store messages and media.
- WeCom OAuth admin login.
- Minimal admin search API (JSON responses only).
- Local disk media storage.
- Deployed to a single ECS instance.

### Phase 2 (after Phase 1 is stable)

- Server-rendered admin UI (search, message viewer, media preview).
- Switch media storage to Alibaba Cloud OSS.
- Signed URL media serving.
- Scheduled sync with retry and alerting.
- PostgreSQL moved to managed RDS.

### Later / unscheduled

- Full-text search engine (Elasticsearch or pg_tsvector tuning).
- Audit log for admin searches.
- Multi-corp support.
- Export functionality.

---

## 8. Explicit Non-Goals

The following are **out of scope** and should not be implemented or designed for without a new approved issue:

| Non-goal | Reason |
|---|---|
| Public-facing access | This is an internal compliance tool. |
| Real-time message streaming | Sync is scheduled pull, not webhook push. |
| Chat replay / conversation reconstruction UI | Not in Phase 1 scope. |
| Mobile app or responsive UI | Admin-only on desktop. |
| Multi-tenant / multi-company support | Single-corp deployment. |
| End-to-end encryption at rest | Not in scope; WeCom decryption happens server-side at sync time. |
| Message deletion or editing | Archive is append-only; no write-back to WeCom. |
| Notification or alerting system | Out of scope for Phase 1. |
| AI summarization or analysis of messages | Not planned. |

---

## 9. Key Environment Variables

Defined fully in `.env.example`. Summarized here for architectural reference:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` / `DB_*` | PostgreSQL connection |
| `WECOM_CORP_ID` | WeCom corporation identity |
| `WECOM_ARCHIVE_SECRET` | Archive API access credential |
| `WECOM_PRIVATE_KEY_PATH` | Path to RSA private key for message decryption |
| `WECOM_PUBLIC_KEY_VERSION` | Key version for WeCom encryption |
| `WECOM_AGENT_ID` | WeCom self-built app agent ID for OAuth login |
| `WECOM_OAUTH_SECRET` | App secret for OAuth (separate from archive secret) |
| `ADMIN_DOMAIN` | Domain registered as WeCom OAuth trusted domain; used to construct callback URL |
| `STORAGE_BACKEND` | `local` \| `oss` \| `s3` |
| `STORAGE_LOCAL_PATH` | Root path for local media files |
| `OSS_*` | Alibaba Cloud OSS credentials and endpoint |
| `SYNC_INTERVAL_SECONDS` | How often the sync worker runs |
| `SYNC_LOOKBACK_DAYS` | Historical depth on first sync |

No real values are stored in this document or in the repository.

---

_Last updated: 2026-06-29 — RND-87, RND-110_
