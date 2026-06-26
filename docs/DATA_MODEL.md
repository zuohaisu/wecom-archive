# Data Model — 365 WeCom Archive

PostgreSQL schema for storing WeCom conversation archive messages.
Related issue: RND-75.

---

## Overview

Six tables cover the full lifecycle from encrypted pull to searchable archive:

| Table | Purpose |
|---|---|
| `key_versions` | Registry mapping WeCom `publickey_ver` to a private key path or alias |
| `sync_states` | Cursor tracking — last successfully synced `seq` per corp |
| `archive_messages` | Core message store — encrypted envelope + decrypted payload |
| `archive_message_recipients` | Per-receiver lookup rows derived from `tolist` |
| `media_files` | Download state for media attachments |
| `contacts` | Lightweight WeCom user identity cache |

---

## Tables

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

One row per corp. Tracks the highest `seq` value that was successfully pulled and stored. The sync worker reads this on startup to resume without duplicating messages.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `corp_id` | varchar(64) | unique; WeCom corp ID |
| `last_seq` | bigint | last successfully processed sequence number |
| `updated_at` | timestamptz | auto-updated on write |

---

### `archive_messages`

Primary message store. Each row is one WeCom conversation archive message. Columns are grouped into three logical sections: the encrypted envelope, the decryption state, and the extracted searchable fields.

#### Encrypted envelope

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | auto-increment |
| `msgid` | varchar(64) | unique; WeCom stable message ID |
| `seq` | bigint | WeCom pull sequence number; indexed for cursor-based sync |
| `publickey_ver` | integer | identifies which RSA key was used to encrypt this message |
| `raw_encrypted_payload` | jsonb | the full encrypted SDK record as received (see note below) |
| `encrypt_random_key` | text | RSA-encrypted AES session key (base64), split from the envelope |
| `encrypt_chat_msg` | text | AES-encrypted message body (base64), split from the envelope |

#### Decryption state

| Column | Type | Notes |
|---|---|---|
| `decrypt_status` | varchar(16) | `pending` / `success` / `failed` (see note below) |
| `decrypted_payload` | jsonb | full decrypted message JSON; null when `decrypt_status` is not `success` |

#### Extracted fields

| Column | Type | Notes |
|---|---|---|
| `content_text` | text | plain-text body extracted from `decrypted_payload`; indexed for FTS |
| `msgtype` | varchar(32) | message type: `text`, `image`, `voice`, `video`, `file`, `mixed`, … |
| `sender` | varchar(64) | WeCom user ID of the message sender (`from` field) |
| `roomid` | varchar(64) | group chat room ID; null for 1:1 messages |
| `msgtime` | bigint | WeCom message timestamp in milliseconds since epoch |
| `tolist` | jsonb | array of recipient WeCom user IDs (canonical source) |
| `sdkfileid` | text | WeCom SDK file ID for media messages; null for text |
| `created_at` | timestamptz | row insert time |

Indexes:
- Unique on `msgid`
- B-tree on `seq` — sync cursor pagination
- B-tree on `msgtype` — filter by type
- B-tree on `sender` — filter by sender
- B-tree on `roomid` — filter by room
- B-tree on `msgtime` — time-range queries and sort
- Composite B-tree on `(msgtime, msgtype)` — combined admin queries
- GIN on `decrypted_payload` — JSONB containment and key-path queries
- GIN on `to_tsvector('simple', coalesce(content_text, ''))` — full-text keyword search

---

### `archive_message_recipients`

Per-receiver lookup rows derived from `archive_messages.tolist`. One row is inserted per recipient when the parent `archive_messages` row is written.

This table exists because a JSONB `@>` containment query on `tolist` requires a full GIN scan of the entire table, while a B-tree index on `receiver_userid` in this table supports efficient equality lookup. For a small archive this matters less; for millions of messages it becomes critical.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `message_id` | bigint FK → `archive_messages.id` | the parent message |
| `receiver_userid` | varchar(64) | one recipient WeCom user ID |
| `receiver_type` | varchar(32) | optional; e.g. `user` or `chatroom` if determinable |
| `created_at` | timestamptz | auto-set on insert |

Indexes:
- B-tree on `message_id` — join back to the parent message
- B-tree on `receiver_userid` — the primary lookup: "find messages received by user X"

---

### `media_files`

Tracks the download and storage state for each media attachment. One row per `sdkfileid`. Media download is handled by a separate worker (not in Phase 1 scope).

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `sdkfileid` | text | unique; WeCom SDK file ID |
| `archive_message_id` | bigint FK → `archive_messages.id` | the message this media belongs to |
| `file_type` | varchar(32) | `image`, `voice`, `video`, `file`, `emotion`, … |
| `local_path` | text | absolute path on disk (Phase 1 local storage) |
| `oss_key` | text | OSS object key (Phase 2) |
| `file_size` | bigint | bytes; null until downloaded |
| `download_status` | varchar(16) | `pending` \| `downloaded` \| `failed` |
| `created_at` | timestamptz | row insert time |
| `updated_at` | timestamptz | last status change |

Indexes:
- Unique on `sdkfileid`
- B-tree on `archive_message_id`

---

### `contacts`

Lightweight cache of WeCom user identities encountered in the archive. Populated opportunistically during sync; not authoritative. Enables human-readable display in the admin UI without re-querying WeCom on every request.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | auto-increment |
| `wecom_userid` | varchar(64) | unique; WeCom user ID |
| `name` | text | display name; null if not yet resolved |
| `created_at` | timestamptz | row insert time |
| `updated_at` | timestamptz | last update |

Indexes: unique on `wecom_userid`.

---

## Migration

Managed with Alembic. Config: `backend/alembic.ini`. Run from `backend/`.

```bash
# Generate SQL for review (offline, no DB required)
cd backend
alembic upgrade head --sql

# Apply to a live database
alembic upgrade head

# Roll back
alembic downgrade base
```

The initial migration is `backend/alembic/versions/0001_initial_schema.py`.

---

## Design Notes

### Receiver lookup: why `archive_message_recipients` alongside `tolist`

`tolist` (JSONB array) is the canonical source of recipients and is retained for completeness. However, querying `tolist @> '["userX"]'` requires a GIN containment scan of every row in `archive_messages`. For large archives this is slow.

`archive_message_recipients` provides a dedicated B-tree index on `receiver_userid`. The sync layer inserts one row per recipient when inserting a message, keeping it in sync. The result is that "find all messages received by user X" is a fast indexed lookup rather than a full-table scan.

### Full-text search: `content_text` and the tsvector GIN index

`decrypted_payload` (JSONB) holds the complete decoded message object, and a GIN index on it supports key-path and containment queries. It does not support keyword full-text search efficiently.

`content_text` is a plain-text column populated by the sync layer with the human-readable body of the message (e.g. `text.content` for text messages, filenames for file messages). A GIN index using `to_tsvector('simple', coalesce(content_text, ''))` enables PostgreSQL `@@` full-text search queries.

The `simple` dictionary is used deliberately — it applies no language-specific stemming, which is appropriate for mixed Chinese/English WeCom content. A `pg_tsvector`-aware Chinese text search configuration can be substituted later without a schema change (only the index rebuild and query change).

### Raw encrypted payload: `raw_encrypted_payload` vs split fields

The WeCom SDK returns an encrypted envelope JSON object containing both `encrypt_random_key` and `encrypt_chat_msg` (and other metadata). Three fields are stored:

- `raw_encrypted_payload` (JSONB) — the complete envelope as received, for audit and replay
- `encrypt_random_key` (TEXT) — split out for direct access by the decryption routine
- `encrypt_chat_msg` (TEXT) — split out for direct access by the decryption routine

Splitting avoids a JSONB key-access on every decryption call. Retaining the full envelope ensures nothing is silently lost if the SDK adds envelope fields in a future version.

### Nullable `decrypted_payload` and `decrypt_status`

`decrypted_payload` is nullable. A row may be in one of three decryption states, tracked by `decrypt_status`:

| Value | Meaning | `decrypted_payload` |
|---|---|---|
| `pending` | Inserted but decryption not yet attempted | null |
| `success` | Decryption succeeded | populated |
| `failed` | Decryption failed (wrong key, corrupt data) | null |

Rows with `decrypt_status = 'failed'` are kept rather than discarded so they can be retried after a key correction. The encrypted fields (`encrypt_random_key`, `encrypt_chat_msg`, `raw_encrypted_payload`) remain available for retry. Application code must not treat a null `decrypted_payload` as an error without also checking `decrypt_status`.

### Key rotation support

`publickey_ver` is stored on every message row. `key_versions` maps each version to a key path. To handle a new key: insert a row into `key_versions`, set `is_active = true` for the new row, and update `WECOM_PUBLIC_KEY_VERSION` in the environment. Older messages retain their original `publickey_ver` and can be re-decrypted using the key registered for that version.

### `updated_at` auto-update behaviour

`updated_at` columns are set at insert time via `server_default=NOW()`. At the ORM layer, `onupdate=func.now()` causes SQLAlchemy to set the column on ORM-driven updates. Raw SQL `UPDATE` statements bypass this mechanism — application code should include an explicit `updated_at = NOW()` clause when writing raw SQL.

---

_Last updated: 2026-06-26 — RND-75_
