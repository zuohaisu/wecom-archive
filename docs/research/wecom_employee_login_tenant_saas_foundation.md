# RND-110 / RND-111 Research: WeCom Employee Login + Tenant-Aware SaaS Foundation

**Historical engineering research.**

> **Superseded as a current architecture reference (GitHub #92).** This
> pre-multi-tenant research describes phased internal/single-tenant assumptions
> that current code has passed. It remains useful only as historical rationale;
> use `docs/architecture/current-state.md` and executable sources for current
> architecture.

**Status:** Research complete; implementation-phase record, not current guidance.

---

## 1. Executive Recommendation

**Use WeCom self-built app OAuth (网页授权登录).**

| Approach | Decision | Rationale |
|---|---|---|
| WeCom self-built app OAuth (网页授权登录) | **Use this** ✅ | Standard for internal enterprise apps. Returns corp UserId. No user friction with `snsapi_base`. |
| Public WeChat login (微信开放平台) | **Do not use** ❌ | For public WeChat users, not enterprise employees. Wrong identity model. |
| WeCom Web Login component (企业微信Web登录) | **Do not use** ❌ | Designed for service-provider portals where any WeCom user logs in. Unnecessary complexity for internal admin console. |
| Username/password | **Do not use** ❌ | No identity verification. Adds credential storage risk. |

**Correct implementation order:**

```
Phase 1 → RND-111: Tenant foundation (data model, default tenant, backfill)
Phase 2 → RND-110: WeCom employee login (OAuth, session, API protection)
Phase 3 → Future SaaS: Multi-tenant configs, per-tenant workers, onboarding
```

Tenant foundation (RND-111) must come first because login sessions and API guards need `tenant_id`. Building login first would require retrofitting tenant_id into sessions later.

Current system can remain single-company for MVP, but the data model and session architecture must be tenant-aware now.

---

## 2. WeCom Login Flow

### 2.1 Authorization URL

```
GET https://open.weixin.qq.com/connect/oauth2/authorize
  ?appid=CORP_ID
  &redirect_uri=URL_ENCODED_CALLBACK_URL
  &response_type=code
  &scope=snsapi_base
  &state=RANDOM_STATE_TOKEN
  &agentid=AGENT_ID
  #wechat_redirect
```

| Parameter | Required | Value |
|---|---|---|
| `appid` | Yes | Enterprise CorpID (e.g., `ww1234567890abcdef`) — NOT a public WeChat appid |
| `redirect_uri` | Yes | URL-encoded callback URL. Domain must match WeCom Admin trusted domain. |
| `response_type` | Yes | Always `code` |
| `scope` | Yes | `snsapi_base` — silent authorization, returns UserId. No user prompt. |
| `state` | Yes | Cryptographically random token (32+ chars). Single-use. Server-side stored. 5-min TTL. |
| `agentid` | Yes | Self-built app Agent ID from WeCom Admin (integer string, e.g. `1000002`) |
| `#wechat_redirect` | Yes | Required anchor suffix. Do not include in URL-encoded redirect_uri. Append after. |

### 2.2 Scope Decision

| Scope | User Prompt | Returns |
|---|---|---|
| `snsapi_base` | **None** (silent) | `UserId`, `DeviceId` |
| `snsapi_privateinfo` | User must tap authorize | Above + `user_ticket` for sensitive info (phone, email) |

**Use `snsapi_base`** for the admin console MVP. It provides sufficient identity (UserId) with zero user friction.

### 2.3 Callback Flow

```
1. WeCom redirects to: GET {redirect_uri}?code=AUTH_CODE&state=STATE_TOKEN
2. Backend validates state matches stored token. Rejects on mismatch (CSRF).
3. Backend gets access_token (from cache or fetches via gettoken API).
4. Backend calls getuserinfo?access_token=TOKEN&code=AUTH_CODE.
5. Backend receives UserId (e.g., "ZhangSan").
6. Backend optionally calls user/get to confirm user is active in corp.
7. Backend resolves tenant from corp_id → tenant_wecom_configs lookup.
8. Backend upserts admin_users row (tenant_id, wecom_user_id).
9. Backend creates admin_sessions row with session_id, tenant_id, expires_at.
10. Backend sets HttpOnly Secure SameSite=Lax cookie.
11. Backend redirects to frontend admin URL. Never to user-controlled URL.
```

### 2.4 QR Code Behavior

- **Desktop browser**: WeCom displays a QR code. User opens WeCom mobile app, scans QR, confirms authorization. Desktop browser is then redirected to callback.
- **WeCom built-in browser (mobile)**: Authorization proceeds directly without QR scan.
- This flow is transparent to the backend. The same callback endpoint handles both cases.

### 2.5 API Endpoints Called by Backend

**Get access_token:**
```
GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=CORP_ID&corpsecret=APP_SECRET
→ { "errcode": 0, "access_token": "...", "expires_in": 7200 }
```
- Token valid 7200 seconds (2 hours).
- **Must cache server-side.** Max ~2000 calls/day per secret.
- Cache key must include corp_id (future multi-tenant safety).

**Get user identity:**
```
GET https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo?access_token=TOKEN&code=AUTH_CODE
→ { "errcode": 0, "UserId": "ZhangSan", "DeviceId": "..." }
```
- No CorpId in response. Corp verification is implicit: the access_token is tied to a specific corp.
- Optional additional verification: call `user/get` to confirm user exists and is active.

**Get user info (optional verification):**
```
GET https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token=TOKEN&userid=USER_ID
→ { "errcode": 0, "userid": "...", "name": "张三", "department": [...], "status": 1 }
```
- `status`: 1=active, 2=disabled, 4=not-activated, 5=left the enterprise. There is
  no `enable` field in the real response (RND-225 correction) — an earlier draft of
  this doc invented one, and the implementation copied it, which made every real
  active employee fail the login check in production.


---

## 3. Required WeCom Admin Configuration

### 3.1 Credentials to Collect

| Credential | Source in WeCom Admin | Example |
|---|---|---|
| CorpID (企业ID) | My Enterprise → Enterprise Info → CorpID | `ww1234567890abcdef` |
| Agent ID (AgentId) | App Management → [Your App] → AgentId | `1000002` |
| App Secret (Secret) | App Management → [Your App] → Show Secret | Random 64-char string |

### 3.2 Trusted Domain Configuration

**Location:** WeCom Admin → App Management → [Your App] → Developer Settings → Web Authorization & JS-SDK Domain (网页授权及JS-SDK域名)

**Value:** Your admin console domain only — no protocol, no path, no port.

| Item | Example |
|---|---|
| Domain to register | `admin.yourcompany.com` |
| NOT | `https://admin.yourcompany.com` |
| NOT | `admin.yourcompany.com:443` |
| NOT | `admin.yourcompany.com/callback` |

### 3.3 Callback URL Constraints

- Must use **HTTPS** in production. WeCom rejects HTTP in production redirects.
- **No port numbers** in the redirect_uri domain.
- Domain must **exactly match** the trusted domain registered in WeCom Admin.
- Subdomain mismatch (registered `example.com`, callback at `admin.example.com`) will cause failure.
- This is the same domain-verification category as the RND-108 callback domain issue.

### 3.4 Development / Local Testing

- Localhost OAuth testing requires either:
  - A reverse proxy (nginx/Caddy) with a real domain pointing to localhost, **OR**
  - A staging deployment with a real domain and HTTPS
- WeCom provides a test environment option in some configurations. Verify in WeCom Admin.

---

## 4. Backend Endpoint Design

### 4.1 New Auth Endpoints

| Method | Path | Auth Required | Purpose |
|---|---|---|---|
| `GET` | `/admin/login` | None | Login page / admin entry route for unauthenticated users. Renders a login page with "Login with WeCom" button, or redirects to `/api/auth/wecom/login`. Must not expose archived data. |
| `GET` | `/api/auth/wecom/login` | None | Construct and redirect to WeCom OAuth URL |
| `GET` | `/api/auth/wecom/callback` | None | WeCom OAuth callback. Exchange code → session. |
| `GET` | `/api/auth/me` | Session | Return current user + tenant context |
| `POST` | `/api/auth/logout` | Session | Delete session, clear cookie |

### 4.2 Protected Admin Resources (Phase 2)

| Method | Path | Protection |
|---|---|---|
| `GET` | `/admin/conversations` | Session required. Serves admin SPA. |
| `GET` | `/api/monitored-accounts` | `Depends(get_current_user)`. Filter by `tenant_id` from session. |
| `GET/POST` | `/api/contacts` | `Depends(get_current_user)`. Filter by `tenant_id` from session. |
| `GET` | `/api/conversations` | `Depends(get_current_user)`. Filter by `tenant_id` from session. |
| `GET` | `/api/conversations/{id}/messages` | `Depends(get_current_user)`. Scoped by `tenant_id`. |

### 4.3 Public Endpoints (No Session)

| Method | Path | Rationale |
|---|---|---|
| `GET` | `/admin/login` | Login page / admin entry route for unauthenticated users. Public. Must not expose archived data. |
| `GET` | `/health` | Monitoring, systemd health checks, load balancer probes. |
| `POST` | `/api/wecom/archive/events` | Called by WeCom servers server-to-server. No user session. |
| `GET` | `/api/auth/wecom/login` | Anonymous entry point for login flow. |
| `GET` | `/api/auth/wecom/callback` | Anonymous callback. OAuth state validation handles security. |
| `GET` | `/static/*` | Static assets required for login page before authentication. |

### 4.4 Critical Security Notes

- `/api/wecom/archive/events` **MUST NOT** be protected by employee session auth. It is called by WeCom server-side infrastructure.
- Admin API protection must be **server-side** (`Depends(get_current_user)`), not merely hidden by frontend routing. Unauthenticated requests must receive `401`.
- The admin HTML page must also check session server-side. If unauthenticated, redirect to login or return 401.

### 4.5 FastAPI Dependency Design

```
get_current_user()
  → reads session_id from cookie
  → looks up admin_sessions WHERE id = sid AND expires_at > now() AND is_revoked = 0
  → returns (admin_user_row, tenant_id)
  → raises HTTPException(401) if invalid
```

Protected endpoint pattern:
```python
@router.get("/api/conversations")
async def list_conversations(
    session: tuple = Depends(get_current_user)
):
    admin_user, tenant_id = session
    return query("SELECT * FROM conversations WHERE tenant_id = ?", [tenant_id])
```


---

## 5. Tenant-Aware Data Model

### 5.1 New Tables

```sql
-- Core tenant entity
CREATE TABLE tenants (
    id         TEXT PRIMARY KEY,  -- UUID
    name       TEXT NOT NULL,     -- Display name, e.g. "Acme Corp"
    slug       TEXT NOT NULL UNIQUE,
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Per-tenant WeCom credentials
CREATE TABLE tenant_wecom_configs (
    id              TEXT PRIMARY KEY,  -- UUID
    tenant_id       TEXT NOT NULL UNIQUE REFERENCES tenants(id),
    corp_id         TEXT NOT NULL,     -- WeCom CorpID
    agent_id        TEXT NOT NULL,     -- WeCom Agent ID
    app_secret      TEXT NOT NULL,     -- MVP: plaintext. Phase 3: encrypted.
    callback_domain TEXT NOT NULL,     -- Trusted domain for OAuth
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Admin users (WeCom employees who have logged in)
CREATE TABLE admin_users (
    id            TEXT PRIMARY KEY,  -- UUID
    tenant_id     TEXT NOT NULL REFERENCES tenants(id),
    wecom_user_id TEXT NOT NULL,     -- WeCom UserId
    name          TEXT,              -- Display name from WeCom
    avatar_url    TEXT,
    last_login_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(tenant_id, wecom_user_id)
);

-- Active login sessions
CREATE TABLE admin_sessions (
    id            TEXT PRIMARY KEY,  -- UUID (session_id in cookie)
    admin_user_id TEXT NOT NULL REFERENCES admin_users(id),
    tenant_id     TEXT NOT NULL REFERENCES tenants(id),
    wecom_user_id TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,     -- e.g., created_at + 8 hours
    is_revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_admin_sessions_expires ON admin_sessions(expires_at);
```

### 5.2 Existing Tables — Add tenant_id

The current repo may not have persistent `monitored_accounts` or `conversations` tables. RND-111 should tenant-scope existing persisted archive tables first and should not create unnecessary persistent tables unless required by implementation.

**Confirmed persisted tables** (add `tenant_id`):

```sql
ALTER TABLE archive_messages           ADD COLUMN tenant_id TEXT REFERENCES tenants(id);
ALTER TABLE archive_message_recipients ADD COLUMN tenant_id TEXT REFERENCES tenants(id);
ALTER TABLE sync_states                ADD COLUMN tenant_id TEXT REFERENCES tenants(id);
ALTER TABLE contacts                   ADD COLUMN tenant_id TEXT REFERENCES tenants(id);
```

**Persistent tables if present, or derived API models if not present:**

- `monitored_accounts` — if currently configuration-derived rather than a persisted table, any future persisted version must include `tenant_id`.
- `conversations` — if conversations remains derived from archive queries rather than stored as a table, all source archive queries used to derive conversations must be tenant-scoped.

**Rule for derived API objects:** tenant scoping must happen at the source query level (e.g., `WHERE archive_messages.tenant_id = ?`).

### 5.3 Unique Constraints — Tenant-Scoped

| Table (persisted or future-persisted) | Constraint |
|---|---|
| `archive_messages` | Index on `(tenant_id, msg_id)`; uniqueness per message dedup strategy |
| `sync_states` | `UNIQUE(tenant_id, seq_type)` or equivalent state key |
| `contacts` | `UNIQUE(tenant_id, contact_id)` |
| `admin_users` | `UNIQUE(tenant_id, wecom_user_id)` |
| `monitored_accounts` (if persisted) | `UNIQUE(tenant_id, account_id)` |
| `conversations` (if persisted) | `UNIQUE(tenant_id, conversation_id)` |


---

## 6. Migration Strategy

### 6.1 Phase 1 (RND-111): Bootstrap Default Tenant

1. Run migration to create `tenants`, `tenant_wecom_configs`, `admin_users`, `admin_sessions` tables.
2. Add nullable `tenant_id` columns to all archive/contact tables.
3. Insert default tenant:

```sql
INSERT INTO tenants (id, name, slug, created_at, updated_at)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default', 'default', datetime('now'), datetime('now'));

INSERT INTO tenant_wecom_configs (id, tenant_id, corp_id, agent_id, app_secret, callback_domain, created_at, updated_at)
VALUES (
    'config-uuid',
    '00000000-0000-0000-0000-000000000001',
    '[from .env WECOM_CORP_ID]',
    '[from .env WECOM_AGENT_ID]',
    '[from .env WECOM_OAUTH_SECRET]',
    '[from .env ADMIN_DOMAIN]',
    datetime('now'),
    datetime('now')
);
```

4. Backfill all existing data:

```sql
UPDATE archive_messages           SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE archive_message_recipients SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE sync_states                SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
UPDATE contacts                   SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
```

5. After backfill, change `tenant_id` columns to `NOT NULL`.

### Conditional Backfill for Optional / Derived Tables

`monitored_accounts` and `conversations` may not be persisted tables in the current repo — they may be config-derived or API-derived objects. RND-111 should not create these tables solely for tenant backfill.

Only run the following UPDATE statements if the corresponding table exists as a persisted table:

```sql
-- Run only if monitored_accounts is a persisted table
UPDATE monitored_accounts SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;

-- Run only if conversations is a persisted table
UPDATE conversations SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
```

If `conversations` remains derived from archive tables, tenant scoping must happen at the source archive query level (e.g., `WHERE archive_messages.tenant_id = ?`). If `monitored_accounts` is config-derived, do not create a table only for tenant backfill — any future persisted version must include `tenant_id`.

### 6.2 Sync Worker — Phase 1

- Worker reads its corp_id from `.env` as before.
- Worker resolves default `tenant_id` from `tenant_wecom_configs WHERE corp_id = ?`.
- Worker uses this `tenant_id` when writing sync records and archive data.
- No change to worker scheduling or timer mechanism.

### 6.3 Sync Worker — Phase 3 (Future)

- Worker queries all active `tenant_wecom_configs`.
- For each config, worker fetches access_token (keyed by corp_id), reads per-tenant sync state, and syncs independently.
- Worker continues via systemd timer, now iterating tenant configs each cycle.

### 6.4 Migration Preflight and Rollback Safety

- Run schema review against production DB before executing migration.
- Take a full database backup or snapshot before migration.
- Verify row counts before and after backfill (each table: `SELECT count(*) WHERE tenant_id IS NULL` before, `SELECT count(*) WHERE tenant_id = 'default-tenant-uuid'` after).
- Do not drop or recreate production data columns or tables.
- Make migrations idempotent where practical (e.g., `IF NOT EXISTS`, `ADD COLUMN ... IF NOT EXISTS` equivalents, or guard conditions).
- If migration fails before completion, rollback or restore from backup immediately.
- Validate current single-company admin console still works after migration.
- Verify sync worker continues to write archive records successfully post-migration.


---

## 7. Session and Security Design

### 7.1 Session Structure

Each session carries:
- `session_id`: UUID v4, stored as cookie value and DB primary key
- `admin_user_id`: FK to admin_users
- `tenant_id`: FK to tenants — the **authorization scope** for all data access
- `wecom_user_id`: For audit trail, redundant with admin_users join
- `expires_at`: After which session is invalid

### 7.2 Cookie Settings

| Setting | Value | Rationale |
|---|---|---|
| Name | `__Host-session_id` (if single origin) or `session_id` | `__Host-` prefix enforces Secure + Path=/ + no subdomain |
| `HttpOnly` | `true` | Prevents JS access (XSS protection) |
| `Secure` | `true` | HTTPS only (set to `false` for localhost dev only) |
| `SameSite` | `Lax` | Allows GET from WeCom OAuth redirect. Blocks CSRF on mutating requests. |
| `Path` | `/` | Available to all API routes |
| `Max-Age` | `28800` (8 hours) | Workday session. Forces re-login next day. |

### 7.3 CSRF / State Parameter

- `state` for OAuth: `secrets.token_urlsafe(32)`, store server-side (in-memory or DB), 5-minute TTL, single-use (delete after verification), **not** in a cookie.
- `SameSite=Lax` on session cookie provides defense-in-depth for mutating endpoints.

### 7.4 Open Redirect Prevention

- Callback endpoint redirects **only** to a hardcoded/configured frontend URL (e.g., `FRONTEND_URL` from `.env` or `tenant_wecom_configs.callback_domain`).
- Never redirect based on a user-controlled query parameter.
- Never redirect to `state`, `redirect_uri`, or any value from the OAuth callback GET params.

### 7.5 Cross-Tenant Data Access Prevention

- `tenant_id` in the session is the **sole source** of tenant scope for all admin queries.
- Every admin API query must include `WHERE tenant_id = ?` bound to session's tenant_id.
- Never accept `tenant_id` as a user-supplied API parameter.
- Auth middleware resolves tenant_id per-request — no cross-request caching that could leak between users.
- Code-review rule: **Every new admin query must have `WHERE tenant_id = current_tenant_id`.**

---

## 8. Logging / Secret Safety

### 8.1 Must NOT Be Logged

| Value | Risk |
|---|---|
| OAuth `code` (full value) | Token theft (short-lived but sensitive) |
| `access_token` | Session hijack on WeCom APIs |
| `app_secret` / CorpSecret | Permanent credential compromise |
| Session token / cookie value | Session hijacking |
| `state` parameter (full value) | CSRF token exposure |
| WeCom callback token / EncodingAESKey | Archive event decryption key compromise |
| Archive secret / private key | Decryption capability exposure |
| Decrypted message payload | Customer data exposure |
| Encrypted payload | Could assist brute-force attacks |
| Message content / customer data | Privacy / compliance violation |
| Any bearer token in URL query strings | Access log exposure |

### 8.2 Allowed / Sanitized Logs

- `login started` (no parameters)
- `callback received` (no code value logged)
- `auth success for user={wecom_user_id}` (user identifier acceptable if team agrees)
- `auth failed: {reason_category}` (e.g., "invalid_state", "corp_mismatch", "user_inactive")
- `session created` (no token logged)
- `session expired / revoked`
- `protected route denied: 401 (no session)`
- `protected route denied: 403 (wrong tenant)` — indicates potential attack or bug


---

## 9. Risks / Blockers

| Risk | Severity | Mitigation |
|---|---|---|
| **WeCom domain verification blocks production login** | High | Ensure trusted domain in WeCom Admin exactly matches deployed domain. Test on staging with real domain first. (Same category as RND-108.) |
| **Local dev cannot fully test OAuth** | Medium | Requires HTTPS + real domain. Use nginx reverse proxy + Let's Encrypt on a dev server, or a staging environment. |
| **Access token cache collision between tenants** | Medium | Cache key must include `corp_id`, not just `"access_token"`. Implement from Phase 1 even with single tenant. |
| **App secret plaintext in DB** | Medium | Acceptable in Phase 1 (internal deployment, single tenant). Phase 3: encrypt with Fernet or Vault/KMS. |
| **Cross-tenant data leakage** | High | **Primary architectural risk.** Mitigated by: session carries tenant_id, all queries scope by tenant_id, never accept tenant_id from user input, code review enforcement. |
| **Session table unbounded growth** | Low | Scheduled cleanup: `DELETE FROM admin_sessions WHERE expires_at < datetime('now', '-1 day')`. |
| **WeCom API rate limits** | Low | gettoken limited to ~2000 calls/day per secret. Proper caching avoids this. getuserinfo rate limits are generous. |

---

## 10. Implementation Plan

### Phase 1 — RND-111: Tenant Foundation

**Goal:** Data model is tenant-aware. Current single-company deployment still works.

| Step | Task |
|---|---|
| 1a | Create migration: `tenants`, `tenant_wecom_configs`, `admin_users`, `admin_sessions` tables |
| 1b | Add nullable `tenant_id` columns to `archive_messages`, `archive_message_recipients`, `sync_states`, `monitored_accounts`, `contacts`, `conversations` |
| 1c | Add tenant-scoped unique constraints |
| 1d | Bootstrap default tenant from `.env` values |
| 1e | Backfill all existing records with default tenant_id |
| 1f | Change tenant_id columns to NOT NULL after backfill |
| 1g | Verify current archive sync, admin console display, and `/health` still function |

### Phase 2 — RND-110: WeCom Employee Login

**Goal:** Admin console is protected. Only authenticated employees of the correct corp can access.

| Step | Task |
|---|---|
| 2a | Implement `GET /api/auth/wecom/login` — redirect to WeCom OAuth URL |
| 2b | Implement `GET /api/auth/wecom/callback` — code exchange, user verification, session creation |
| 2c | Implement access_token cache (keyed by corp_id, LRU or time-based, 7000s TTL) |
| 2d | Implement `get_current_user` FastAPI dependency (session validation, returns user+tenant) |
| 2e | Apply `Depends(get_current_user)` to all admin API endpoints |
| 2f | Add `WHERE tenant_id = ?` scoping to all admin queries |
| 2g | Implement `POST /api/auth/logout` |
| 2h | Implement `GET /api/auth/me` |
| 2i | Protect `/admin/conversations` page with session check |
| 2j | Ensure `/health`, `/api/wecom/archive/events`, and static assets remain public |
| 2k | Update frontend: login button, session detection via `/api/auth/me`, logout button |

### Phase 3 — Future SaaS Multi-Tenant

**Goal:** Support multiple companies on the same deployment.

| Step | Task |
|---|---|
| 3a | Encrypt `app_secret` in `tenant_wecom_configs` (Fernet with key from `.env`) |
| 3b | Refactor sync worker to iterate active `tenant_wecom_configs` |
| 3c | Per-tenant sync states |
| 3d | Login callback resolves tenant from corp_id in DB |
| 3e | Tenant onboarding flow (admin UI for adding tenant config) |
| 3f | Vault/KMS integration for secrets |
| 3g | Billing/subscription if needed |
| 3h | Per-tenant rate limiting |

---

## 11. Source Links

Official WeCom developer documentation (Chinese):

| Document | URL |
|---|---|
| Web OAuth — Getting Started (网页授权登录 开始开发) | https://developer.work.weixin.qq.com/document/path/91335 |
| Construct OAuth Authorization URL (构造网页授权链接) | https://developer.work.weixin.qq.com/document/path/91022 |
| Get User Identity (获取访问用户身份) | https://developer.work.weixin.qq.com/document/path/91023 |
| Get Access Token (获取access_token) | https://developer.work.weixin.qq.com/document/path/91039 |
| Read Member Info (读取成员) | https://developer.work.weixin.qq.com/document/path/90196 |
| Web Login Component (企业微信Web登录) — NOT recommended for this MVP | https://developer.work.weixin.qq.com/document/path/98152 |

---

*Document maintained for RND-110 and RND-111. Updated 2026-06-28.*

