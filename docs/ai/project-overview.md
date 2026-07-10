# Project Overview — 365 WeCom Archive

**One-line description:** Internal admin system that archives, decrypts, stores, and provides review capabilities for WeCom (企业微信) enterprise conversation data.

## Project Goal

Provide a compliance and operational review tool for organizations using WeCom. The system automatically pulls conversation messages and media from the WeCom Conversation Archive API, decrypts them, and presents them in a searchable, timeline-based review console accessible to authorized administrators.

## Primary Capabilities

| Capability | Description |
|-----------|-------------|
| **Archive Sync** | Scheduled pull of encrypted messages from WeCom Archive API via C SDK |
| **Message Decryption** | RSA + AES decryption using WeCom's encryption scheme |
| **Media Download** | Scheduled download of image media from WeCom (with magic-byte validation) |
| **Conversation Review Console** | Three-column UI (staff/contact selector → conversation list → message timeline) |
| **Admin Auth** | WeCom OAuth employee login, with password fallback mode |
| **Tenant Management** | Multi-tenant data model (single tenant in current MVP) |
| **System Diagnostics** | Message reachability audit page (aggregate stats, no message content) |
| **i18n** | Chinese + English language switching in admin UI |

## Current State

**Codebase status:** core feature areas are implemented in this repository.
Live production state should be verified separately from repository contents.

All core features are implemented. The system is currently in maintenance/enhancement mode — new features are added via Linear issues (RND-* pattern).

### What's Done

- Full archive sync + decryption pipeline (systemd-timed)
- Image media download pipeline (separate systemd timer)
- Conversation Review Console with all review modes
- WeCom OAuth login + password fallback
- Tenant-aware data model and session scoping
- Media storage abstraction layer (pluggable provider)
- Message reachability diagnostics

### What's In Progress / Planned

See [current-status.md](current-status.md) for the latest status.

---

*Part of the docs/ai/ set — maintained for AI agent onboarding.*
