# Data Model — Crowntime WeCom Archive

PostgreSQL schema for storing WeCom conversation archive messages and tenant
management infrastructure for future SaaS use.

Related issues: RND-75 (initial schema), RND-111 (tenant foundation), RND-156
(multi-tenant), RND-184 (corp ID uniqueness), RND-376 (billing authority).

---

## Overview

Core archive, tenant, and identity tables cover the lifecycle from encrypted
pull to searchable archive, plus the tenant-aware foundation for employee
login (RND-110, shipped) and future multi-tenant SaaS operation:

| Table | Purpose |
|---|---|
| `tenants` | Top-level tenant entity; one default row for MVP |
| `tenant_wecom_configs` | Per-tenant WeCom app credentials |
| `admin_users` | WeCom employees who have authenticated |
| `admin_sessions` | Active login sessions |
| `billing_plans` | Server-authoritative price, period and storage quota |
| `plan_entitlements` | Normalized boolean capabilities attached to a plan |
| `subscriptions` | One authoritative current subscription per tenant |
| `subscription_history` | Append-only snapshots of subscription assignments |
| `key_versions` | Registry mapping WeCom `publickey_ver` to a private key path or alias |
| `sync_states` | Cursor tracking — last successfully synced `seq` per tenant+corp |
| `archive_messages` | Core message store — encrypted envelope + decrypted payload |
| `archive_message_recipients` | Per-receiver lookup rows derived from `tolist` |
| `media_files` | Download state for media attachments (tenant-scoped via `UNIQUE(tenant_id, sdkfileid)`) |
| `contacts` | Lightweight WeCom user identity cache |
| `external_contacts` | Tenant-scoped external-contact compatibility/profile record |
| `external_contact_follows` | Employee-scoped external-contact remarks and follow state |
| `external_contact_nickname_history` | Customer nickname transition audit timeline |
| `external_contact_refresh_tasks` | Durable, coalesced external-contact metadata refresh work |

---

## Tenant Foundation Tables

### `tenants`

Top-level tenant entity. MVP: one default row with
`id = 00000000-0000-0000-0000-000000000001` and `slug = 'default'`.

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | UUID string |
| `name` | varchar(255) | Display name (e.g. "Acme Corp") |
| `slug` | varchar(128) | URL-safe identifier; unique |
| `is_active` | boolean | Soft-disable a tenant |
| `created_at` | timestamptz | auto-set on insert |
| `updated_at` | timestamptz | auto-updated on write |

Indexes: unique on `slug`.

---

### `tenant_wecom_configs`

One row per tenant. Stores WeCom app credentials used for archive sync and
OAuth login. A given `corp_id` can be active on at most one tenant
(RND-184, enforced by partial unique index `uq_tenant_wecom_configs_active_corp_id`).

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | UUID string |
| `tenant_id` | varchar(36) FK → `tenants.id` | unique (1:1 with tenant for MVP) |
| `corp_id` | varchar(64) | WeCom CorpID |
| `agent_id` | varchar(64) | WeCom Agent ID (self-built app) |
| `app_secret` | text | Phase 1: plaintext (internal only). Phase 3: encrypt at rest. **Do not log.** |
| `callback_domain` | varchar(255) | OAuth trusted domain registered in WeCom Admin |
| `is_active` | boolean | Active configs must have unique `corp_id` (partial unique index) |
| `created_at` | timestamptz | auto-set on insert |
| `updated_at` | timestamptz | auto-updated on write |

Populated by `scripts/bootstrap_default_tenant.py` from `WECOM_CORP_ID`,
`WECOM_AGENT_ID`, `WECOM_OAUTH_SECRET`, `ADMIN_DOMAIN` env vars.

---

### `admin_users`

WeCom employees who have completed OAuth login. Created or updated on each
successful authentication. Not pre-populated — records are created at login time.

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | UUID string |
| `tenant_id` | varchar(36) FK → `tenants.id` | |
| `wecom_user_id` | varchar(64) | WeCom UserId returned by getuserinfo API |
| `name` | text | Display name from WeCom (nullable until resolved) |
| `avatar_url` | text | nullable |
| `last_login_at` | timestamptz | nullable; updated on each login |
| `created_at` | timestamptz | auto-set on insert |
| `updated_at` | timestamptz | auto-updated on write |

Indexes:
- Unique on `(tenant_id, wecom_user_id)`.

---

### `admin_sessions`

Active login sessions. The `id` is the session UUID stored as a cookie value.
The `get_current_user()` FastAPI dependency validates rows in this table on
every protected request.

| Column | Type | Notes |
|---|---|---|
| `id` | varchar(36) PK | Session UUID; value stored in `session_id` cookie |
| `admin_user_id` | varchar(36) FK → `admin_users.id` | |
| `tenant_id` | varchar(36) FK → `tenants.id` | authorization scope — all admin queries filter by this |
| `wecom_user_id` | varchar(64) | duplicated for audit trail / fast logging |
| `created_at` | timestamptz | auto-set on insert |
| `expires_at` | timestamptz | hard TTL; session is invalid after this |
| `is_revoked` | boolean | set on logout |

Indexes: B-tree on `expires_at` (used by session validation and cleanup queries).

Cleanup: `DELETE FROM admin_sessions WHERE expires_at < NOW() - INTERVAL '1 day'`
(scheduled cleanup, **not yet automated** — technical debt).

---

## Billing Authority Tables

RND-376 establishes the only source of commercial access truth. Existing
tenants are not silently enrolled by the migration: a trusted domain service
must assign a subscription before any entitlement is granted.

### `billing_plans`

The first seeded row is immutable by code: `annual_base_cny_99`, CNY 99.00,
12 calendar months, 5 GiB storage and unlimited seats. Price and quota are
stored as integer cents/bytes; no client value participates in access checks.

### `plan_entitlements`

One normalized row per enabled boolean capability. The annual base plan starts
with `archive_access` and `unlimited_seats`. Storage quota is a numeric plan
field and is read through the same entitlement service.

### `subscriptions`

There is exactly one row per tenant (`UNIQUE(tenant_id)`). It is the mutable
current projection, with status, effective range, source, renewal count and
revision. Only `trial` and `active` inside the half-open interval
`[starts_at, ends_at)` grant capabilities. `past_due`, `expired`, `canceled`,
future-start and missing subscriptions fail closed.

An inactive plan cannot be newly assigned. It does not retroactively remove an
already purchased term; reads continue to honor that term until its own end.

### `subscription_history`

Every authoritative assignment appends a full snapshot keyed by subscription
and monotonically increasing revision. Application code has no update/delete
path for these rows. RND-384 builds payment idempotency and renewal transactions
on this primitive; RND-385 consumes `get_storage_quota()` for the actual storage
write gate.

---

## Archive Tables

All archive tables have a `tenant_id` column (migration 0002). Existing rows
are backfilled with the default tenant ID by `bootstrap_default_tenant.py`.

### `key_versions`

Stores the mapping from WeCom's `publickey_ver` integer to the private key used for RSA decryption. Required to support key rotation without losing the ability to re-decrypt older messages.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `publickey_ver` | integer | unique; from WeCom encrypted message envelope |
| `key_alias` | varchar(128) | human-readable key identifier (e.g. `wecom-key-v1`) |
| `private_key_path` | text | path to the PEM file on disk, or a key management alias |
| `is_active` | boolean | marks the current active key for new messages |
| `created_at` | timestamptz | auto-set on insert |

Indexes: unique on `publickey_ver`.

---

### `sync_states`

One row per (tenant, corp). Tracks the highest `seq` value that was successfully pulled and stored. The sync worker reads this on startup to resume without duplicating messages.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `corp_id` | varchar(64) | WeCom corp ID; unique within tenant (`UNIQUE(tenant_id, corp_id)`) |
| `last_seq` | bigint | last successfully processed sequence number |
| `tenant_id` | varchar(36) FK → `tenants.id` | NOT NULL after bootstrap |
| `updated_at` | timestamptz | auto-updated on write |

---

### `archive_messages`

Primary message store. Each row is one WeCom conversation archive message. Columns are grouped into three logical sections: the encrypted envelope, the decryption state, and the extracted searchable fields.

#### Encrypted envelope

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | auto-increment |
| `msgid` | varchar(64) | WeCom stable message ID; unique within tenant (`UNIQUE(tenant_id, msgid)`) |
| `seq` | bigint | WeCom pull sequence number; indexed for cursor-based sync |
| `publickey_ver` | integer | identifies which RSA key was used to encrypt this message |
| `raw_encrypted_payload` | jsonb | the full encrypted SDK record as received (see note below) |
| `encrypt_random_key` | text | RSA-encrypted AES session key (base64), split from the envelope |
| `encrypt_chat_msg` | text | AES-encrypted message body (base64), split from the envelope |

#### Decryption state

| Column | Type | Notes |
|---|---|---|
| `decrypt_status` | varchar(16) | `pending` / `success` / `failed` (see note below) |
| `decrypted_payload` | jsonb | reserved for the full decrypted message JSON; in practice never populated by the decrypt worker — a deliberate data-minimization constraint (see "Nullable decrypted_payload" note below). Always null on rows written by the real pipeline. |

#### Extracted fields

| Column | Type | Notes |
|---|---|---|
| `content_text` | text | plain-text body extracted from `decrypted_payload`; indexed for FTS |
| `msgtype` | varchar(32) | message type as received from WeCom. The canonical list (27 registered types as of #25), per-type support tier (`SUPPORTED`/`PARTIAL`/`UNSUPPORTED`), category, and alias resolution (e.g. `weapp`↔`miniprogram`, `audio_archive`↔`meeting_voice_call`) live in `app/message_type_registry.py` — treat it, not this table, as the source of truth. Examples: `text`, `image`, `video`, `voice`, `file`, `location`, `link`, `card`, `markdown`, `news`, `sphfeed`, `docmsg`, `revoke`, `mixed`, `chatrecord`, `sys`, `vote`, `collect`, `meeting`, `schedule`, `redpacket`, `switch_corp`, … |
| `sender` | varchar(64) | WeCom user ID of the message sender (`from` field) |
| `roomid` | varchar(64) | group chat room ID; null for 1:1 messages |
| `msgtime` | bigint | WeCom message timestamp in milliseconds since epoch |
| `tolist` | jsonb | array of recipient WeCom user IDs (canonical source) |
| `sdkfileid` | text | WeCom SDK file ID for media messages; null for text |
| `tenant_id` | varchar(36) FK → `tenants.id` | NOT NULL after bootstrap |
| `created_at` | timestamptz | row insert time |
| `structured_content` | jsonb | type-specific sub-payload for in-scope msgtypes (RND-197); set by the same decrypt run, null otherwise |
| `is_revoked` | boolean | set only as a side effect of revoke reconciliation (`app.revoke_reconciliation`), not by the decrypt step itself |
| `revoked_at` | timestamptz | nullable; set alongside `is_revoked` |

Indexes:
- Unique on `(tenant_id, msgid)` — tenant-scoped deduplication
- B-tree on `seq` — sync cursor pagination
- B-tree on `msgtype`, `sender`, `roomid`, `msgtime`, `tenant_id`
- Composite B-tree on `(msgtime, msgtype)`
- GIN on `decrypted_payload` — JSONB containment (see "Nullable decrypted_payload" note: not currently populated, so this index is presently inert on real data)
- GIN on `structured_content` — JSONB containment (RND-197)
- GIN on `to_tsvector('simple', coalesce(content_text, ''))` — full-text search

---

### `archive_message_recipients`

Per-receiver lookup rows derived from `archive_messages.tolist`. One row is inserted per recipient when the parent `archive_messages` row is written.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `message_id` | bigint FK → `archive_messages.id` | the parent message |
| `receiver_userid` | varchar(64) | one recipient WeCom user ID |
| `receiver_type` | varchar(32) | optional; e.g. `user` or `chatroom` if determinable |
| `tenant_id` | varchar(36) FK → `tenants.id` | NOT NULL after bootstrap; matches parent row |
| `created_at` | timestamptz | auto-set on insert |

Indexes:
- B-tree on `message_id`, `receiver_userid`, `tenant_id`

---

### `media_files`

Tracks the download and storage state for each media attachment. One row per `(tenant_id, sdkfileid)` pair. Media download is handled by a dedicated worker (RND-151/168).

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `sdkfileid` | text | WeCom SDK file ID; unique within tenant (`UNIQUE(tenant_id, sdkfileid)`) |
| `archive_message_id` | bigint FK → `archive_messages.id` | the message this media belongs to |
| `tenant_id` | varchar(36) FK → `tenants.id` | NOT NULL after bootstrap |
| `file_type` | varchar(32) | `image`, `voice`, `video`, `file`, `emotion`, … |
| `local_path` | text | storage reference; resolved through `MediaStorageProvider` |
| `oss_key` | text | OSS object key (Phase 2, reserved) |
| `file_size` | bigint | bytes; null until downloaded |
| `download_status` | varchar(16) | `pending` \| `downloaded` \| `failed` |
| `created_at` | timestamptz | row insert time |
| `updated_at` | timestamptz | last status change |

Indexes:
- Unique on `(tenant_id, sdkfileid)`
- B-tree on `archive_message_id`

---

### `contacts`

Lightweight cache of WeCom user identities encountered in the archive. Populated opportunistically during sync; not authoritative. Enables human-readable display in the admin UI without re-querying WeCom on every request.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `wecom_userid` | varchar(64) | WeCom user ID; unique within tenant (`UNIQUE(tenant_id, wecom_userid)` as of migration 0002) |
| `name` | text | display name; null if not yet resolved |
| `tenant_id` | varchar(36) FK → `tenants.id` | NOT NULL after bootstrap |
| `created_at` | timestamptz | row insert time |
| `updated_at` | timestamptz | last update |

Indexes: unique on `(tenant_id, wecom_userid)` — tenant-scoped deduplication.

---

### External-contact identity (RND-170)

`external_contacts` is one customer-level record per
`(tenant_id, external_userid)`. Its pre-existing `name` column is a legacy
compatibility display label and can contain an employee's old local remark;
it is **not** treated as the customer's real nickname. The current customer
nickname instead lives in `current_nickname_raw`,
`current_nickname_normalized`, `current_nickname_display`, and
`current_nickname_observed_at`.

`external_contact_follows` has one unique row per
`(tenant_id, external_userid, follow_userid)`. It stores that employee's raw
and normalized `remark`, `is_active`, and observation time. Its composite
foreign key points to the tenant-scoped external-contact record, preventing a
remark from crossing tenants or customers.

`external_contact_nickname_history` records each **normalized state
transition** after the first authoritative nickname observation. It retains
old/new raw, normalized, and safe-display values plus `observed_at`; a
valid-to-blank transition is a real history event, while repeated equivalent
normalized values are not duplicated. Migration `0035` intentionally does not
backfill the new nickname fields from legacy `external_contacts.name`, because
that value's historical meaning is ambiguous.

Indexes include tenant/contact lookup indexes and `pg_trgm` GIN indexes for
current nickname, active remark, and historical nickname search.

`external_contact_refresh_tasks` is separate from the contact profile tables:
it stores one coalesced, tenant-scoped task per external identifier, including
safe source, attempt count, next retry time, and fixed failure classification.
That lets an inbound direct archive message request a later API lookup even
when no readable external-contact relationship exists yet. It never stores a
nickname, remark, callback payload, or API response.

---

## Derived API Objects (not persisted)

The following are computed at query time from the archive tables and are **not**
stored as persistent tables. They become tenant-scoped automatically once the
source queries include `WHERE archive_messages.tenant_id = <tenant_id>`.

| Object | Derived from |
|---|---|
| `monitored_accounts` (archive seats) | RND-132: union of (a) `sender`/`receiver_userid` LIKE `'staff_%'` (legacy mock/dev convention) and (b) any `admin_users.wecom_user_id` for the tenant that also appears as a sender/recipient in the archive — no formal seat-roster table exists yet. See `_collect_staff_ids()` in `app/routers/conversations.py`. |
| `conversations` | Aggregated from `archive_messages` + `archive_message_recipients` |

---

## Migration

Managed with Alembic. Config: `backend/alembic.ini`. Run from `backend/`.

```bash
# Apply all migrations
cd backend
alembic upgrade head

# Generate SQL for review (offline, no DB required)
alembic upgrade head --sql

# Roll back
alembic downgrade base
```

Migrations:
- `backend/alembic/versions/0001_initial_schema.py` — initial archive schema
- `backend/alembic/versions/0002_tenant_foundation.py` — tenant tables + tenant_id columns
- `backend/alembic/versions/0003_media_tenant_scoping.py` — media_files tenant scoping
- `backend/alembic/versions/0004_tenant_wecom_config_corp_id_uniqueness.py` — corp_id uniqueness (RND-184)

After applying migration 0002, run the bootstrap script to create the default
tenant and backfill existing rows:

```bash
cd backend
python scripts/bootstrap_default_tenant.py
```

Required env vars for bootstrap: `DATABASE_URL`, `WECOM_CORP_ID`, `WECOM_AGENT_ID`,
`WECOM_OAUTH_SECRET`. Optional: `ADMIN_DOMAIN`.

---

## Design Notes

### Tenant Scoping

All admin queries are scoped by `WHERE tenant_id = <session_tenant_id>`, enforced
by `get_current_user()` FastAPI dependency (RND-110, shipped). The `tenant_id`
in the session is the sole authorization scope; it is never accepted as a
user-supplied API parameter.

### Receiver lookup: why `archive_message_recipients` alongside `tolist`

`tolist` (JSONB array) is the canonical source of recipients and is retained for completeness. However, querying `tolist @> '["userX"]'` requires a GIN containment scan of every row in `archive_messages`. For large archives this is slow.

`archive_message_recipients` provides a dedicated B-tree index on `receiver_userid`. The sync layer inserts one row per recipient when inserting a message, keeping it in sync. The result is that "find all messages received by user X" is a fast indexed lookup rather than a full-table scan.

### Full-text search: `content_text` and the tsvector GIN index

`decrypted_payload` (JSONB) is reserved to hold the complete decoded message object, with a GIN index supporting key-path and containment queries — though see the "Nullable decrypted_payload" note below: the column is not currently populated by the decrypt worker, so this index is presently inert on real data. It would not support keyword full-text search efficiently even if populated.

`content_text` is a plain-text column populated by the sync layer with the human-readable body of the message (e.g. `text.content` for text messages, filenames for file messages). A GIN index using `to_tsvector('simple', coalesce(content_text, ''))` enables PostgreSQL `@@` full-text search queries.

The `simple` dictionary is used deliberately — it applies no language-specific stemming, which is appropriate for mixed Chinese/English WeCom content.

### Raw encrypted payload: `raw_encrypted_payload` vs split fields

The WeCom SDK returns an encrypted envelope JSON object containing both `encrypt_random_key` and `encrypt_chat_msg` (and other metadata). Three fields are stored:

- `raw_encrypted_payload` (JSONB) — the complete envelope as received, for audit and replay
- `encrypt_random_key` (TEXT) — split out for direct access by the decryption routine
- `encrypt_chat_msg` (TEXT) — split out for direct access by the decryption routine

### Nullable `decrypted_payload` and `decrypt_status`

`decrypted_payload` is nullable. A row may be in one of three decryption states, tracked by `decrypt_status`:

| Value | Meaning | `decrypted_payload` |
|---|---|---|
| `pending` | Inserted but decryption not yet attempted | null |
| `success` | Decryption succeeded | null — see note below |
| `failed` | Decryption failed (wrong key, corrupt data) | null |

`decrypted_payload` is deliberately never written by `run_decrypt_once()` (`backend/app/services/decrypt_worker.py`), even on `success` — a data-minimization decision (see the `# SF-1` comment at the point it's skipped) enforced by a regression test (`backend/tests/test_decrypt_structured_content.py::test_decrypted_payload_column_is_still_never_assigned_in_the_decrypt_script`). The column and its GIN index remain in the schema but are not populated by the production pipeline; the normalized fields below (`content_text`, `msgtype`, `sender`, `roomid`, `msgtime`, `tolist`, `sdkfileid`, `structured_content`) are populated instead. Only `backend/scripts/mock_ingest.py`, a test-fixture generator, ever sets this column directly.

### Key rotation support

`publickey_ver` is stored on every message row. `key_versions` maps each version to a key path. To handle a new key: insert a row into `key_versions`, set `is_active = true` for the new row, and update `WECOM_PUBLIC_KEY_VERSION` in the environment.

### `app_secret` plaintext storage (Phase 1)

`tenant_wecom_configs.app_secret` stores `WECOM_OAUTH_SECRET` in plaintext in Phase 1. This is acceptable for an internal single-tenant deployment. Phase 3 must encrypt at rest using Fernet (symmetric) or a Vault/KMS integration before storing, and must not log the value.

---

_Last updated: 2026-07-10 — Documentation refresh_
