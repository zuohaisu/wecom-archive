# Business Terms — Crowntime WeCom Archive

## Domain Terminology

### Core Concepts

| Term | Chinese | Definition |
|------|---------|------------|
| **Tenant** | 租户 | A company/organization using the system. Each tenant has its own WeCom credentials, archive data, and admin users. Current MVP has one default tenant. |
| **WeCom (企业微信)** | 企业微信 | Enterprise WeChat — Tencent's enterprise communication platform. The source of archived conversations. |
| **Corp** | 企业 | A corporation/company on WeCom platform. Maps 1:1 with a Tenant in the current data model. `corp_id` is the WeCom-level identifier. |
| **Archive** | 存档 | The encrypted conversation data pulled from WeCom's Conversation Archive API. Not a backup — a compliance/audit record. |
| **Conversation** | 会话 | A grouping of messages between participants. Two types: **direct** (1:1) and **group** (multi-participant chat room). |
| **Message** | 消息 | A single WeCom conversation message — text, image, voice, video, file, etc. Stored as `archive_messages` rows. |
| **Media** | 媒体 | File attachments in messages (images, voice recordings, videos, documents). Downloaded separately from the archive sync. |
| **Monitored Account** | 监控账号 | A WeCom account whose conversations are subject to archive review. Determined by two signals: (a) `staff_` prefix convention, (b) any admin user who also appears as a sender/recipient in the archive. |
| **Contact** | 联系人 | A counterpart identity encountered in archived conversations — external contacts, other employees, bot identities. |

### System Roles

| Term | Definition |
|------|------------|
| **Administrator** | A human who has authenticated via WeCom OAuth (or password) and can access the Review Console. Stored in `admin_users`. |
| **Reviewer** | An administrator performing conversation review. Same as administrator — no separate role. |
| **Archive Seat** | A monitored account's position in the archive. Determined at query time (no formal seat roster exists). |

### Feature Areas

| Term | Definition |
|------|------------|
| **Sync Worker** | systemd-timed script that pulls new encrypted messages from WeCom Archive API. |
| **Decryption Pipeline** | Script that decrypts pending messages using RSA private key, extracts searchable fields. |
| **Media Download Worker** | Separate systemd-timed script that downloads image media from WeCom for recently ingested messages. |
| **Conversation Review Console** | The primary admin UI — a three-column layout for browsing accounts/contacts, conversations, and message timelines. |
| **Reachability Audit** | System diagnostics feature that analyzes message deliverability patterns (aggregate statistics only, no message content). |

### Infrastructure

| Term | Definition |
|------|------------|
| **systemd timer** | Linux mechanism for scheduled task execution. Used for both the sync worker and media download worker. |
| **File Lock** | `fcntl.flock` acquired by workers before touching the database. Prevents concurrent runs regardless of trigger mechanism. |
| **Tenant Scope** | `tenant_id` column on every archive table. All queries filter by the session's tenant_id. |

### Data Model Concepts

| Term | Definition |
|------|------------|
| **seq** | WeCom's sequential message number used for cursor-based sync. Stored in `sync_states.last_seq`. |
| **msgid** | WeCom's stable, unique message identifier. Combined with `tenant_id` for uniqueness. |
| **roomid** | WeCom group chat room identifier. Null for 1:1 (direct) messages. |
| **sdkfileid** | File identifier in the WeCom SDK for media attachments. Used to download files. |
| **publickey_ver** | Key version number used for encryption. Enables key rotation without losing ability to decrypt old messages. |

---

*Part of the docs/ai/ set — maintained for AI agent onboarding.*
