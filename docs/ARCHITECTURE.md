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
│  │   QiniuStorageProvider (implemented, RND-174 — optional,  │   │
│  │     per-row storage_backend; local remains default/       │   │
│  │     rollback)                                             │   │
│  │   Alibaba OSS / S3 (not implemented, reserved config)     │   │
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
| Media storage | Pluggable provider (`MediaStorageProvider` interface) | ✅ Local disk (RND-185) + Qiniu Kodo, optional (RND-174); OSS/S3 contract ready |
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

Media files are served via the authenticated API route (`GET /api/conversations/{id}/messages/{msgid}/media`), which performs tenant authorization before resolving any media storage provider. The provider used to serve a given row is resolved from that row's own `storage_backend`/`storage_ref` columns (RND-174), not from the deployment-wide default write provider — so local and Qiniu-backed rows can coexist safely in the same deployment (see §5).

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
| Local disk | `MEDIA_STORAGE_PROVIDER=local` (or `STORAGE_BACKEND=local`) | ✅ Implemented (RND-185). Default, and the rollback target. |
| Qiniu Kodo | `MEDIA_STORAGE_PROVIDER=qiniu_kodo` | ✅ Implemented (RND-174). Optional — requires `QINIU_*` config (see §6). |
| Alibaba Cloud OSS | `STORAGE_BACKEND=oss` | Contract defined, not implemented |
| S3-compatible | `STORAGE_BACKEND=s3` | Reserved placeholder |

The storage layer is abstracted behind the `MediaStorageProvider` interface (`backend/app/media_storage.py`). `MEDIA_STORAGE_PROVIDER` selects the **default write provider** — where *new* media is uploaded — not how existing rows are read.

**Per-row storage resolution (RND-174).** Every `media_files` row records its own `storage_backend` ("local" or "qiniu_kodo") and `storage_ref` (that provider's own reference — a local path or a Qiniu object key). Reads always resolve the provider from the row, never from the current `MEDIA_STORAGE_PROVIDER` value. This is what makes the following safe:

- **Mixed storage.** Local-backed and Qiniu-backed rows can coexist in the same deployment, even the same conversation timeline — each is served through its own recorded provider.
- **Cutover.** Setting `MEDIA_STORAGE_PROVIDER=qiniu_kodo` only changes where *new* downloads are written. Every existing row keeps using the provider recorded on it.
- **Rollback.** Setting `MEDIA_STORAGE_PROVIDER` back to `local` only changes new writes again. Rows already written to Qiniu (`storage_backend=qiniu_kodo`) are **not** reinterpreted as local and are **not** automatically migrated — they remain readable only as long as Qiniu credentials stay configured. A full rollback off Qiniu (no Qiniu access retained at all) requires migrating those rows' bytes back to local storage first; RND-186 migrates the other direction (Local → Qiniu) only — a Qiniu → Local reverse-migration tool remains unimplemented. Simply flipping the env var back does not do this.

Legacy rows written before this ticket (or migration 0005's backfill) are stamped `storage_backend="local"`, `storage_ref=<their local_path>`; `local_path` itself is retained as a legacy/local-only compatibility field and is never treated as an authoritative Qiniu reference.

`STORAGE_LOCAL_PATH` applies only to rows with `storage_backend=local` — it has no effect on Qiniu-backed rows, which are resolved entirely through `QINIU_*` configuration instead.

Object keys for Qiniu are tenant-scoped and deterministic: `tenants/{tenant_id}/{category}/{archive_message_id}{ext}`, where `category` is `images` today (the only media type the download worker produces) and also `videos`/`voice`/`files` for a row migrated by RND-186 with that `file_type` (see below) — same key shape either way, so nothing downstream needs to special-case a migrated row.

**Qiniu bucket must be private.** This app never assumes public-read access and never issues a permanent public object URL. Two controlled access paths exist for Qiniu-backed media:

- The always-available backend proxy (`GET /api/conversations/{id}/messages/{msgid}/media`, RND-174): the backend fetches the object server-side using a short-lived (60s) internal signed download request built from `QINIU_DOMAIN`, and streams the bytes back — no Qiniu URL or credential ever reaches the client through this route.
- The unified media access descriptor (`GET /api/conversations/{id}/messages/{msgid}/media/access`, RND-187 — **implemented locally, developer re-acceptance pending, not yet deployed to production**): after the exact same tenant/ownership authorization as the proxy route, plus an object-key tenant-prefix check, the backend mints a short-lived, single-object Signed URL (official Qiniu SDK) and returns it to the browser, which then fetches the image directly from `media.crowntime.cn` — the image bytes no longer round-trip through this backend. TTL is `MEDIA_SIGNED_URL_TTL_SECONDS` (default 900s, bounded 60–3600s). Local-backed media keeps using the proxy route unchanged (the descriptor's `access_type="proxy"` case). See [API.md](API.md) and [ops/media_storage_ops.md](ops/media_storage_ops.md) for the full contract, TTL config, and logging-redaction rules.

`QINIU_DOMAIN` **must** be a full `https://` base URL — `http://` and bare hostnames are rejected at provider-construction time.

**Before enabling Qiniu**, refresh backend dependencies (`pip install -r requirements.txt` inside the venv) so the `qiniu` SDK package is present — the app does not require it in local mode, but `qiniu_kodo` selection will fail fast if it and/or `QINIU_*` config are missing.

See [research/rnd_185_media_storage_abstraction.md](research/rnd_185_media_storage_abstraction.md) for the base provider contract and [research/rnd_174_qiniu_kodo_provider.md](research/rnd_174_qiniu_kodo_provider.md) for the Qiniu provider, per-row storage model, and rollback details.

**Historical Local → Qiniu media migration (RND-186)** is implemented and passing the full test suite: `backend/scripts/migrate_local_media_to_qiniu.py` is a manual, repeatable, resumable tool that migrates already-downloaded `media_files` rows from `storage_backend="local"` to `"qiniu_kodo"`, for any content-verified supported media type (`image`/`video`/`voice`/`file` — not images only), with full storage metadata (`bucket`/`mime_type`/`checksum_sha256`) persisted per row. It has **not** been executed in production yet — no historical rows have actually been migrated. Migrating a video/voice/file row does not make it retrievable through the media route yet (see the note below and [research/rnd_186_local_qiniu_migration.md](research/rnd_186_local_qiniu_migration.md)). See [ops/media_storage_ops.md](ops/media_storage_ops.md) for CLI usage.

Client-facing Signed URL / CDN delivery (RND-187) is implemented locally and passing the full test suite; developer re-acceptance is pending and it has **not** been deployed to production — see the Storage Strategy note above and [API.md](API.md) for the `GET .../media/access` contract.

**Media serving remains image-only regardless of RND-186.** `GET /api/conversations/{id}/messages/{msgid}/media` and the `.../media/access` descriptor both still gate on `msgtype == "image"` — this is unchanged by RND-186 on purpose (Media Access API changes are explicitly out of that ticket's scope). A migrated video/voice/file row is durably stored in Qiniu with complete metadata but not yet servable through any existing route; extending media serving to those types is tracked as a suggested follow-up ticket (see [research/rnd_186_local_qiniu_migration.md](research/rnd_186_local_qiniu_migration.md)), not implemented here.

---

## 6. Deployment

| Concern | Choice |
|---------|--------|
| Server | Alibaba Cloud ECS (single instance) |
| Process manager | systemd for worker/media timers; main web service is operator-managed |
| Reverse proxy | Operator-managed reverse proxy in front of port 8035 |
| Database | PostgreSQL on the same ECS instance (or RDS) |
| Media | Mixed local filesystem + optional Qiniu Kodo object storage (RND-174). Each `media_files` row records its own storage backend; new writes use the configured default provider (`MEDIA_STORAGE_PROVIDER`), reads are selected per row. See §5. |
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
