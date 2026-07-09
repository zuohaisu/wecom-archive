# RND-185 Media Storage Abstraction

## Scope

RND-185 introduces a storage provider boundary for media bytes while keeping
the current local-filesystem behavior unchanged. It does not implement Qiniu
Kodo, migration, signed URLs, CDN behavior, frontend changes, or schema
changes.

## Current Behavior Preserved

- `media_files.local_path` remains the local-compatible storage reference for
  already-downloaded media.
- The admin timeline still emits the same authenticated media route URL only
  when a downloaded image is servable.
- `GET /api/conversations/{conversation_id}/messages/{msgid}/media` still
  returns a `FileResponse` for local files and a plain 404 for missing,
  unsafe, pending, failed, non-image, or unsupported media.
- The image media worker still writes a `.part` object first, validates the
  downloaded bytes, then publishes the final image object.
- Tenant authorization remains outside the storage provider.

## Provider Contract

`backend/app/media_storage.py` defines `MediaStorageProvider`, a small
byte-storage interface:

- `save_bytes(storage_ref, data) -> str`
- `read_bytes(storage_ref) -> bytes`
- `exists(storage_ref) -> bool`
- `delete(storage_ref) -> bool`
- `replace(source_ref, target_ref) -> str`
- `size_bytes(storage_ref) -> int`
- `get_download_url(storage_ref) -> Optional[str]`
- `supports_local_path() -> bool`
- `get_local_path(storage_ref) -> Optional[Path]`

The provider deliberately does not decide whether an admin or tenant may
access media. Callers must perform application-level authorization before
calling provider methods.

## LocalStorageProvider

`LocalStorageProvider` is the only implemented provider in this ticket. It
uses `STORAGE_LOCAL_PATH` as its root and keeps the existing safety rules:

- relative references are resolved under the configured root
- absolute paths must resolve under the configured root
- path traversal and symlink escape resolve to missing media
- local-path serving is supported for FastAPI `FileResponse`
- deletes are best effort and path-safe

This preserves the existing `local_path`-based database behavior while
moving filesystem operations behind the provider boundary.

## Provider Selection

The factory is `get_media_storage_provider()`.

Configuration:

- `MEDIA_STORAGE_PROVIDER=local` selects `LocalStorageProvider`
- if unset, existing `STORAGE_BACKEND=local` is honored as a compatibility
  alias
- if both are unset, the provider defaults to local

Only `local` is implemented for RND-185. Unsupported provider names fail
explicitly instead of silently falling back.

## Affected Media Paths

- Timeline serialization still uses `resolve_image_file_state()` before
  advertising `media_url`; that helper now resolves through the active
  provider.
- The media route still uses `resolve_servable_image_path()` before serving;
  that helper now obtains the local path through the active provider.
- The image media worker now uses the provider for `.part` writes, final
  publish, file-size lookup, and orphan cleanup.
- Existing image type detection remains byte-signature based for downloads
  and extension allow-list based for serving.

## Tenant Isolation

RND-156 isolation remains in the application layer:

- timeline media lookup stays scoped by `MediaFile.tenant_id`
- media download route still filters `MediaFile.tenant_id == current tenant`
- worker candidate and reset queries remain tenant-scoped
- provider calls receive only references that were authorized by those
  tenant-scoped lookups

The storage provider has no tenant authorization logic and must not be used
as an authorization boundary.

## Future Qiniu Provider

RND-186 can add `QiniuStorageProvider` by implementing the same provider
contract. Expected changes should be localized to provider construction and
serving strategy:

- implement upload/read/exists/delete/size against Qiniu object keys
- decide whether `replace()` maps to copy/delete or direct final-object upload
- return signed/provider URLs from `get_download_url()` if RND-188 chooses
  direct signed URL serving
- update media route behavior only if local `FileResponse` is no longer the
  selected serving strategy

Business-layer tenant checks and `media_files` query scoping should remain
unchanged.
