---
kind: business_term
name: Business Glossary
category: business_term
scope:
    - '**'
---

### 租户
- Definition：A company/organization using the system. Each tenant has its own WeCom credentials, archive data, and admin users. The current MVP ships with one default tenant.
- Aliases：Tenant

### 存档
- Definition：The encrypted conversation data pulled from WeCom's Conversation Archive API. Not a backup — a compliance/audit record stored as `archive_messages` rows.
- Aliases：Archive

### 监控账号
- Definition：A WeCom account whose conversations are subject to archive review. Determined by two signals: (a) `staff_` prefix convention, (b) any admin user who also appears as a sender/recipient in the archive. No formal seat roster exists.
- Aliases：Monitored Account、Archive Seat

### 会话
- Definition：A grouping of messages between participants. Two types: direct (1:1) identified by a sorted pair of participant IDs, and group (multi-participant chat room) identified by `roomid`.
- Aliases：Conversation

### 消息
- Definition：A single WeCom conversation message — text, image, voice, video, file, etc. Stored as `archive_messages` rows with WeCom's stable `msgid` and sequential `seq` cursor.
- Aliases：Message

### 媒体
- Definition：File attachments in messages (images, voice recordings, videos, documents). Downloaded separately from the archive sync and stored via a pluggable storage provider (local disk or Qiniu Kodo).
- Aliases：Media

### 对话审查控制台
- Definition：The primary admin UI — a three-column server-rendered HTML + JS interface for browsing accounts/contacts, conversations, and message timelines with auto-refresh, search, and diagnostics.
- Aliases：Conversation Review Console、Review Console

### 同步工作器
- Definition：systemd-timed script that pulls new encrypted messages from WeCom Archive API on a schedule, acquires a file lock, and persists the sync cursor after each batch.
- Aliases：Sync Worker

### 解密流水线
- Definition：Script that decrypts pending messages using RSA private key, extracts searchable fields, and creates recipient rows from the message's `tolist`.
- Aliases：Decryption Pipeline

### 媒体下载工作器
- Definition：Separate systemd-timed script that downloads image media from WeCom for recently ingested messages, validates bytes via magic signatures, and updates `media_files` rows.
- Aliases：Media Download Worker

### 可达性审计
- Definition：System diagnostics feature that analyzes message deliverability patterns and produces aggregate statistics without exposing message content.
- Aliases：Reachability Audit

### 管理员
- Definition：A human who has authenticated via WeCom OAuth (or password fallback) and can access the Review Console. Stored in `admin_users`; there is no separate reviewer role.
- Aliases：Administrator、Reviewer

### 系统定时器
- Definition：Linux mechanism for scheduled task execution. Used for both the sync worker and media download worker, configured via systemd timer units versioned in the repo.
- Aliases：systemd timer

### 文件锁
- Definition：`fcntl.flock` acquired by workers before touching the database or shared resources. Prevents concurrent runs regardless of trigger mechanism (timer vs event).
- Aliases：File Lock

### 租户范围
- Definition：`tenant_id` column on every archive table. All queries filter by the session's `tenant_id`, ensuring multi-tenant isolation.
- Aliases：Tenant Scope
