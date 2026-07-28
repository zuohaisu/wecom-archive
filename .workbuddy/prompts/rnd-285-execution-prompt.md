# RND-285 开发执行提示词 — A3-2 邀请流程（令牌 + 邮件）

> 线性任务：`RND-285` `[BLOCKED: F0] A3-2 邀请流程（令牌 + 邮件）`
> 父 Epic：`RND-273` Epic · A3 用户管理
> 工程纪律：Agent **绝不 git commit/push**；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS。代码标识符加反引号。
> 标签：backend。估算 0.75w。

---

## 0. 任务目标（一句话）

实现「邀请合规/运营团队成员」的后端流程：`POST /api/admin/users/invite`（建一条 pending `AdminUser` + 发邀请邮件）与 `POST /api/admin/users/accept`（凭令牌设置密码、激活账号）。**不实现任何前端页面**（那是 D1 SSR 前端票，不在本票 scope）。

---

## 1. 关键工程判断（务必先读，避免返工）

### 1.1 令牌存在 `admin_users` 行上 —— 本票 **不需要新建表、不需要新建 Alembic migration**
- `RND-277`（F0-1）已在 `backend/app/db/models.py:199-201` 给 `AdminUser` 加了 `invite_token`(Text, nullable)、`invited_by`(FK→admin_users.id, nullable)、`invite_status`(Text, nullable) 三列，并在 `:155` 建了索引 `ix_admin_users_invite_token`。
- 当前迁移链 head = **`0018`**（`0018_password_reset_tokens.py`，RND-278，已 commit 到 main）。再建一个 `0019` 会与你无关的 RND-293 冲突，且重复 RND-277 的列。**所以本票绝对不要新增迁移、不要新增 `invitetokens` 表、不要改 `models.py`**。
- 验收靠「无 schema 变更 → `alembic check` 零漂移」来保证正确性。

### 1.2 鉴权依赖 `get_current_user` 已存在 —— 直接复用，不要自己造
- `backend/app/auth.py:319` 已实现 `get_current_user(session_id: Optional[str] = Cookie(...), db=Depends(get_db)) -> Tuple[AdminUser, str]`，返回 `(当前管理员, tenant_id)`。
- Invite 端点用 `Depends(get_current_user)` 取出发起人 + 其租户作用域。**租户隔离铁律**：被邀请人 `tenant_id` 必须等于发起人的 `tenant_id`，绝不接受请求体里的 tenant。
- 不要在 RND-285 里实现/修改任何 session、cookie、JWT 逻辑——那是 F0-2 的事。本票只「消费」`get_current_user`。
- Linear 上 `[BLOCKED: F0]` 指 F0 epic 整体就绪（登录端到端发 session）后才可上线，但**符号已可用、本票现在就能实现**。

### 1.3 邀请「先建 pending 行、接受时激活」—— 对 `wecom_user_id NOT NULL` 的处理
- `AdminUser.wecom_user_id` 是 `NOT NULL` 且与 `tenant_id` 有唯一约束 `uq_admin_users_tenant_wecom`（`:137`）。因此邀请时**必须**给一个 `wecom_user_id`。
- **主路径（WeCom 产品形态）**：邀请体带 `wecom_user_id`（企业微信员工 ID，必填）+ `role`（必填）+ 可选 `email`/`name`。直接用来建 pending 行。
- **回退路径（仅邮箱邀请、无 WeCom ID）**：若请求体未给 `wecom_user_id` 但给了 `email`，用确定性占位 `wecom_user_id = f"invited:{email.lower()}"`，并对同一占位已有 pending 行时返回 `409` 或重发既有 token（二选一，建议重发）。
- **不要**为绕过 NOT NULL 去改 `models.py` 或加迁移；用上面的占位约定即可。
- 关于 Linear 描述「接受流程建 AdminUser」的口径：本实现「邀请即建 `AdminUser`（状态 `disabled`，不可登录），接受即激活」。这与既有 `invite_token/invite_status` 列落在 `admin_users` 上的事实一致；若你方其实想要「接受时才建行 + 独立 `invitetokens` 表」，请先与 PM 确认再改方案（那会变成本票新增迁移，与 §1.1 冲突）。

### 1.4 `invite_status` / `status` 生命周期取值约定（Text，非枚举）
- `invite_status`：`pending`（已邀请未接受）→ `accepted`（已激活）；后续可扩展 `revoked`/`expired`（本票不强制实现）。
- `status`（账号状态枚举 `active`/`disabled`）：pending 行用 `disabled`（无密码、不可登录）；accept 后置 `active`。
- 令牌：`secrets.token_urlsafe(32)`（与 `app/auth.py:178,236` 一致），URL-safe 256-bit，碰撞概率可忽略；索引 `ix_admin_users_invite_token` 非唯一，靠随机性保证不冲突。
- **令牌过期（TTL）本票 OUT OF SCOPE**：`admin_users` 上没有 `invite_token_created_at` 列，无法服务端校验过期。若需要过期，是 RND-277 的 follow-up（加列+迁移），不在 RND-285。本票接受端点只校验「token 命中一条 pending 行」。

### 1.5 邮件：复用 `email.py` 范式 + 新增设置项
- `backend/app/email.py` 已有 `send_password_reset_email`（`:20`），含「无 SMTP → console 回退、绝不在日志泄露 token」的范式。新增 `send_invite_email(to_email, accept_link, locale="zh-CN")` 照此实现。
- `backend/app/settings.py` 的 `EmailSettings`（`:57`）已有 `reset_base_url`/`reset_token_ttl_hours`。**新增 `invite_base_url`（validation_alias="INVITE_BASE_URL"）** 镜像 `reset_base_url`；不要复用 reset 的 base url（语义不同）。
- accept 链接：`f"{invite_base_url.rstrip('/')}/admin/accept-invite?token={raw_token}"`（前端 `onboarding-invite.html` 页消费 `?token=`，见 planner L95）。前端页不在本票 scope。

### 1.6 端点加在已有 `routers/auth.py`，不加新 router 文件、不改 `main.py`
- `backend/app/main.py:94` 已 `include_router(auth_router)`。新增两个端点直接 append 到 `backend/app/routers/auth.py`（与现有 `password_forgot`/`password_reset` 同文件、同风格）。
- 导入约定：**函数内惰性 import** `from app.auth import get_current_user, hash_password`、 `from app.email import send_invite_email`（参照 `auth.py:248,281` 现有写法，避免循环依赖）。
- 端点成功返回 `JSONResponse({"ok": True})`（参照 `:275,292`）。

---

## 2. 精确落点（改哪些文件、加什么）

| 文件 | 改动 |
|---|---|
| `backend/app/routers/auth.py` | 新增 2 个端点 + 2 个 pydantic body（`_InviteBody` / `_AcceptBody`）；惰性 import `get_current_user`/`hash_password`/`send_invite_email` |
| `backend/app/email.py` | 新增 `send_invite_email(to_email, accept_link, locale="zh-CN") -> bool`，含 console 回退、不泄露 token |
| `backend/app/settings.py` | `EmailSettings` 新增 `invite_base_url: str = Field(default="", validation_alias="INVITE_BASE_URL")` |
| `backend/tests/test_rnd285_invite_flow.py` | **新增** 测试文件（见 §4） |
| ~~`backend/app/db/models.py`~~ | **不改**（列已存在） |
| ~~`backend/alembic/versions/*`~~ | **不新增迁移** |
| ~~`backend/app/routers/users.py` / `main.py`~~ | **不新增 router、不改 main**（避免与 RND-284/286 并行冲突） |

### 2.1 端点签名（写进 `routers/auth.py`）

```python
class _InviteBody(BaseModel):
    wecom_user_id: str | None = None   # 主路径必填（WeCom 员工 ID）
    email: str | None = None
    name: str | None = None
    role: str                          # 必填，须 ∈ {"owner","admin","compliance","legal","readonlyaudit"}

class _AcceptBody(BaseModel):
    token: str
    password: str
    name: str | None = None

@router.post("/api/admin/users/invite")
def invite_user(body: _InviteBody,
                current: Tuple[AdminUser, str] = Depends(get_current_user),
                db: Session = Depends(get_db)):
    admin_user, tenant_id = current
    # 1) 角色校验
    if body.role not in {"owner","admin","compliance","legal","readonlyaudit"}:
        raise HTTPException(status_code=400, detail="invalid_role")
    # 2) 解析 wecom_user_id（主路径用 body.wecom_user_id；回退用占位）
    wecom_user_id = (body.wecom_user_id or "").strip()
    if not wecom_user_id:
        if not body.email:
            raise HTTPException(status_code=400, detail="wecom_user_id_or_email_required")
        wecom_user_id = f"invited:{body.email.lower()}"
    # 3) 防重复 pending（同 tenant+wecom_user_id 已有 pending → 重发既有 token）
    existing = db.query(AdminUser).filter(
        AdminUser.tenant_id == tenant_id,
        AdminUser.wecom_user_id == wecom_user_id,
        AdminUser.invite_status == "pending",
    ).first()
    if existing is not None:
        raw_token = existing.invite_token
    else:
        raw_token = secrets.token_urlsafe(32)
        pending = AdminUser(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            wecom_user_id=wecom_user_id,
            email=(body.email or "").strip() or None,
            name=body.name,
            role=body.role,
            status="disabled",
            invite_status="pending",
            invite_token=raw_token,
            invited_by=admin_user.id,
        )
        db.add(pending)
    db.commit()
    # 4) 发邮件（惰性 import）
    from app.email import send_invite_email
    settings = get_email_settings()
    base = settings.invite_base_url or get_wecom_oauth_settings().admin_domain
    accept_link = f"{base.rstrip('/')}/admin/accept-invite?token={raw_token}"
    if body.email:
        send_invite_email(body.email, accept_link)
    return JSONResponse({"ok": True})

@router.post("/api/admin/users/accept")
def accept_invite(body: _AcceptBody, db: Session = Depends(get_db)):
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="weak_password")
    user = db.query(AdminUser).filter(
        AdminUser.invite_token == body.token,
        AdminUser.invite_status == "pending",
    ).first()
    if user is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")
    from app.auth import hash_password
    user.password_hash = hash_password(body.password)
    user.status = "active"
    user.invite_status = "accepted"
    if body.name:
        user.name = body.name
    db.commit()
    return JSONResponse({"ok": True})
```

> 注：`secrets`/`uuid` 已在 `auth.py` 顶部 import（`app/auth.py:28` 用了 `secrets`；`routers/auth.py` 顶部需确认 `import secrets`/`import uuid` 或就近补 import）。`get_email_settings`/`get_wecom_oauth_settings` 已在 `auth.py` 使用。

---

## 3. 实现清单（逐条打勾 `[x]`）

- [ ] 确认 `routers/auth.py` 顶部已 `import secrets` / `import uuid`（没有就补）。
- [ ] 在 `routers/auth.py` 新增 `_InviteBody` / `_AcceptBody` pydantic 模型。
- [ ] 新增 `invite_user` 端点：角色校验 → 解析 `wecom_user_id`（占位回退）→ 防重复 pending（命中则重发）→ `db.add` pending `AdminUser`(`status="disabled"`, `invite_status="pending"`, `invite_token`, `invited_by=admin_user.id`)→ `db.commit()` → `send_invite_email` → 返回 `{"ok": True}`。
- [ ] 新增 `accept_invite` 端点：密码强度校验(≥8) → 按 token 查 pending 行（无则 `400 invalid_or_expired_token`）→ `hash_password` → `status="active"` / `invite_status="accepted"` → `db.commit()` → 返回 `{"ok": True}`。
- [ ] `accept_invite` **不挂 `get_current_user`**（受邀人尚未登录，这是设计内的公开端点）。
- [ ] `invite_user` **挂 `Depends(get_current_user)`**，并用返回的 `tenant_id` 作为被邀请人租户；不读请求体里的 tenant。
- [ ] `email.py` 新增 `send_invite_email`，严格镜像 `send_password_reset_email`：无 SMTP 走 console（只记收件人，绝不在日志写 `accept_link`/`token`）；有 SMTP 走 `SMTP_SSL` 发送。文案用中文「您被邀请加入康冠时代企业微信会话存档……」。
- [ ] `settings.py` 的 `EmailSettings` 新增 `invite_base_url`（alias `INVITE_BASE_URL`）。
- [ ] **确认未新增任何 migration / 未改 `models.py` / 未新增 router 文件 / 未改 `main.py`**。
- [ ] 新增 `tests/test_rnd285_invite_flow.py`（见 §4）。
- [ ] 跑 `make verify` 全绿（含 `test_architecture_boundary.py`）。
- [ ] 跑 `alembic check`（或 `alembic upgrade head` + 比对）确认零漂移（本票无 schema 变更，应直接绿）。
- [ ] **不 commit**（交 QA）。

---

## 4. 测试（`tests/test_rnd285_invite_flow.py`，镜像 `test_rnd278_password_reset.py` 风格）

- 用 sqlite in-memory + `Base.metadata.create_all(tables=[Tenant.__table__, AdminUser.__table__])` 建表。
- `test_invite_creates_pending_user_and_sends_email`：构造 `get_current_user` 的 mock 返回值 `(inviter, tenant_id)`（用 `patch("app.routers.auth.get_current_user", return_value=(inviter, tenant_id))`），调用 `invite_user`，断言：DB 多一条 `AdminUser`，`status=="disabled"`、`invite_status=="pending"`、`invite_token` 非空、`invited_by==inviter.id`、`tenant_id` 正确；email 函数被调用且链接含 token。
- `test_duplicate_invite_resends_same_token`：同 tenant+wecom_user_id 二次邀请，断言 `invite_token` 不变、无新行。
- `test_accept_sets_password_and_activates`：先用 `invite_user` 造 pending，再调 `accept_invite({token, password="longenough"})`，断言 `password_hash` 被 `hash_password` 写入、`status=="active"`、`invite_status=="accepted"`；响应体不含 token。
- `test_accept_rejects_invalid_token`：`accept_invite({token:"bogus", password:"longenough"})` → `400 invalid_or_expired_token`。
- `test_accept_rejects_weak_password`：`accept_invite({token:<valid>, password:"short"})` → `400 weak_password`。
- `test_invite_requires_auth`：`patch("app.routers.auth.get_current_user", side_effect=HTTPException(401))`，调 `invite_user` → `401`。
- `test_console_email_does_not_log_token`：monkeypatch `get_email_settings` 返回 `smtp_host=""` 的 mock，调 `send_invite_email`，`caplog` 断言 `token` 不出现在日志。
- `test_invite_settings_use_invite_base_url_env`：`monkeypatch.setenv("INVITE_BASE_URL", "https://archive.example.test")` → `get_email_settings().invite_base_url == "https://archive.example.test"`。

---

## 5. 范围守门（红线，违反即判 Fail）

- [ ] **禁止**新增 Alembic migration / 新增 `invitetokens` 表 / 改 `models.py`（列已存在，见 §1.1）。
- [ ] **禁止**在 RND-285 实现或修改 session/cookie/JWT/登录逻辑（那是 F0-2，只用 `get_current_user`）。
- [ ] **禁止**新增前端 HTML/SSR 页面（D1 前端票，不在本票；只产出 accept 链接字符串）。
- [ ] **禁止**改 `role` / `status` 枚举定义（沿用 RND-277 既有枚举）。
- [ ] **禁止**改动既有 `password_forgot`/`password_reset` 端点或其它 router 行为（回归安全）。
- [ ] 不要为「令牌过期」加列/加迁移（OUT OF SCOPE，见 §1.4）。
- [ ] 不 commit。

---

## 6. 交付报告格式（完成后在对话里贴出）

```
## RND-285 实现报告
- 改动文件：backend/app/routers/auth.py, backend/app/email.py, backend/app/settings.py, backend/tests/test_rnd285_invite_flow.py
- 新增迁移：无（列已由 RND-277 提供）
- 端点：POST /api/admin/users/invite（Depends(get_current_user)）、POST /api/admin/users/accept（公开）
- 关键决策：令牌存 admin_users 行；wecom_user_id 主路径必填、回退占位 f"invited:{email}"；invite 建 disabled/pending 行，accept 置 active/accepted
- 邮件：send_invite_email + INVITE_BASE_URL 设置；console 回退不泄露 token
- 测试：N 个用例全绿
- make verify：PASS；alembic check：零漂移 PASS
- 未 commit（待 QA）
```

---

## 7. 路由判定（拿不准就停）

- 若 PM 明确要「接受时才建 `AdminUser` 行 + 独立 `invitetokens` 表」→ 这会变成新增迁移，与 §1.1 冲突，**先问用户/PM 再动手**，不要擅自加表。
- 若 `get_current_user` 签名与你预期不符（返回类型变了）→ 以 `app/auth.py:319` 实际签名为准，必要时问。
- 其它歧义：在报告里标注「假设」，不要静默猜。
