# Project Overview — Crowntime WeCom Archive

> **Status:** secondary AI-onboarding synopsis. The authoritative current map is
> [`docs/architecture/current-state.md`](../architecture/current-state.md).

**One-line description:** Proprietary hosted multi-tenant SaaS that archives,
decrypts, stores, and provides tenant-isolated WeCom conversation review.

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
| **Tenant Management** | Multi-tenant tenant/identity, lifecycle, credential, and access model |
| **System Diagnostics** | Message reachability audit page (aggregate stats, no message content) |
| **i18n** | Chinese + English language switching in admin UI |

## Current State

**Codebase status:** core feature areas are implemented in this repository.
Live production state should be verified separately from repository contents.

New work is tracked in GitHub Issues. RND identifiers in history are migrated issue keys; they do not require Linear.

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
