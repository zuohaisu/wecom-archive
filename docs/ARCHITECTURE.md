# Architecture — Crowntime WeCom Archive

Reference document covering product boundary, components, data flow, auth, storage, deployment, and explicit non-goals.

---

## 1. Product Boundary

Crowntime WeCom Archive is an **internal, admin-only** system that:

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
│  │  media_files · media_quota_blocks · tenant_storage_daily   │  │
│  │  sync_states · contacts                                    │  │
│  │  tenants · tenant_wecom_configs                            │  │
│  │  admin_users · admin_sessions                              │  │
│  │  billing_plans · subscriptions · payment_orders/events     │  │
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
| Sync worker | Callback-primary Python worker + 30-minute systemd reconciliation (`OnCalendar=*:0/30`) | ✅ Implemented; worker/media timer units are versioned in repo |
| Admin UI — Conversation Review Console | Server-rendered HTML + JS (FastAPI) | ✅ Three-column, WeCom-style (RND-154/157) |
| Admin UI — Diagnostics | Server-rendered HTML + JS | ✅ Message reachability audit (RND-180) |
| Auth — WeCom OAuth | WeCom OAuth 2.0 (`snsapi_base`) | ✅ RND-110 |
| Auth — Password fallback | PBKDF2 + env vars | ✅ RND-112 |
| Billing — annual purchase | Provider-neutral orders + WeChat Pay API v3 Native | ✅ Implemented locally (RND-380); production enablement remains gated by RND-390 |

---

## 3. Data Flow

### 3.1 Message Sync (pull)

```
[validated WeCom callback] ──non-blocking──► [archive dispatch]
[systemd reconciliation: OnCalendar=*:0/30] ─► [same archive entrypoint]
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
        ├── Reads pending/failed archive_messages rows
        ├── Decrypts with RSA private key (WECOM_PRIVATE_KEY_PATH)
        ├── Populates content_text + extracted fields (structured_content, msgtype,
        │     sender, roomid, msgtime, tolist, sdkfileid) — decrypted_payload itself
        │     is deliberately never populated (data-minimization; see DATA_MODEL.md)
        └── Creates archive_message_recipients rows from tolist
```

Cursor is persisted after each successful batch so restarts are safe and non-duplicating.

### 3.2 Media Download (archive-complete wake-up + reconciliation)

```
[successful archive sync/decrypt commit]
         │  bounded fresh/pending preflight; no media => no wake-up
         ▼
[one mtime-only signal] → wecom-archive-media-event.path/service
         │
         ▼
download_wecom_media_once.py (same unified pipeline: app/media_download.py —
                               image/voice/video/file/emotion + nested media)
  ├── Acquires its own file lock (MEDIA_DOWNLOAD_LOCK_PATH)
  ├── Selects candidate messages (archive-complete: recent/newest-first;
  │   timer: --since-hours 72 --newest-first --retry --limit 20)
  ├── Downloads via WeCom SDK and validates exact payload bytes
  ├── Locks tenant; measures downloaded bytes + active plan quota
  ├── Denied → durable quota_blocked fact; no provider write, no message loss
  └── Allowed → provider publish + media/rollup update in the lock transaction

[systemd reconciliation: OnCalendar=*:15/30] ──► same media entrypoint
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

### 3.4 Annual-plan purchase (write)

```text
Owner browser → POST /api/billing/orders (plan code + idempotency key)
              → server reads authoritative price/term/entitlements
              → WeChat Pay API v3 Native → signed response → code_url
              → GET .../qr → server-generated PNG (raw URL not returned)

WeChat Pay → POST /api/payments/wechat/notify (raw signed body)
           → public-key signature + freshness verification
           → AES-GCM resource decrypt + merchant/order/amount checks
           → payment_events replay record + paid order state
           → provider-neutral subscription activation/renewal
           → subscriptions + history + audit committed atomically
```

The payment provider boundary exposes create/query/close/verified-event
operations and contains no subscription policy. The order service is the only
bridge from a trusted payment fact to subscription activation. This keeps a
future Alipay adapter possible without changing entitlement authority; no
Alipay adapter is implemented by RND-380.

### 3.5 Storage-capacity authority and write gate

`storage_capacity.py` is the single policy boundary. It combines the effective
RND-376 subscription quota with the live tenant-scoped sum of successfully
downloaded media bytes. The browser reads this through
`GET /api/billing/capacity`; it never computes or authorizes capacity.

The worker first receives and validates the complete payload in memory, so the
gate uses exact bytes. It then holds a PostgreSQL row lock on the tenant from
measurement through provider publication, `media_files` persistence and daily
rollup refresh. Concurrent workers therefore cannot spend the same remaining
bytes. Unknown usage and inactive entitlement fail closed. Capacity denial is
a durable, retryable state, separate from SDK/provider failures.

The same owner page reads current plan, dates, and entitlement names through
`GET /api/billing/subscription`. Its trial/paid/expiring/expired/canceled/
unavailable presentation state is classified from server time and the same
RND-376 subscription summary used by permission checks. Browser-supplied
status, quota, or entitlement claims therefore cannot change access.

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
- Any active WeCom internal employee (`user/get.status == 1`) may log in. There is no
  `enable` field in the real `user/get` response (RND-225) — `status` alone gates
  activation: 1=active, 2=disabled, 4=not-activated, 5=left the enterprise. See
  https://developer.work.weixin.qq.com/document/path/90196.
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
- The unified media access descriptor (`GET /api/conversations/{id}/messages/{msgid}/media/access`, RND-187 — **implemented locally, developer re-acceptance pending, not yet deployed to production**): after the exact same tenant/ownership authorization as the proxy route, plus an object-key tenant-prefix check, the backend mints a short-lived, single-object Signed URL (official Qiniu SDK) and returns it to the browser, which then fetches the image directly from `media.example.com` — the image bytes no longer round-trip through this backend. TTL is `MEDIA_SIGNED_URL_TTL_SECONDS` (default 900s, bounded 60–3600s). Local-backed media keeps using the proxy route unchanged (the descriptor's `access_type="proxy"` case). See [API.md](API.md) and [ops/media_storage_ops.md](ops/media_storage_ops.md) for the full contract, TTL config, and logging-redaction rules.

`QINIU_DOMAIN` **must** be a full `https://` base URL — `http://` and bare hostnames are rejected at provider-construction time. The domain is fully config-driven: it may be a Qiniu **CDN acceleration** domain or a Qiniu **origin/source** domain, and switching between them (RND-207) is a value change to this one variable with no code change — see the migration runbook below.

**RND-207 — origin domain, thumbnails, and browser-cacheable signed URLs (implemented locally, not yet deployed):**
- **Retire the CDN domain, move to an origin domain** (`media-origin.example.com`, Scheme B — new domain in parallel, verify, cut over, then retire the CDN domain). Because serving is driven by `QINIU_DOMAIN`, this needs no code change; the full manual procedure and rollback are in [rnd-207-migration-runbook.md](rnd-207-migration-runbook.md).
- **Fixed-window signed URLs:** `GET .../media/access` now snaps each signed URL's absolute expiry to a fixed window boundary (`MEDIA_SIGNED_URL_WINDOW_SECONDS`, defaults to the TTL) via `compute_signed_url_deadline`, so repeated requests for the same object within a window return a **byte-identical** URL — letting the browser reuse its HTTP cache instead of re-downloading on every fresh signature. The private bucket and TTL bounds are unchanged; no long-lived token reaches the client.
- **List/timeline thumbnails:** the list renders a small server-generated thumbnail (a separate stored object under `tenants/{tenant}/thumbnails/…`, **not** a Qiniu CDN image transform) while the viewer still opens the original. `GET .../media/access?variant=thumb` signs the thumbnail; the timeline exposes `thumbnail_access_url` + `image_width`/`image_height` (the latter to reserve a layout box and eliminate image-swap CLS). Thumbnails are JPEG (opaque) / PNG (alpha), EXIF-oriented, with animated GIFs collapsed to a static first frame; generation (`app/media_thumbnails.py`, Pillow) is failure-isolated — a thumbnail never affects original archival/serving. New downloads generate inline; historical images are filled by the manual, idempotent `backend/scripts/backfill_thumbnails_once.py` (migration `0012` adds the `thumbnail_*`/`image_*` columns). Local proxy byte responses also gained a `Cache-Control: private, max-age` header.

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
| `wecom-archive-worker.service` | oneshot | `OnCalendar=*:0/30` | Callback-primary sync/decrypt reconciliation; archive-complete media wake-up |
| `wecom-archive-worker.timer` | timer | — | Activates above |
| `wecom-external-contact-refresh.service` | oneshot | path signal / retry timer | Drains small persisted external-contact metadata refresh batches |
| `wecom-external-contact-refresh.path` | path | shared mtime signal | Starts incremental refresh work without putting identifiers on disk |
| `wecom-external-contact-refresh.timer` | timer | `OnUnitInactiveSec=15min` | Retries ready persisted refresh tasks |
| `wecom-external-contact-reconcile.service` | oneshot | daily timer | Full external-contact metadata reconciliation |
| `wecom-external-contact-reconcile.timer` | timer | `OnCalendar=*-*-* 04:15:00` | Activates above |
| `wecom-archive-reachability-check.service` | oneshot | `OnCalendar=*-*-* 04:30:00` | Full reachability reconciliation |
| `wecom-archive-reachability-check.timer` | timer | — | Activates above |
| `wecom-archive-media-event.service` | oneshot | systemd path signal | Runs existing generic media CLI after archive-complete wake-up |
| `wecom-archive-media-event.path` | path | shared mtime signal | Activates the event service without a queue payload |
| `wecom-archive-media-download.service` | oneshot | `OnCalendar=*:15/30` | Reconcile pending/retryable generic media |
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

### 7.1 Worker tenant/corp scope contract (RND-222)

The three archive workers (`scripts/sync_wecom_archive_once.py`,
`scripts/decrypt_wecom_messages_once.py`,
`scripts/download_wecom_media_once.py`) are thin CLI shells over
application functions in `backend/app/services/{sync,decrypt,media}_worker.py`.
Each shell resolves exactly **one** `(tenant_id, corp_id)` pair per
invocation — `corp_id` from `WECOM_CORP_ID`, `tenant_id` resolved from the
active `tenant_wecom_configs` row matching it (`_require_tenant_id`,
duplicated per-script rather than shared — see each script's own copy) —
and passes `tenant_id` into the service as a **required** parameter with
no default and no `None` fallback. This is a deliberate code-level
contract, not just a convention: none of the three `run_*_once()`
functions can be called without naming a tenant, so a caller can never
accidentally process every tenant's rows in one run. There is currently
no "iterate every tenant" mode; a multi-corp deployment runs one worker
invocation per corp.

**Decrypt tenant-scope audit (RND-222).** Before this ticket,
`decrypt_wecom_messages_once.py` was the one worker whose pending/failed
scan, recipient-repair scan (`repair_missing_recipients`), and
revoke-reconciliation repair scan (`reconcile_pending_revocations`) had no
tenant filter at all — every decrypt run scanned and could mutate *every*
tenant's rows, even though it can only ever hold one corp's RSA private
key. Fixed: `run_decrypt_once()` filters every one of those four
operations by `tenant_id`. Sync and media were already tenant-scoped and
are unchanged. See `backend/app/services/decrypt_worker.py`'s module
docstring for the full analysis.

**Transaction boundaries** (unchanged by the RND-222 extraction — each
service function preserves its worker's original commit pattern exactly):

| Worker | Commit pattern |
|--------|-----------------|
| Sync | Per-run: inserted records commit once, then the seq cursor advances and commits once more. No records fetched → no commit at all. |
| Decrypt | Per-run, single commit: every row mutation, the recipient-repair scan, and the revoke-reconciliation scan happen in memory/via `flush()`, then one `session.commit()` at the end. A commit failure raises `DecryptCommitError`, which the shell translates to `[FAIL] Database commit failed: …` + exit 1. |
| Media | Per-candidate: each download outcome is persisted and committed individually, with rollback + best-effort orphan-object cleanup on a confirmed DB commit failure for that one candidate — one bad candidate never blocks the rest of the run. |

**Reachability automation (RND-339).** After a successful sync and decrypt,
`run_archive_worker_once.py` best-effort invokes the incremental reachability
one-shot. It freezes new successful-message IDs after the last complete
incremental watermark, persists only aggregate runs and internal finding
references, and never resolves findings. The separate daily reconciliation
unit shares the process/tenant-active-run exclusion, scans the last seven days
plus messages behind active findings, and may resolve only after complete
coverage. Its failure, exception, or held lock never changes archive worker
success. `GET /api/admin/reachability-findings` projects an authenticated
tenant's findings through an allowlisted, opaque-cursor contract; it contains
no tenant, archive-message, run, identity, content, path, or error identifier.

**Exit code semantics** are unchanged by this extraction: `0` on success
(including "nothing to do"), `1` on any fatal failure (missing/invalid
env, SDK load/init failure, no active tenant for `WECOM_CORP_ID`, or —
decrypt only — a database commit failure). A worker never partially
exits; every `[INFO]` summary line is printed before the process exits,
even on a non-fatal-but-reported failure (e.g. sync's `GetChatData`
returning a non-zero code still prints the full summary before the
`[FAIL]` + exit 1).

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

### 9.1 Configuration Architecture (RND-223)

Web-app runtime reads are consolidated behind `backend/app/settings.py`: seven
domain-grouped Typed Settings classes (`DatabaseSettings`, `AuthSettings`,
`WecomOAuthSettings`, `WecomCallbackSettings`, `MediaStorageSettings`,
`ThumbnailSettings`, `VoiceTranscodeSettings`), each a `pydantic-settings` `BaseSettings` instantiated
fresh on every call via a `get_xxx_settings()` factory — never cached — so an
env var change takes effect on the next read, matching the pre-RND-223
`os.getenv`/`os.environ.get` semantics the test suite relies on
(`monkeypatch.setenv` before a request/call). Environment variable names,
defaults, and fail-loud/degrade behavior are unchanged; only the read
mechanism moved. `backend/app/main.py` is now a composition root:
`create_app()` builds and wires the FastAPI instance (middleware, routers,
health endpoints, static mount), and the module-level `app = create_app()`
is what `uvicorn app.main:app` serves.

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
