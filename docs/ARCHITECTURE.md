# Architecture — 365 WeCom Archive

Reference document covering product boundary, components, data flow, auth, storage, deployment, and explicit non-goals.

---

## 1. Product Boundary

365 WeCom Archive is an **internal, admin-only** system that:

- Pulls conversation messages and media from the WeCom Conversation Archive API on a scheduled basis.
- Decrypts messages using RSA + AES (WeCom SDK encryption scheme).
- Stores messages, attachments, and metadata in company-controlled PostgreSQL and media store.
- Exposes a **Conversation Review Console** (three-column server-rendered UI) and a **Message Reachability Diagnostics** page to authorized administrators.

It is **not** a public-facing product. Access is restricted to employees authenticated via WeCom OAuth (self-built app, `snsapi_base` scope) or a password fallback mode. Any active internal employee can log in; there is no separate allow-list.

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
│  │  Sync Worker  │  │   REST API      │  │   Auth Middleware   │  │
│  │  (systemd)    │  │  (admin routes) │  │   (WeCom OAuth)    │  │
│  └──────┬───────┘  └────────┬────────┘  └────────────────────┘  │
│         │                   │                                    │
│  ┌──────▼───────────────────▼────────────────────────────────┐  │
│  │               PostgreSQL (primary store)                   │  │
│  │  archive_messages · archive_message_recipients             │  │
│  │  media_files · sync_states · contacts                      │  │
│  │  tenants · tenant_wecom_configs                            │  │
│  │  admin_users · admin_sessions                              │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Media Storage Provider (pluggable)           │   │
│  │   LocalStorageProvider (implemented, RND-185)             │   │
│  │   Qiniu Kodo / Alibaba OSS / S3 (future, RND-186)        │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────────────┐
│              Admin UI (server-rendered HTML + JS)                │
│                                                                  │
│  Conversation Review Console (three-column, WeCom-style)        │
│    ├── Left column: entity selector (staff / contact)           │
│    ├── Middle column: conversation list                         │
│    └── Right column: message timeline                           │
│                                                                  │
│  System Diagnostics Page (message reachability statistics)      │
│  Message Search (/admin/messages)                                │
└─────────────────────────────────────────────────────────────────┘
```

### Component summary

| Component | Technology | Status |
|-----------|-----------|--------|
| REST API | Python 3.11 + FastAPI | ✅ Fully implemented |
| Database | PostgreSQL 14+ | ✅ Schema deployed, Alembic migrations active |
| Media storage | Pluggable provider (`MediaStorageProvider` interface) | ✅ Local disk (RND-185); OSS/S3 contract ready |
| Sync worker | Python scripts via systemd timer (`OnCalendar=*:0/5`) | ✅ Implemented; worker/media timer units are versioned in repo |
| Admin UI — Conversation Review Console | Server-rendered HTML + JS (FastAPI) | ✅ Three-column, WeCom-style (RND-154/157) |
| Admin UI — Diagnostics | Server-rendered HTML + JS | ✅ Message reachability audit (RND-180) |
| Auth — WeCom OAuth | WeCom OAuth 2.0 (`snsapi_base`) | ✅ RND-110 |
| Auth — Password fallback | PBKDF2 + env vars | ✅ RND-112 |

---

## 3. Data Flow

### 3.1 Message Sync (pull)

```
[systemd timer: OnCalendar=*:0/5]
         │
         ▼
run_archive_worker_once.py
  ├── Acquires file lock (WORKER_LOCK_PATH)
  ├── sync_wecom_archive_once.py
  │     ├── Reads cursor from sync_states table
  │     ├── WeCom Archive API → paginated message batch (encrypted)
  │     └── Stores encrypted envelope + metadata in archive_messages
  │
  └── decrypt_wecom_messages_once.py
        ├── Reads pending archive_messages rows
        ├── Decrypts with RSA private key (WECOM_PRIVATE_KEY_PATH)
        ├── Populates decrypted_payload + content_text + extracted fields
        └── Creates archive_message_recipients rows from tolist
```

Cursor is persisted after each successful batch so restarts are safe and non-duplicating.

### 3.2 Media Download (separate, independent timer)

```
[systemd timer: OnBootSec=5min, OnUnitActiveSec=5min]
         │
         ▼
download_wecom_image_media_once.py
  ├── Acquires file lock (MEDIA_DOWNLOAD_LOCK_PATH)
  ├── Selects candidate image messages (--since-hours 72, --limit 20)
  ├── Downloads via WeCom SDK → .part file
  ├── Validates bytes (magic-byte detection)
  └── Publishes → media_files row updated to download_status='downloaded'
```

### 3.3 Admin Review (read)

```
Browser (admin user)
         │
         ├── GET  /api/monitored-accounts  (staff mode entities)
         ├── GET  /api/contacts            (contact mode entities)
         ├── GET  /api/conversations       (conversation list)
         ├── GET  /api/conversations/{id}/messages (timeline with pagination)
         │
         ▼
FastAPI → verify session (get_current_user dependency)
         │
         ▼
PostgreSQL → tenant-scoped queries (WHERE tenant_id = ?)
         │
         ▼
JSON response → rendered as HTML (client-side JS)
```

Media files are served via the authenticated API route (`GET /api/conversations/{id}/messages/{msgid}/media`), which performs tenant authorization before checking the media storage provider.

---

## 4. Auth Flow

Two separate credential domains. Auth mode is controlled by `AUTH_MODE`.

### 4.1 Archive Credential (server-to-WeCom)

- Used by the sync worker to call the WeCom Conversation Archive API.
- Configured via: `WECOM_CORP_ID`, `WECOM_ARCHIVE_SECRET`, `WECOM_PRIVATE_KEY_PATH`, `WECOM_PUBLIC_KEY_VERSION`.
- Never exposed to the browser. Lives only on the server.

### 4.2 Admin Login (human-to-system)

Auth mode is controlled by the `AUTH_MODE` environment variable:

| `AUTH_MODE` | Behavior |
|------------|----------|
| `wecom` (default) | WeCom OAuth employee login |
| `password` | Temporary username/password fallback |

Both modes share the same session model (`admin_sessions` table), the same cookie (`session_id`), and the same tenant-scoped query model.

#### WeCom OAuth mode (`AUTH_MODE=wecom`)

- WeCom self-built app OAuth 2.0 (`snsapi_base` scope, silent authorization).
- Env vars: `WECOM_CORP_ID`, `WECOM_AGENT_ID`, `WECOM_OAUTH_SECRET`, `ADMIN_DOMAIN`.
- Any active WeCom internal employee (status=1, enable=1) may log in.
- The backend resolves `tenant_id` from the `TenantWecomConfig` row matching `corp_id`.
- CSRF protection: random single-use state token with 5-minute TTL.

```
Desktop browser → GET /admin/login (WeCom button)
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
         → set session_id cookie (HttpOnly, SameSite=Lax, 8h TTL)
         → redirect to /admin/conversations
```

#### Password fallback mode (`AUTH_MODE=password`)

- Temporary mode for when WeCom OAuth domain authorization is pending.
- Env vars: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`.
- Hash: `pbkdf2:sha256:260000:<salt_b64>:<hash_b64>`.
- Session cookie is identical in flags to WeCom OAuth sessions.

#### Shared session behavior (both modes)

- Server-side session (`admin_sessions` table); HttpOnly cookie (`session_id`), 8-hour TTL, SameSite=Lax.
- All archive API and HTML admin routes scope every query by `session.tenant_id`.
- `tenant_id` is never accepted from user-supplied request params or headers.
- Logout: `POST /api/auth/logout` revokes the session row and clears the cookie.
- `/api/auth/me`: always returns HTTP 200; `authenticated` field reflects session validity.

---

## 5. Storage Strategy

| Backend | Config value | Status |
|---------|-------------|--------|
| Local disk | `MEDIA_STORAGE_PROVIDER=local` (or `STORAGE_BACKEND=local`) | ✅ Implemented (RND-185) |
| Alibaba Cloud OSS | `STORAGE_BACKEND=oss` | Contract defined, not implemented |
| S3-compatible | `STORAGE_BACKEND=s3` | Reserved placeholder |
| Qiniu Kodo | — | Contract defined for future (RND-186) |

The storage layer is abstracted behind `MediaStorageProvider` interface (`backend/app/media_storage.py`). Switching backends requires only a config change and provider implementation.

See [research/rnd_185_media_storage_abstraction.md](research/rnd_185_media_storage_abstraction.md) for the provider contract details.

---

## 6. Deployment

| Concern | Choice |
|---------|--------|
| Server | Alibaba Cloud ECS (single instance) |
| Process manager | systemd for worker/media timers; main web service is operator-managed |
| Reverse proxy | Operator-managed reverse proxy in front of port 8035 |
| Database | PostgreSQL on the same ECS instance (or RDS) |
| Media | Local disk on ECS (Phase 1) |
| Secrets | Environment variables injected by systemd `EnvironmentFile` — never in code |

### systemd units

| Unit | Type | Schedule | Purpose |
|------|------|----------|---------|
| `wecom-archive-worker.service` | oneshot | `OnCalendar=*:0/5` | Sync + decrypt archive messages |
| `wecom-archive-worker.timer` | timer | — | Activates above |
| `wecom-archive-media-download.service` | oneshot | `OnUnitActiveSec=5min` | Download recent image media |
| `wecom-archive-media-download.timer` | timer | — | Activates above |

The repository does **not** currently version:

- the main `wecom-archive-365.service` unit
- the reverse-proxy configuration

See [DEPLOYMENT.md](DEPLOYMENT.md) for the explicit repo-owned vs
operator-managed boundary.

---

## 7. Tenant Model

The system is tenant-aware (RND-156): a single deployment can support multiple companies (tenants) in the future.

- Each `tenant` has its own `tenant_wecom_configs` (WeCom credentials).
- All archive tables are scoped by `tenant_id`.
- Sessions carry `tenant_id` as the authorization scope.
- Current MVP: single default tenant.

---

## 8. Conversation Model

### Review Console Identity

| Type | Identity Key | Example |
|------|-------------|---------|
| Direct conversation | Sorted pair of participant IDs | `direct__staff_001___contact_abc` |
| Group conversation | `roomid` | `wr_xxxxxxxxxxxxxxxxxxxxx` |

### Monitored Accounts / Archive Seats

No formal archive-seat roster exists. Two signals are combined:
1. Legacy `"staff_"` prefix convention (mock/dev fixtures).
2. WeCom user IDs that are BOTH authenticated admin users AND observed as senders/recipients in the archive.

---

## 9. Key Environment Variables

The current source of truth is `.env.example`.

Important caveat:

- `DATABASE_URL` is required at app runtime
- some optional variables are script-specific rather than global runtime settings
- future storage-provider placeholders should not be treated as implemented config

---

## 10. Explicit Non-Goals

The following are **out of scope**:

| Non-goal | Reason |
|----------|--------|
| Public-facing access | Internal compliance tool |
| Real-time message streaming | Sync is scheduled pull, not webhook push |
| Mobile app or responsive UI | Admin-only on desktop |
| End-to-end encryption at rest | WeCom decryption happens server-side at sync time |
| Message deletion or editing | Archive is append-only; no write-back to WeCom |
| Notification or alerting system | Out of scope |
| AI summarization or analysis | Not planned for current phase |

---

_Last updated: 2026-07-10 — Documentation refresh_
