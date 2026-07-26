# Current Status — 365 WeCom Archive

**Last updated:** 2026-07-13

## Overall Status

**Repository status:** all core feature areas are implemented in code, and the
repo contains the main local bring-up path plus worker/media deployment assets.
Live production state should be verified separately from git.

---

## What's Complete (P0)

| Feature | RND | Notes |
|---------|-----|-------|
| WeCom Archive API pull | ✅ | C SDK integration, encrypted message pull, cursor-based resumption |
| Message decryption | ✅ | RSA + AES decryption pipeline |
| Tenant foundation | RND-156, RND-111 | Multi-tenant data model, default tenant bootstrap |
| Auth — WeCom OAuth | RND-110 | Employee login via WeCom self-built app OAuth |
| Auth — Password fallback | RND-112 | Temporary mode for dev/staging |
| Conversation Review Console | RND-154, RND-157 | Three-column UI with WeCom-style chat bubbles |
| Message timeline | RND-152 | Ordered timeline with auto-load older history |
| Auto-refresh | RND-153 | Periodic UI refresh, new-message indicator |
| i18n | RND-157 | Chinese + English locale switching |
| Media download worker | RND-151, RND-168 | Scheduled image download via systemd timer |
| Media storage abstraction | RND-185 | Pluggable `MediaStorageProvider` interface; `LocalStorageProvider` |
| Qiniu Kodo storage provider | RND-174 | Optional second backend (`MEDIA_STORAGE_PROVIDER=qiniu_kodo`); local remains default/rollback. Per-row `storage_backend`/`storage_ref` (migration 0005) so local and Qiniu media coexist safely; HTTPS-only private retrieval proxied through the existing authenticated route |
| System diagnostics | RND-178, RND-180 | Message reachability audit with aggregate stats |
| Message type placeholders | RND-173, RND-177 | Named display for all WeCom message types |
| Corp ID uniqueness | RND-184 | Active corp_id uniqueness constraint across tenants |
| Tenant WeCom config uniqueness | RND-184 | Duplicate active corp_id detection (ORM + DB) |
| Company homepage | RND-171 | For ICP beian filing |

## What's In Progress / Pending

| Feature | Priority | Status | Notes |
|---------|----------|--------|-------|
| Local → Qiniu historical media migration | P2 | Implemented locally; not yet executed in production (RND-186) | `scripts/migrate_local_media_to_qiniu.py` — manual, repeatable, resumable tool; migrates any content-verified supported media type (image/video/voice/file), persists full storage metadata (bucket/mime_type/checksum_sha256). Passing full test suite. No production rows migrated yet. Video/voice/file rows are not yet servable through the media route once migrated — see docs/research/rnd_186_local_qiniu_migration.md for the suggested follow-up ticket |
| Media URL / Signed URL / CDN delivery | P2 | Implemented locally; developer re-acceptance pending (RND-187) | `GET .../media/access` mints short-lived, single-object Qiniu Signed URLs (official SDK) after full tenant/ownership authorization; browser fetches directly from `media.example.com`. Local-backed media still proxies unchanged. Not yet deployed to production — see docs/API.md and docs/ops/media_storage_ops.md |
| Multi-tenant onboarding UI | P3 | Not started | Admin UI for adding tenant configs |
| App secret encryption | P3 | Not started | Encrypt at rest in tenant_wecom_configs |
| Conversation export | P3 | Not started | Security approval required |
| AI summarization | P4 | Not planned | Future product direction |

## Deprecated / Removed

Nothing deprecated in the current codebase.

## Key Technical Debt

- `tenant_wecom_configs.app_secret` stored in plaintext (Phase 1 acceptable for internal deployment)
- No formal archive-seat roster (derived at query time from two heuristic signals)
- Session table cleanup not automated
- No full-text search engine (uses PostgreSQL tsvector + GIN index)
- No automated test for full end-to-end sync + decrypt pipeline (requires WeCom SDK)

---

*Part of the docs/ai/ set — maintained for AI agent onboarding.*
