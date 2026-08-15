# API Reference — Crowntime WeCom Archive

Route catalog for the current FastAPI application.

Source of truth:

- runtime routes: `backend/app/main.py`
- router modules under `backend/app/routers/`
- interactive OpenAPI UI at `/docs`

This document focuses on the stable HTTP surface and the auth model. For ORM
details, see [DATA_MODEL.md](DATA_MODEL.md).

---

## 1. Auth Model

### Public routes

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Readiness check (DB + schema revision) — see `docs/DEPLOYMENT.md` §7.5. Alias of `/health/ready`; use `/health/live` for a dependency-free liveness probe. |
| `GET` | `/health/live` | Liveness check (process only) |
| `GET` | `/health/ready` | Readiness check (DB + schema revision) |
| `GET` | `/admin/login` | Login page shell |
| `POST` | `/api/auth/password/login` | Password login when `AUTH_MODE=password` |
| `GET` | `/api/auth/wecom/login` | Start WeCom OAuth flow |
| `GET` | `/api/auth/wecom/callback` | Finish WeCom OAuth flow |
| `GET` | `/api/wecom/archive/events` | WeCom callback URL verification |
| `POST` | `/api/wecom/archive/events` | WeCom event signature validation |

### Session-protected routes

All routes below require a valid `session_id` cookie and derive authorization
scope exclusively from `admin_sessions.tenant_id`.

`tenant_id` is never accepted from request params or headers.

---

## 2. Auth / Session APIs

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/auth/me` | Always returns HTTP 200; payload includes `authenticated` |
| `POST` | `/api/auth/logout` | Revokes server-side session and clears cookie |

---

## 3. Annual-plan purchase and WeChat Pay

The HTML and `/api/billing/*` routes require a valid owner session. They accept
both an `admin`-scope owner for an active tenant and the restricted
`provisioning`-scope owner created by the self-service organization flow.
Tenant identity always comes from that session.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/billing` | Owner purchase/status page |
| `GET` | `/api/billing/plan` | Server-authoritative annual plan and payment availability |
| `GET` | `/api/billing/subscription` | Current tenant plan, effective dates, entitlements and explicit customer-facing state |
| `GET` | `/api/billing/capacity` | Live tenant-scoped quota, downloaded-byte usage, remaining bytes and capacity state |
| `GET` | `/api/billing/orders/latest` | Latest tenant order or `null` |
| `POST` | `/api/billing/orders` | Create/idempotently replay a Native QR order; requires `Idempotency-Key` |
| `GET` | `/api/billing/orders/{order_id}` | Tenant-scoped order and activation state |
| `GET` | `/api/billing/orders/{order_id}/qr` | Server-rendered PNG QR; never exposes the raw provider URL |
| `POST` | `/api/billing/orders/{order_id}/refresh` | Signed provider query and reconciliation |
| `POST` | `/api/billing/orders/{order_id}/close` | Close an unpaid order |

The browser supplies only the seeded plan code. Price, currency, period and
entitlements are loaded from the database; client-provided amounts are not
accepted. Order states are `creating`, `pending`, `paid_activation_pending`,
`succeeded`, `closed` and `failed`. A verified payment may temporarily remain
`paid_activation_pending`; callback retries or owner refresh safely retry the
same provider-neutral subscription activation.

`POST /api/payments/wechat/notify` is intentionally not session-protected. It
authenticates WeChat Pay itself: the handler verifies the raw-body RSA
signature, requires the configured `PUB_KEY_ID_*`, enforces timestamp
freshness, decrypts the AES-GCM resource, verifies AppID/merchant/order/amount/
currency, and then applies idempotent activation. Invalid notifications return
a generic failure without provider details. The route must be public over
HTTPS, but no other billing route should bypass normal session authentication.

WeChat Pay Native is a one-time annual purchase, not an automatic debit
agreement. The payer may choose only the funding methods offered by WeChat for
that transaction; the product does not promise a specific bank-card option.
Tencent's Conversation Archive service fee remains separate.

`GET /api/billing/capacity` measures only this tenant's successfully downloaded
`media_files` bytes and joins that fact to the effective subscription quota.
States are `normal`, `warning_80`, `warning_90`, `full`, `over_limit`, and
`unavailable`. Missing/inactive subscription or unavailable usage never grants
write capacity. `measured_at` is the server measurement time; the response is
not a browser estimate.

`GET /api/billing/subscription` has no tenant, status, quota, or entitlement
input. It derives all fields from the authenticated owner's tenant and the
RND-376 authority. Customer-facing states are `trial`, `paid_active`,
`expiring_soon` (30-day server-time window), `expired`, `canceled`, and
`unavailable`; inactive states include a machine-readable reason so the page
can explain why access is unavailable and direct the owner to purchase/renew.

---

## 4. Review Console APIs

### Monitored accounts

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/monitored-accounts` | `include_conversation_count` (default `true`) | Array of monitored-account summary objects |

Monitored accounts are derived at query time from archive participants plus
authenticated admin identities; there is no separate seat-roster table.
When `include_conversation_count=false`, the response preserves the same
member data but returns `conversation_count: null` rather than performing the
archive-wide count aggregation. This is intended for UIs that do not display
the count.

### Contacts

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/contacts` | none | Array of contact summary objects |

### External contacts

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/admin/external-contacts` | `company`, `tags`, `owner_wecom_userid`, `q`, `offset`, `limit` | Tenant-scoped external-contact page |
| `GET` | `/api/admin/external-contacts/{external_userid}` | none | One external-contact identity, follow remarks, nickname history, and conversations |

The RND-170 identity contract keeps three facts distinct: `name` is a legacy
compatibility label, `current_nickname` is the customer-level current WeCom
nickname, and `follow_remarks` contains employee-scoped remarks. List/detail
`display_name` has no selected employee context and therefore uses the current
nickname (or a safe fallback), never an arbitrary employee remark. A `q`
search can match an active employee remark, current nickname, or former
nickname; `search_matches` identifies the match type and, for a remark, the
matching `follow_userid`. Each external identity appears once per result page.

### Conversations

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/conversations` | `mode=staff&staff_id=...` or `mode=contact&contact_id=...`; optional `include_participant_metadata` | Array of conversation summaries |

Rules:

- `mode=staff` requires `staff_id`
- `mode=contact` requires `contact_id`
- conversations are returned latest-activity-first
- `include_participant_metadata=false` uses the compact staff-list path: direct
  conversation titles remain resolved, while group-card participant arrays are
  omitted because the console does not render them

### Conversation timeline

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/conversations/{conversation_id}/messages` | `limit`, `before` | Paginated message timeline |

Important behavior:

- results are tenant-scoped
- returned in ascending message-time order within the page
- `before` is an opaque cursor from the previous response's `pagination.next_before`
- conversation IDs are either:
  - group: `<roomid>`
  - direct: `direct__<uid_a>___<uid_b>`

### Media serving

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/conversations/{conversation_id}/messages/{msgid}/media` | none | Image file bytes or HTTP 404 |
| `GET` | `/api/conversations/{conversation_id}/messages/{msgid}/media/access` | none | Unified media access descriptor (JSON) — see below |

Both routes only serve already-downloaded image media and apply full tenant
authorization and safe-path checks before returning anything.

**Status (RND-187): implemented locally, developer re-acceptance pending —
not yet deployed to production.**

#### `GET .../media/access` — unified media access descriptor (RND-187)

Returns a small JSON descriptor telling the client *how* to load an image,
instead of proxying the image bytes itself. The frontend calls this first
for every image message, then loads the actual image from `url` — it never
needs to know or reason about which storage backend served it, and it never
constructs a Qiniu/CDN URL itself.

- **Method / path**: `GET /api/conversations/{conversation_id}/messages/{msgid}/media/access`
- **Authentication**: requires a valid `session_id` cookie, same as every
  other session-protected route (`get_current_user`). No cookie → `401`.
- **Tenant requirement**: `tenant_id` comes only from the session — never
  from a request param. The message must belong to `conversation_id` for
  the session's tenant, and the resolved `media_files` row must
  independently belong to that same tenant (defense-in-depth — a row whose
  `tenant_id` column diverged from its parent message's would still be
  denied). For Qiniu-backed rows there is one more check before a signed
  URL is ever minted: the object key's own `tenants/{tenant_id}/` prefix
  must agree with the authenticated tenant — see `docs/ops/media_storage_ops.md`.
- **Success response** (`200`), shape shared by both storage backends:

  ```json
  {
    "media_id": 207,
    "storage_backend": "qiniu_kodo",
    "access_type": "signed_url",
    "url": "https://media.example.com/<redacted-path>?e=<redacted>&token=<redacted>",
    "expires_at": "2026-07-12T10:15:00+00:00",
    "content_type": "image/jpeg",
    "size_bytes": 333287
  }
  ```

  (The `url` value above is illustrative only — this document never
  contains a real Signed URL. A real value is a single-object, time-boxed
  Qiniu URL, valid only until `expires_at`.)

  - **Qiniu-backed media**: `access_type="signed_url"`. `url` is a
    short-lived signed URL (official Qiniu SDK signer) scoped to exactly one
    object; the browser fetches it directly from `QINIU_DOMAIN` (a CDN or, per
    RND-207, an origin domain) — the image bytes no longer round-trip through
    this backend. `expires_at` is an ISO-8601 timestamp. **RND-207**: the
    absolute expiry is snapped to a fixed window (`MEDIA_SIGNED_URL_WINDOW_SECONDS`,
    defaulting to the TTL `MEDIA_SIGNED_URL_TTL_SECONDS`, default 900s, bounded
    60–3600s), so repeated requests for the same object within a window return
    a byte-identical `url`/`expires_at` and the browser reuses its HTTP cache.
  - **Local-backed media**: `access_type="proxy"`. `url` is the existing
    `.../media` route, unchanged; `expires_at` is `null` (the URL carries
    no time-boxed credential of its own — the session cookie authorizes
    each request to it, exactly as before RND-187).
- **`variant` query param (RND-207/RND-258)**: `?variant=thumb` returns a
  descriptor for the generated **list thumbnail** when one exists, else falls
  back to the original. `?variant=play` returns a generated MP3/WAV playback
  derivative for a `voice`/`audio_archive` row when one exists; ordinary
  voice descriptor requests select that same derivative automatically. A
  failed or unsupported conversion falls back to the original AMR/SILK so it
  remains downloadable. `size_bytes` is `null` for either derived object.
  The same object-key tenant-prefix check applies to every derivative. The
  timeline (`GET .../messages`) additionally carries
  `thumbnail_access_url` (the `?variant=thumb` URL, `null` when no thumbnail
  exists) and `image_width`/`image_height` (the original's intrinsic pixels,
  for layout-box reservation). Nested mixed/chatrecord media descriptors carry
  the same thumbnail fields.
- **Cache-Control**: `no-store` on **every** response this endpoint can
  produce — success or error, any status code (`200`/`401`/`404`/`500`/`502`/`503`).
  This response is per-user and short-lived and must never be cached by a
  shared/CDN proxy.
- **Error responses**: `401` (not authenticated), `404` (wrong tenant,
  wrong conversation, non-image message, missing/pending media row,
  unservable file, or a Qiniu object key whose tenant prefix doesn't match
  — all collapsed to the same generic `404` so a client cannot distinguish
  "doesn't exist" from "not yours"), `500` (misconfigured storage backend or
  TTL), `502` (confirmed signed-URL generation failure against a provider
  that did respond), `503` (storage provider outage — never reported as a
  plain `404`, an outage must not look like missing media).
- **Never exposed**: `storage_ref` (the raw Qiniu object key or local
  filesystem path), `local_path`, `sdkfileid`, Qiniu `AK`/`SK`, or any raw
  provider/SDK error text — in the response body or in any log line.

---

## 5. Data Export APIs

All export routes are tenant-scoped and require the `owner` role. The browser
never supplies `tenant_id`. Every accepted export consumes a server-side quota
and writes the existing export audit record; password confirmation is obtained
through `POST /api/admin/export/approve` and is bound to the canonical request
parameters.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/exports` | Shared export center used by review, search and customer-detail pages |
| `GET` | `/api/admin/exports/quota` | Current natural-month counters and Asia/Shanghai reset time |
| `POST` | `/api/admin/exports/text` | Generate a bounded PDF or Excel file synchronously |
| `POST` | `/api/admin/exports/media` | Accept one asynchronous tenant-wide original-media ZIP job |
| `GET` | `/api/admin/exports/jobs` | Latest 20 media export jobs for the authenticated tenant |
| `GET` | `/api/admin/exports/jobs/{job_id}` | One tenant-owned job state |
| `GET` | `/api/admin/exports/jobs/{job_id}/download` | Authenticated download or short-lived private-storage redirect |

Text scope supports one or more of `roomid` (group room or derived
`direct__...` conversation ID), `participant_id`, public WeCom `message_ids`,
and a paired `start_ms`/`end_ms`. The synchronous limit is 50,000 rows. The
monthly limits are 10 text exports and 1 full-media ZIP per tenant. The quota
check and consumption run in the same transaction as text generation or media
job acceptance; validation failures roll back, while an accepted asynchronous
job keeps its consumed allowance even if later generation fails.

Media jobs move through `queued`, `processing`, `ready`, `failed`, and
`expired`. A ready ZIP contains tenant-owned Qiniu originals plus
`manifest.csv`; local, missing, cross-tenant, size-mismatched, or
checksum-mismatched sources fail the whole job. The worker verifies source
size and SHA-256 through Qiniu's server-side `qhash/sha256` response, then
Qiniu Dora builds the ZIP asynchronously in storage. The API redirects an
authenticated Owner to a short-lived private Qiniu URL. The requesting Owner
receives an email linking to `/admin/exports?job=...`, which is not a bearer
credential and still requires an Owner session. The download route fails
closed exactly seven days after successful generation; the maintenance worker
then deletes the private object idempotently.

---

## 6. Search / Message APIs

These routes expose raw message-centric access in addition to the main
conversation-oriented review console.

### HTML message search pages

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/messages` | Search/filter archive messages in HTML |
| `GET` | `/admin/messages/{msgid}` | Message detail page in HTML |

### JSON message search APIs

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/api/search/contacts` | `q`, `limit` | Contact/global identity search results |
| `GET` | `/api/messages` | `sender`, `q`, `msgtype`, `roomid`, `limit` | Array of messages |
| `GET` | `/api/messages/{msgid}` | none | One message plus recipient list |

`/api/search/contacts` includes matching external identities once. Its
`match_field` is `remark`, `current_nickname`, or `historical_nickname` for an
external result; `match_context_userid` is populated only for a remark match.


Search behavior:

- `q` is a case-insensitive substring match on `content_text`
- `sender`, `msgtype`, and `roomid` are exact-match filters
- all queries are tenant-scoped

---

## 7. Diagnostics APIs

| Method | Path | Query | Response |
|--------|------|-------|----------|
| `GET` | `/admin/diagnostics/reachability` | none | HTML diagnostics shell |
| `GET` | `/api/admin/reachability-audit` | `conversation_id`, `message_type`, `msgtime_from`, `msgtime_to`, `limit`, `offset`, `include_samples`, `sample_limit` | Aggregate reachability report |

The HTML page is a thin UI over the JSON diagnostics endpoint. All
classification happens server-side in `app/reachability_audit.py`.

---

## 8. WeCom Callback APIs

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/wecom/archive/events` | Decrypts and verifies WeCom `echostr` |
| `POST` | `/api/wecom/archive/events` | Verifies signature, decrypts the event envelope, validates CorpID, persists a targeted external-contact refresh task for `change_external_contact`, and dispatches archive work asynchronously |

The POST acknowledgement never waits for targeted refresh, archive work, or
any outbound contact API request. Direct inbound archive messages from a
WeCom external-user identifier persist the same coalesced task after decrypt
commit; group messages and messages sent by an archive seat do not. A small
dedicated worker drains persisted tasks, while the independent daily full
reconciliation is the durable fallback for missed callbacks, unavailable
customer relationships, and worker restarts.

These routes are intentionally **not** session-protected.

---

## 8. Verification Notes

This document was aligned against the current FastAPI route table and OpenAPI
schema generated by the app itself. If you change routes, update this file in
the same change.
