# RND-186 Local → Qiniu Media Migration

## Scope

RND-186 adds `scripts/migrate_local_media_to_qiniu.py`, a manual, repeatable
tool that copies existing `media_files` rows currently served by
`LocalStorageProvider` (`storage_backend="local"`) to Qiniu Kodo, flipping
each row's `storage_backend`/`storage_ref` (migration 0005's columns) to
`"qiniu_kodo"` once the upload is confirmed. It does not execute a one-time
production migration itself, does not delete local files, does not change
the download worker (RND-147/RND-174), the media route, the RND-187
signed-URL route, CDN behavior, or multi-tenant architecture. It adds two
additive migrations: 0006 (retry bookkeeping) and 0007 (full storage
metadata: bucket/mime_type/checksum_sha256).

**Media scope revision (this document supersedes the first RND-186
revision):** migration candidates are **not** image-only. Any `media_files`
row with a recognized, content-verified media type — image, video, voice,
or file — is a candidate, driven by `MediaFile.file_type` (an existing,
generic column) and verified against the file's actual byte content via
`app.media_storage.detect_media_signature_from_bytes()`. See "Supported
Media Types" and "Remaining Limitations" below for what this does and does
not mean in practice today.

## Why a Separate Tool, Not a Route or a Worker

The download worker (`download_wecom_image_media_once.py`) writes *new*
media through whichever provider `MEDIA_STORAGE_PROVIDER` currently selects.
Migration is a different operation entirely — it reads *already-downloaded*
bytes from one provider and writes them to a different, specific provider
(always Qiniu, regardless of the deployment's current default write
provider) — so this ships as its own script rather than a flag on the
worker. This mirrors the codebase's existing convention of one self-
contained one-shot script per distinct operation (`sync_wecom_archive_once.py`,
`download_wecom_image_media_once.py`, `bootstrap_default_tenant.py`, …).

## Supported Media Types

`app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES = {"image", "video",
"voice", "file"}` — deliberately the exact same set
`app.media_classification` already recognizes as having downloadable bytes
(`text` and the generic `unsupported` bucket are excluded: they never
produce a `media_files` row with bytes to migrate). A row with `file_type`
outside this set (`NULL`, or some future/unrecognized value) is **excluded
from candidate selection outright** — not merely skipped during migration —
because "the system can identify the media type" is an admission criterion
per the ticket, not a per-row failure reason.

Object-key category mapping (`app.media_storage.MEDIA_TYPE_KEY_CATEGORIES`,
matching the ticket's own examples exactly):

| `file_type` | key category | example key |
|---|---|---|
| `image` | `images` | `tenants/123/images/456.jpg` |
| `video` | `videos` | `tenants/123/videos/789.mp4` |
| `voice` | `voice` | `tenants/123/voice/1001.amr` |
| `file` | `files` | `tenants/123/files/1002.pdf` |

### Content-verified signature detection (never extension-only)

`app.media_storage.detect_media_signature_from_bytes(data)` returns
`(media_type, extension, mime_type)` or `None` — checked in a fixed order:

1. **Image** (unchanged, reuses `detect_image_type_from_bytes`): JPEG, PNG,
   GIF, WEBP magic bytes.
2. **Video**: an MP4-family `ftyp` box (`data[4:8] == b"ftyp"`) →
   `.mp4`/`video/mp4`. Deliberately scoped to the common case — this does
   not attempt full container-format parsing (e.g. it does not distinguish
   `.mov`/`.3gp` sub-brands; all MP4-family video is saved with a `.mp4`
   extension). Legacy AVI and other non-MP4-family containers are not
   recognized and are safely excluded (never migrated), not guessed at.
3. **Voice/audio**: AMR (`#!AMR`), SILK v3 (`#!SILK_V3`, the codec WeCom/
   WeChat voice messages commonly use), WAV (`RIFF`...`WAVE`), or MP3 with
   an ID3 tag (`ID3`). A bare MP3 frame-sync-only file with no ID3 tag is
   not recognized (documented limitation — avoids the well-known false-
   positive risk of frame-sync-only MP3 detection).
4. **File** (generic attachment): a deliberately conservative allow-list —
   PDF (`%PDF-`) and the ZIP family (`PK\x03\x04`, which also covers
   `.docx`/`.xlsx`/`.pptx` since those are ZIP containers). There is no
   universal signature for "any file type"; anything outside this allow-
   list returns `None` and is never migrated — this is the direct
   implementation of the ticket's "无法安全识别类型的文件" exclusion, not an
   oversight.

A signature that IS recognized but disagrees with the row's own
`file_type` (e.g. `file_type="video"` but the bytes are actually a JPEG)
is rejected as `"media_type_mismatch"` — a stale/corrupted `file_type`
must never silently relabel what actually gets uploaded.

## Candidate Selection (tenant-scoped) — `build_candidate_query()`

```
storage_backend == "local"
AND download_status == "downloaded"
AND file_type IN {"image", "video", "voice", "file"}
AND (migration_status IS NULL OR (migration_status == "failed" AND --retry))
```

- `storage_backend == "local"` is the gate that skips already-migrated
  rows: a migrated row's `storage_backend` is `"qiniu_kodo"`, so it is
  excluded by construction, and a repeated run with no candidates left
  performs **zero** provider calls — not merely an idempotent overwrite, a
  true no-op.
- `download_status == "downloaded"` restricts to rows that actually have
  bytes to migrate — `pending`/`failed` download rows have nothing to copy
  and are left entirely alone (RND-147's download lifecycle is untouched).
- `file_type IN {...}` (new in the media scope revision) excludes rows
  whose type is unset or unrecognized outright.
- `migration_status` distinguishes "never attempted" (`NULL`) from
  "attempted and failed" (`"failed"`), gated by `--retry` — same shape as
  the download worker's own `--retry` flag for `download_status="failed"`.

Rows that are **never even candidates** (not just skipped at migration
time): messages with no `media_files` row at all — miniprogram/link/
location/card/structured messages never get one, since the query starts
from `media_files`, not `archive_messages` — rows whose download failed,
and rows whose `file_type` isn't in the supported set.

Every query is scoped by a single `tenant_id`, resolved server-side from
`WECOM_CORP_ID` exactly like every other one-shot script — tenant_id is
never accepted from caller-supplied input, matching this codebase's
established rule.

## Migration Bookkeeping (migration 0006)

`storage_backend`/`storage_ref` (from RND-174's migration 0005) already
fully answer "is this row migrated" and "where do its bytes live" — no new
column is needed for that. What they cannot represent is "this row was
attempted and failed," because a failed attempt must leave
`storage_backend`/`storage_ref` **completely untouched** rather than
encoding failure into them. Migration 0006 adds, purely for this script's
own use — no serving/timeline code reads them:

| Column | Meaning |
|---|---|
| `migration_status` | `NULL` (never attempted) \| `"migrated"` \| `"failed"` |
| `migration_attempted_at` | Timestamp of the most recent attempt |
| `migration_error` | Short, sanitized diagnostic tag — never a raw path, sdkfileid, or exception string. One of: `missing_local_reference`, `local_file_missing`, `unrecognized_signature`, `media_type_mismatch`, `qiniu_upload_error`, `db_commit_error`. |

## Storage Metadata Persistence (migration 0007, QA fix)

QA review of the first revision found `storage_ref` (the object key string)
was the only migration-time metadata, and `file_size` was never
recomputed — the row simply kept whatever the original local download had
recorded. Migration 0007 adds three first-class columns, populated at the
moment of a **confirmed-successful upload**, all derived from the exact
bytes just uploaded:

| Column | Meaning |
|---|---|
| `bucket` | The Qiniu bucket the object actually lives in. Read directly from `QiniuStorageProvider.bucket` (the provider instance that performed the upload) — never re-read from `QINIU_BUCKET` independently, so it can never drift from what was actually used. `NULL` for `storage_backend="local"` (no bucket concept). |
| `mime_type` | Content-sniffed (never extension-inferred) — e.g. `"image/jpeg"`, `"video/mp4"`, `"audio/amr"`, `"application/pdf"`. |
| `checksum_sha256` | Hex-encoded SHA-256 of the exact bytes uploaded — fixed algorithm, computed once from the trusted in-memory payload (matches this codebase's existing "never re-derive from a remote round-trip" discipline — see `qiniu_storage.py`'s `file_size` handling in RND-174). |

`file_size` (pre-existing column, migration 0001) is now **overwritten**
with `len(data)` at migration time — the row's pre-existing (download-time)
value is never trusted, satisfying the ticket's explicit "不要只相信历史字段"
requirement.

**Design decision — no separate `object_key` column.** `storage_ref`
(migration 0005) is already the authoritative object key / local path
reference, read by every existing consumer (the media route, the RND-187
signed-URL route, this migration tool). Duplicating it into a second
`object_key` column would be two sources of truth for the same value with
no offsetting benefit — a drift risk, not a safety improvement. This is a
deliberate call, not an oversight; it is documented here and in the model/
migration docstrings for QA to evaluate explicitly.

Additive only, no backfill for either 0006 or 0007: every pre-existing row
gets the new columns as `NULL`. In practice this is moot today — no
migration has been executed in production yet (see "Remaining
Limitations" in the fix report), so there are no already-migrated rows
that would be missing the new metadata.

## Per-Row Migration — `migrate_one()` / `_read_and_identify()`

1. Resolve the row's local reference: `storage_ref`, falling back to legacy
   `local_path` for a pre-migration-0005 row.
2. Resolve and existence-check that reference through
   `LocalStorageProvider.get_local_path()` — the *exact* traversal/
   existence safety logic the serving route already relies on, reused
   rather than reimplemented. (Shared by `--dry-run` and the live path via
   `_read_and_identify()`, so the two can never disagree about what counts
   as migratable.)
3. Read the file's bytes and content-verify the media type via
   `detect_media_signature_from_bytes()` — never trust `file_type`, an
   extension, or msgtype alone.
4. Upload via `target_provider.save_bytes(target_ref, data)`, where
   `target_ref` is built by `build_tenant_media_key(tenant_id,
   media_key_category(media_type), archive_message_id, ext)` — the
   identical key format a future type-specific download worker would use
   for a direct Qiniu write, generalized by `media_type`.
5. Only on a confirmed-successful upload: a single DB commit atomically
   sets `storage_backend`/`storage_ref`/`file_size`/`mime_type`/
   `checksum_sha256`/`bucket`/`migration_status="migrated"`.

`migrate_one()` performs **no database write** — only a local read and a
single Qiniu upload — so the caller's DB transition is the only place state
is persisted, and a caller that decides not to persist (`--dry-run`) never
has anything to undo.

## Failure and Resumability

- Any failure (`missing_local_reference`, `local_file_missing`,
  `unrecognized_signature`, `media_type_mismatch`, `qiniu_upload_error`)
  leaves `storage_backend`/`storage_ref` **untouched** — the row is exactly
  as servable via Local as it was before the script ran. Only
  `migration_status="failed"` plus a short `migration_error` tag is
  recorded.
- A successful Qiniu upload followed by a **database commit failure** needs
  no compensating Qiniu delete: the uploaded object sits at a deterministic
  key that nothing yet references (`storage_backend` is still `"local"`).
  The next attempt simply re-uploads to the same key (a harmless overwrite)
  and retries the commit. `session.rollback()` also reverts the ORM
  object's in-memory attribute changes back to `"local"`.
- A run interrupted at any point (crash, kill, deploy) leaves every
  already-committed row migrated and every not-yet-reached row completely
  untouched — safe to re-run immediately with the same flags.

## CLI

```
python scripts/migrate_local_media_to_qiniu.py --count-only
python scripts/migrate_local_media_to_qiniu.py --dry-run --limit 20
python scripts/migrate_local_media_to_qiniu.py --limit 20 --batch-size 10
python scripts/migrate_local_media_to_qiniu.py --limit 50 --retry
```

| Flag | Meaning |
|---|---|
| `--count-only` | Report `candidate_total` / `already_migrated` / `local_cleanup_candidates` only. Zero reads or writes against storage or the local filesystem. |
| `--dry-run` | Read and content-verify every candidate exactly as a live run would (so the report is accurate), but never call Qiniu and never write to the database. |
| `--limit N` | Max rows migrated this run (default 50). |
| `--batch-size N` | Candidate fetch page size (default 20) — bounds memory for a large backlog; does **not** change commit granularity. Each row commits independently, so a crash mid-batch loses no already-committed progress. |
| `--retry` | Also reconsider rows with `migration_status="failed"`. |

Concurrency: a non-blocking `fcntl.flock` lock — a second concurrent
invocation exits `0` immediately without touching the database.

Required environment: `DATABASE_URL`, `WECOM_CORP_ID`, `STORAGE_LOCAL_PATH`
(read-only — this tool never deletes local files), and all `QINIU_*`
variables — the migration **target** is always Qiniu, independent of
`MEDIA_STORAGE_PROVIDER`/`STORAGE_BACKEND`.

## Local Cleanup — Extension Point, Not Implemented

Per RND-186's explicit instruction, local files are kept as a rollback
safety net for now — this script never deletes anything, regardless of
media type. Extension point (unchanged by the media scope revision,
already type-agnostic):

- `is_local_cleanup_candidate(media_file)` — pure predicate: `True` iff a
  row is migrated and still has a lingering `local_path`.
- `find_local_cleanup_candidates(session, tenant_id)` — the tenant-scoped
  SQL query mirroring that predicate.
- `--count-only` reports `local_cleanup_candidates: N` for visibility only.

## Object Key / Tenant Isolation

Every write goes through `build_tenant_media_key(tenant_id,
media_key_category(media_type), ...)` — a tenant-prefixed, traversal-safe
key format for every supported media type. Every query/read/write in this
script is scoped by a single resolved `tenant_id`; there is no cross-tenant
code path. This is defense in depth exactly as documented for RND-174/
RND-187 — the authorization boundary is the tenant-scoped query, not the
object key prefix, and this script performs no authorization of its own
(it never serves media to a caller — it is an offline batch tool).

## Compatibility with RND-187 (Signed URL) and the Media Route

A migrated **image** row needs zero code changes to start benefiting from
RND-187's signed-URL delivery — `GET .../media/access` resolves and signs
it exactly like any image row the download worker wrote directly to
Qiniu. **A migrated video/voice/file row is a different story: the media
route and the RND-187 signed-URL route both still only serve `msgtype ==
"image"` rows** (`app/routers/conversations.py`'s `_resolve_authorized_media`
hard-gates on this) — this is unchanged, on purpose, since Media Access
API / Signed URL changes are explicitly out of this ticket's scope. A
migrated video/voice/file row is therefore fully durable in Qiniu with
complete metadata, but not yet retrievable through any existing route. See
"Remaining Limitations" in the fix report.

## Database Changes

- `0006_media_migration_bookkeeping.py`: adds `media_files.migration_status`
  (indexed), `migration_attempted_at`, `migration_error`.
- `0007_media_migration_metadata.py`: adds `media_files.bucket`,
  `mime_type`, `checksum_sha256`.

Both are additive only — no backfill, no destructive change, no rewrite of
any existing column. `downgrade()` for each drops exactly its own columns,
leaving migration 0005's `storage_backend`/`storage_ref` and everything
else untouched.

## Non-Goals (unchanged by this ticket)

Message parsing, Media Access API shape, Signed URL policy, CDN policy,
the media serving routes, multi-tenant architecture, billing, and the
frontend are all unmodified — verified by running the full existing test
suite unchanged.
