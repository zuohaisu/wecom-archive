# Known Pitfalls — 365 WeCom Archive

Common traps, gotchas, and "don't touch this" areas that have caused problems in the past.

---

## Critical — Do Not Change Without Understanding

### 1. Tenant Scoping

**Every** admin query **must** filter by `WHERE tenant_id = <session.tenant_id>`. The `tenant_id` is the sole authorization boundary. Never accept `tenant_id` from user input.

**Bad:**
```python
# Missing tenant_id filter — leaks data across tenants
query = db.query(ArchiveMessage).filter(ArchiveMessage.sender == sender)
```

**Good:**
```python
query = db.query(ArchiveMessage).filter(
    ArchiveMessage.sender == sender,
    ArchiveMessage.tenant_id == tenant_id,  # from session
)
```

### 2. `/api/wecom/archive/events` Must NOT Be Session-Protected

This endpoint is called by WeCom servers (server-to-server). If you add `Depends(get_current_user)` here, the entire archive event callback will break silently.

### 3. Media Path Traversal

The `resolve_safe_media_path()` function in `media_storage.py` is the only valid way to resolve media file paths. **Never** construct file paths directly from `media_files.local_path` — that field comes from the database and could be compromised.

### 4. WeCom SDK Is a C Library

The SDK (`app/sdk/wecom_sdk.py`) is wrapped via `ctypes`. It:
- Must be initialized before every call (`init_sdk()` / `destroy_sdk()`).
- Has a per-sdk-instance lock.
- Is only available on Linux (production).
- Cannot be tested in CI without the actual `.so` file.

### 5. Lock Files Prevent Concurrent Workers

Both the archive worker and media download worker use `fcntl.flock` on lock files. Never remove or change these locks without understanding the concurrency model. If you add a new worker, it must use its own lock file path.

---

## Common Mistakes

### Auth Mode Confusion

`AUTH_MODE=wecom` (default) and `AUTH_MODE=password` are mutually exclusive. If you test the password endpoint while `AUTH_MODE=wecom`, you get a 404. This is intentional.

### Environment Variables

- `WECOM_OAUTH_SECRET` is different from `WECOM_ARCHIVE_SECRET`. They serve different purposes.
- `ADMIN_DOMAIN` must be the domain only (no protocol, no port, no path).
- `MEDIA_STORAGE_PROVIDER=local` is the new RND-185 selector. `STORAGE_BACKEND=local` is the backward-compatible alias.

### Alembic Migrations

Migrations must be idempotent where possible. If you add a migration that:
- Creates a unique index, handle the case where duplicate data already exists.
- Adds a NOT NULL column, provide a default or backfill first.

### Archive Sync Order

The archive worker runs sync **then** decrypt. If only sync runs (decrypt fails), messages exist in the DB with `decrypt_status='pending'`. They're invisible in the review console until decrypted.

### Media Download

- The media download worker only handles **images** by scope (RND-151). Voice, video, and file downloads are not implemented.
- The timer uses `--since-hours 72` to limit candidates — old messages may have expired WeCom download windows.
- `--retry` is intentionally **not** used by the timer — retries are manual only.

---

## Areas Most Likely to Break

| Area | Risk | Why |
|------|------|-----|
| Conversation aggregation logic | High | `_collect_staff_ids()` heuristic — changes here affect what accounts appear in the console |
| Timeline message dedup | High | `mergeMessagesByMsgid()` in JS — incorrect merge leads to duplicate or missing messages |
| Tenant bootstrap script | Medium | `bootstrap_default_tenant.py` must be run exactly once; re-running may cause conflicts |
| Auth state token cleanup | Medium | OAuth state tokens are in-memory with 5-min TTL — not suitable for multi-process deployments |
| i18n locale switching | Low | Client-side only (localStorage) — server-rendered HTML always starts in zh-CN |
| Reachability audit query | Medium | Can be slow on large archives — the /api/messages query with reachability join is not optimized for extreme scale |

---

## Debugging Tips

- Check `journalctl -u wecom-archive-worker.service` for worker failures.
- Check `journalctl -u wecom-archive-media-download.service` for media download failures.
- The `/health` endpoint is the first thing to check when deployment seems broken.
- Use `alembic upgrade head --sql` to preview migration SQL before applying.
- Run `python scripts/download_wecom_media_once.py --count-only` to see media download progress without downloading (unified pipeline: image/voice/video/file/emotion; use `--types` to narrow).

---

*Part of the docs/ai/ set — maintained for AI agent onboarding.*