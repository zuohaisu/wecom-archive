# RND-174 Qiniu Kodo Provider Integration

## Scope

RND-174 adds `QiniuStorageProvider` as a second implementation of the
RND-185 `MediaStorageProvider` contract, selectable via
`MEDIA_STORAGE_PROVIDER=qiniu_kodo` for new writes. It does not implement
historical local-to-Qiniu migration (RND-186) or client-facing signed URL /
CDN delivery (RND-187), and does not change tenant authorization. `local`
remains the default provider and the rollback target for new writes.

**Post-ship QA pass.** An independent QA review of the first RND-174
implementation found four blocking issues, fixed in this revision:

1. Media rows had no unambiguous per-row storage backend discriminator —
   `media_files.local_path` was reused for both local paths and Qiniu
   object keys. **Fixed:** added `media_files.storage_backend` /
   `storage_ref` (migration 0005) — see "Storage Backend and Reference".
2. Private Qiniu retrieval built the fetch URL with a hardcoded
   `http://` prefix. **Fixed:** `QINIU_DOMAIN` is now validated as an
   HTTPS base URL and joined safely — see "HTTPS Domain Validation".
3. The worker could delete a successfully published object if a later
   metadata/stat call failed. **Fixed:** `file_size` is now computed from
   the in-memory downloaded payload, never a post-publish
   `size_bytes()` call — see "Upload / Publish and Cleanup Semantics".
4. Qiniu availability/stat errors were collapsed into "object missing".
   **Fixed:** `exists()`/`size_bytes()` now raise
   `MediaStorageUnavailable` for anything that isn't a confirmed
   not-found — see "Error Classification".

## Provider Contract (unchanged)

`backend/app/media_storage.py`'s `MediaStorageProvider` ABC is unchanged
from RND-185. `QiniuStorageProvider` (`backend/app/qiniu_storage.py`)
implements every method:

- `save_bytes` — uploads via `qiniu.put_data`, overwriting the same key
  deterministically (no `insertOnly` policy set).
- `read_bytes` — fetches via a short-lived (60s) internal
  `Auth.private_download_url` (over HTTPS) + `httpx.get`; the signed URL
  is never logged, returned, or held past the single fetch.
- `exists` / `size_bytes` — `BucketManager.stat`; raise
  `MediaStorageUnavailable` for anything that is not a confirmed
  found/not-found result (see "Error Classification").
- `delete` — `BucketManager.delete`; best-effort, never raises.
- `replace` — `BucketManager.move` (server-side rename; no
  download+reupload).
- `get_download_url` — always `None`, matching `LocalStorageProvider`
  (see "Media Serving" below).
- `supports_local_path` — `False`; `get_local_path` — always `None`.

All `qiniu` SDK usage is confined to `app/qiniu_storage.py` — the worker
and the media route only ever call the provider interface.

## SDK Boundary and Local-Mode Independence

`app/qiniu_storage.py` is only imported inside
`media_storage.get_media_storage_provider()`'s `qiniu_kodo` branch (a
local, deferred import). Local mode — the default — never imports the
`qiniu` package and never reads `QINIU_*` configuration. Selecting
`qiniu_kodo` (via config or a row's own `storage_backend`) with any of
`QINIU_ACCESS_KEY` / `QINIU_SECRET_KEY` / `QINIU_BUCKET` / `QINIU_DOMAIN`
missing or invalid raises `QiniuConfigurationError` (a
`MediaStorageConfigurationError`) immediately — there is no silent
fallback to local. **Before enabling Qiniu in any environment, refresh
backend dependencies** (`pip install -r requirements.txt`) so the `qiniu`
package is actually installed.

## Provider Resolution: Default Write Provider vs. Per-Row Read Provider

Two distinct resolution paths, both in `app/media_storage.py`:

- **`get_media_storage_provider(storage_backend=None)`** — the general
  factory. With an explicit `storage_backend` ("local" / "qiniu_kodo"),
  it constructs exactly that provider. With `storage_backend=None`, it
  falls back to the deployment's configured *default write* provider
  (`MEDIA_STORAGE_PROVIDER`, then `STORAGE_BACKEND`, then `"local"`).
  - `get_default_media_storage_provider()` is a named alias for the
    no-argument form — used by the download worker, which needs "the
    provider new writes go through."
  - `get_media_storage_provider_for_backend(storage_backend)` requires
    the argument — raises `UnsupportedMediaStorageProvider` rather than
    silently defaulting if it's empty.
- **`resolve_media_file_state(media_file)`** — resolves the provider from
  a `media_files` row's own `storage_backend`/`storage_ref` (falling back
  to a legacy `local_path` only when `storage_backend` was never
  backfilled — see `resolve_effective_storage_reference`). This is what
  the media route and timeline serializer use to decide whether a row is
  servable — **never** the deployment-wide default.

This separation is what makes switching `MEDIA_STORAGE_PROVIDER` safe:
it only changes where **new** media is written; every existing row keeps
resolving through the provider recorded on it.

Unknown provider names — from config or from a row's `storage_backend` —
raise `UnsupportedMediaStorageProvider` (a `MediaStorageConfigurationError`)
rather than falling back to local.

## Configuration

| Variable | Required for qiniu_kodo | Notes |
|---|---|---|
| `QINIU_ACCESS_KEY` | yes | never logged |
| `QINIU_SECRET_KEY` | yes | never logged |
| `QINIU_BUCKET` | yes | assumed private |
| `QINIU_DOMAIN` | yes | **full HTTPS base URL**, e.g. `https://private-media.example.com` — see below |
| `QINIU_REGION` | no | falls back to SDK auto-discovery |
| `QINIU_TIMEOUT_SECONDS` | no | default 30 |

See `.env.example` for the full documented block.

### HTTPS Domain Validation

`app/qiniu_storage.py:normalize_qiniu_https_base_url()` validates
`QINIU_DOMAIN` at provider-construction time:

- scheme must be exactly `https` — a missing scheme, `http://`, or any
  other scheme raises `QiniuConfigurationError` immediately (the original
  implementation hardcoded `http://` regardless of the configured value,
  which both downgraded transport security and, for an operator who
  supplied the documented `https://` form, produced a malformed
  `"http://https://host/..."` URL).
- host must be present and well-formed; no query string or fragment.
- a trailing slash (or trailing-slash path) is stripped so joining with a
  storage_ref never produces a double slash.

The resulting base URL is joined with a URL-safe (`urllib.parse.quote`)
storage_ref in `QiniuStorageProvider._object_url()` — never raw string
concatenation. `read_bytes()` always builds `https://...` for
`Auth.private_download_url`; HTTPS is never downgraded to HTTP, and the
signed URL/token is never logged or returned to any caller.

## Object Key Design

`app/media_storage.py:build_tenant_media_key(tenant_id, category, identifier, suffix="")`
produces:

```
tenants/{tenant_id}/{category}/{identifier}{suffix}
```

For image downloads: `tenants/{tenant_id}/images/{archive_message_id}{ext}`
(and `...{archive_message_id}.part` for the in-flight temp object) — the
same format the RND-147 worker already used for local paths, now shared
by both providers since it is provider-opaque (`Path` semantics are never
assumed).

Sanitization: every segment is passed through `_sanitize_key_segment`,
which collapses any character outside `[A-Za-z0-9_.-]` to `_` and strips
leading dots, so a segment can never become empty, `.`, or `..` — the
traversal-safety property is "no `/`-delimited path segment equals
`.`/`..`/empty", not "no `..` substring anywhere" (a `..` embedded inside
a single sanitized segment, e.g. from `tenant/../escape`, is inert — it is
not its own path component). `tenant_id` and `archive_message_id` are
already DB-trusted values; this sanitization is defense in depth (object
key prefixing is not an authorization boundary — see "Tenant Isolation"
below), not a response to a known caller-supplied value at this call
site.

Deterministic: the same `(tenant_id, category, identifier)` always
produces the same key, so retried/idempotent processing overwrites in
place instead of accumulating duplicate objects.

## Storage Backend and Reference (migration 0005)

**Fields added to `media_files`:**

- `storage_backend` (`String(32)`, nullable, indexed) — `"local"` or
  `"qiniu_kodo"`.
- `storage_ref` (`Text`, nullable) — that provider's own reference (a
  local path for `"local"`, a Qiniu object key for `"qiniu_kodo"`).

**Backfill (part of migration 0005's `upgrade()`, not a separate script):**
every pre-existing row is stamped `storage_backend='local'`,
`storage_ref=<its local_path>` (a row with no `local_path` — e.g. still
`pending`/`failed` — gets `storage_backend='local'`, `storage_ref=NULL`).
This is safe because Qiniu did not exist as a selectable provider before
RND-174, so every historical row was necessarily written by
`LocalStorageProvider`. No historical local media is migrated to Qiniu by
this migration — that remains RND-186.

**`local_path` compatibility.** The column is kept, untouched, as a
legacy/local-only reference: new code reads `storage_backend`/
`storage_ref` first, falling back to a populated `local_path` only when
`storage_backend` was never backfilled (`resolve_effective_storage_reference`
in `app/media_storage.py`). A Qiniu-backed row's `local_path` is always
left `None` by the worker — it must never be treated as an authoritative
Qiniu reference. `media_files.oss_key` remains unused/always `None`
(unchanged from before RND-174).

**Mixed storage.** Because the provider is resolved per-row, a local row
and a Qiniu row can appear in the same conversation timeline and both
serve correctly, regardless of the current `MEDIA_STORAGE_PROVIDER`
value. See "Rollback" below for what switching that value does and does
not do.

## Upload / Publish and Cleanup Semantics

Worker flow (`scripts/download_wecom_image_media_once.py`): chunks are
assembled fully in memory (`data`), written to a `.part` object via
`provider.save_bytes`, byte-signature-checked, then published via
`provider.replace(part_ref, final_ref)`. `download_one()` returns
`(outcome, detail, file_size)` — `file_size = len(data)`, computed
**before** upload from the in-memory payload, never from a post-publish
remote stat call.

For Qiniu, `replace()` maps to `BucketManager.move` — a server-side
rename, not a download+reupload — so the complete, validated payload is
already present at `final_ref` the moment `replace()` returns 2xx; there
is no window where a partially-written object is reachable at the final
key.

**Cleanup rule (QA fix #3).** The caller (`_run()`) persists
`download_status`, `storage_backend`, `storage_ref`, and `file_size` in a
single `try` block that performs *only* database writes — no remote
provider call happens inside it. This means the `except Exception:`
around that block can only ever be reached by a genuine database commit
failure, never by a transient stat/metadata hiccup, so it is safe for
that block to delete the now-unreferenced final object as a deliberate
rollback. A transient post-publish metadata/stat failure — the original
bug — cannot reach this path at all, because nothing on the success path
calls `size_bytes()`/`exists()` anymore.

Other cleanup rules (unchanged): the worker's `finally` block deletes the
`.part` temp object on every non-`"downloaded"` outcome (download error,
write error, unsupported type, publish/rename failure) — the final object
is never touched on these paths because it was never created. A retried
attempt re-uploads to the same deterministic `.part` key (overwrite) and
moves it to the same deterministic final key (`force=true`), so retries
converge instead of accumulating orphaned objects.

`get_or_reset_media_file()` resets `storage_backend`/`storage_ref` to
`None` together with `local_path`/`file_size` when resetting a row for a
fresh attempt, so a retried row can never keep a stale backend/reference
pair from a previous attempt (e.g. a previously Qiniu-backed row being
retried after the default write provider was switched back to local).

## Media Serving

RND-187 (signed URL / CDN delivery) is explicitly out of scope. The
existing authenticated route,
`GET /api/conversations/{conversation_id}/messages/{msgid}/media`, is
unchanged in shape and unchanged for local-provider requests. The
provider is resolved from the row's own `storage_backend`/`storage_ref`
(`resolve_effective_storage_reference`), not from the deployment-wide
default. For a non-local provider (`supports_local_path() == False`), the
route fetches bytes via `provider.read_bytes()` and returns them directly
(`Response(content=data, media_type=...)`) instead of `FileResponse`. The
Qiniu bucket is assumed private; `get_download_url()` always returns
`None`, so no Qiniu URL — signed or permanent — is ever handed to a
caller outside `app/qiniu_storage.py`.

`app/media_storage.py:resolve_image_file_state(storage_ref, storage_backend)`
— the tri-state predicate shared by the timeline serializer and the media
route — accepts an explicit `storage_backend` (production call sites
always pass the row's own value via `resolve_media_file_state()`;
`storage_backend=None` is kept only for backward compatibility with
pre-remediation single-argument callers, falling back to the deployment
default). For a local provider it behaves exactly as in RND-185; for a
non-local provider it uses `provider.exists()` (which itself now
distinguishes confirmed-missing from unavailable) plus an extension check
on the storage-ref string.

**Response codes** (media route):

| Code | Meaning |
|---|---|
| 404 | wrong tenant/conversation, non-image, missing/pending/failed row, unsafe storage reference, disallowed extension, or a *confirmed* missing remote object |
| 503 | the storage provider could not confirm the object's state (`MediaStorageUnavailable` — network timeout, auth failure, bucket error, SDK failure) |
| 502 | a storage operation otherwise failed against a provider that did respond (`MediaStorageOperationError`) |
| 500 | the row names an unset/unsupported/misconfigured storage backend (`MediaStorageConfigurationError`) |

A provider outage is never reported as a plain 404 (QA fix #4/#5).

The timeline serializer (list view, many rows per response) treats a
`MediaStorageUnavailable`/`MediaStorageConfigurationError` for any single
row as `file_state = "missing"` for that row only, rather than failing
the whole page — the dedicated media route is where a genuine outage is
surfaced distinctly, for a request that is actually about one object.

## Tenant Isolation

Unchanged authorization sequence (RND-156): authenticate → resolve tenant
from session → tenant-scoped `_fetch_conversation_messages` →
tenant-scoped `MediaFile.tenant_id` lookup → **only then** is a storage
provider resolved or called. `QiniuStorageProvider` has no tenant/authz
logic and cannot be used to bypass it — a syntactically valid Qiniu key
from another tenant is unreachable through this route unless the
authorized `media_files` row itself points at it, which the tenant-scoped
query prevents. The `tenants/{tenant_id}/...` key prefix is defense in
depth, not the authorization boundary. The provider is never part of
authorization — it is only reached after authorization has fully
completed.

## Error Classification

`app/media_storage.py` defines a small exception hierarchy so callers can
distinguish "confirmed missing" from "provider unavailable" from
"misconfigured" — collapsing all three into a 404-shaped "missing" was
QA finding #4:

- `MediaObjectNotFound(FileNotFoundError)` — a confirmed "not found"
  response from the provider.
- `MediaStorageUnavailable(OSError)` — the operation could not be
  confirmed to succeed or fail (network timeout, auth failure, bucket
  error, SDK/service failure). Never means "the object doesn't exist."
- `MediaStorageConfigurationError(ValueError)` — provider configuration
  is missing or invalid (`QiniuConfigurationError` subclasses this;
  `UnsupportedMediaStorageProvider` subclasses this too).
- `MediaStorageOperationError(OSError)` — a storage operation failed for
  a reason other than a confirmed not-found or unavailable.

Each subclasses the builtin exception type existing callers already
caught (`FileNotFoundError`/`OSError`/`ValueError`), so no pre-existing
`except` clause anywhere in the codebase needed to change to keep
compiling — only the route and worker call sites that specifically need
the finer distinction were updated.

`QiniuStorageProvider.exists()`/`size_bytes()`: a confirmed 612 ("no such
file/entry") status returns `False` / raises `MediaObjectNotFound`; a 2xx
status returns `True` / the size; **anything else — including an
exception from the SDK/network call itself — raises
`MediaStorageUnavailable`**, never a silent `False`/"missing" (the exact
bug QA finding #4/#5 describes). `delete()` remains best-effort and never
raises, matching `LocalStorageProvider`.

`QiniuStorageProvider` never logs or raises raw Qiniu SDK response
bodies, URLs, or exception text — `qiniu.http.response.ResponseInfo.error`
/ `.text_body` / `.url` can carry the request URL or body, so only a
fixed message plus an HTTP-style status code is used. Network/SDK
exceptions are reduced to `{fallback} ({type(exc).__name__})` — never
`str(exc)` — since for `httpx`/`requests` errors that can embed the
(token-bearing) request URL.

## Rollback

Setting `MEDIA_STORAGE_PROVIDER` back to `local` changes **only** where
new downloads are written. It does **not** reinterpret or migrate rows
already written to Qiniu — those rows keep `storage_backend="qiniu_kodo"`
and continue to be served through `QiniuStorageProvider`, which requires
Qiniu credentials to remain configured. If Qiniu credentials are removed
while Qiniu-backed rows exist, those specific rows become unavailable
(the media route returns 500 for a misconfigured backend named on a row
it can no longer construct a provider for) — existing local rows are
completely unaffected either way. A full rollback that also stops
depending on Qiniu requires migrating those rows' bytes back to local
storage first — RND-186 ships a Local → Qiniu migration tool only; a
Qiniu → Local reverse-migration tool remains unimplemented — before
removing Qiniu credentials.
