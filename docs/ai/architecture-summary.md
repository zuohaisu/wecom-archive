# Architecture Summary — 365 WeCom Archive

**One sentence:** A Python FastAPI backend that pulls encrypted messages from WeCom on a schedule, decrypts them, stores everything in PostgreSQL, serves a conversation review console to authenticated admins, and downloads media through a pluggable storage provider.

---

## Module Relationship

```
app/main.py  (FastAPI app entry, HTML routes, health endpoint)
  │
  ├── app/routers/auth.py              ─── Auth endpoints (WeCom OAuth, password)
  ├── app/routers/conversations.py     ─── Conversation aggregation API + media serving
  ├── app/routers/reachability_audit.py ─── Message reachability audit API
  ├── app/routers/wecom_events.py      ─── WeCom server-to-server events
  │
  ├── app/auth.py                ─── Session handling, get_current_user(), token caching
  ├── app/media_storage.py       ─── MediaStorageProvider interface + LocalStorageProvider
  ├── app/media_classification.py─── Media type detection (magic bytes)
  ├── app/i18n_assets.py         ─── Loads i18n.js for admin UI
  ├── app/reachability_audit.py  ─── Reachability classification logic
  ├── app/wecom_contacts.py      ─── Contacts DB operations
  ├── app/display_names.py       ─── Display name resolution
  │
  ├── app/sdk/wecom_sdk.py       ─── WeCom C SDK wrapper (ctypes)
  │
  └── app/db/
      ├── models.py              ─── All SQLAlchemy ORM models
      ├── session.py             ─── DB session management
      ├── base.py                ─── Declarative base
      └── contacts.py            ─── Contact CRUD
```

## Call Flow — Request Lifecycle

```
HTTP Request
  │
  ▼
FastAPI route handler
  │
  ├── Depends(get_current_user)  ─── Reads session cookie → validates in admin_sessions → returns (user, tenant_id)
  │
  ├── Query filters by tenant_id (WHERE tenant_id = <session.tenant_id>)
  │
  ├── Business logic (reachability audit, conversation aggregation, etc.)
  │
  └── Response
      ├── JSON (API endpoints)
      ├── HTML (admin pages)
      └── FileResponse (media serving)
```

## Database Relationships

```
tenants
  ├── tenant_wecom_configs  (1:1 per tenant, maps to WeCom corp)
  ├── admin_users           (employees who have logged in)
  ├── admin_sessions        (active login sessions, scoped by tenant_id)
  │
  ├── archive_messages      (core: encrypted envelope + decrypted payload)
  │   ├── archive_message_recipients (per-receiver lookup rows)
  │   └── media_files       (download state for media attachments)
  │
  ├── sync_states           (cursor tracking: last seq per tenant+corp)
  ├── contacts              (lightweight WeCom user cache)
  └── key_versions          (publickey_ver → private key path mapping)
```

## Worker Pipeline

```
[systemd timer] → run_archive_worker_once.py
                                        │
                          ┌─────────────┴─────────────┐
                          ▼                           ▼
              sync_wecom_archive_once.py    decrypt_wecom_messages_once.py
              (pull encrypted messages)     (decrypt + extract fields)
                          │                           │
                          ▼                           ▼
                    sync_states                   archive_messages
                    (cursor update)               (decrypted_payload populated)

[separate timer] → download_wecom_image_media_once.py
                    (download image media via SDK → .part → validate → publish)
```

## Auth Flow

```
WeCom OAuth:                   Password fallback:
Browser → /admin/login         Browser → /admin/login
  → GET /api/auth/wecom/login      → POST /api/auth/password/login
  → redirect to WeCom OAuth URL    → verify PBKDF2 hash
  → callback → validate state      → create session
  → exchange code for UserId       → set cookie → redirect to /admin/conversations
  → verify active employee
  → resolve tenant_id
  → create session
  → set cookie → redirect to /admin/conversations
```

---

*Part of the docs/ai/ set — maintained for AI agent onboarding.*