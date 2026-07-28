# RND-278 开发 agent 执行提示词 —— F0-3 password_reset_tokens 表 + 邮箱找回流程

> 面向开发 agent（单人端到端实现 RND-278）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS（不引 React）。

## 一、任务（一句话）

为管理员账号体系新增「邮箱找回密码」能力：新建 `password_reset_tokens` 表、提供标准库实现的邮件发送模块（本项目首个邮件能力，作为 F0-3 依赖一并自建）、落地 `/api/auth/password/forgot` + `/api/auth/password/reset` 两个端点、补齐 SSR 的「忘记密码 / 重置密码」页面并接上登录页的「忘记密码」链接。**不实现短信、不实现绑手机**（Non-goals）。

## 二、前置依赖（开工前必查，两项任一不满足 → 停下并报告）

本任务 **BLOCKED on F0-1**：

1. **F0-1 已落地**（RND-277 `AdminUser` 扩展 + RND-276 per-user 密码鉴权）：
   ```bash
   cd backend
   python -c "from app.db import models; c=[x.name for x in models.AdminUser.__table__.columns]; assert {'password_hash','email','role','status'} <= set(c), c; print('F0-1 OK', c)"
   python -c "from app.auth import verify_password; print('verify_password OK')"
   ```
   断言失败 → **停下报告**（依赖未落地，禁止自行改模型/加迁移/改鉴权；那属于 F0-1/F0-2）。
2. **邮件发送能力 = 本票自建**（Linear 依赖项「邮件发送能力」无独立 ticket）。本票内创建 `backend/app/email.py` + `EmailSettings`，见第五·三节。

> ⚠️ **Alembic 编号重要提示**：当前工作树 head 已是 `0016_media_download_attempts.py`。
> RND-277 的执行提示词写的是「新建 0016 / down_revision=0015」，但 `0016` 在树中已被媒体下载迁移占用，
> 因此 F0-1 真正落地时其迁移应为 `0017`（`down_revision="0016"`）。**本票不要硬编码 revision 号**：
> 实现时用 `cd backend && alembic heads` 读取真实 head，将新迁移 `down_revision` 设为该 head，
> 文件序号顺延（F0-1 落地后 head ≥ 0017，本票即 0018；以运行时 `alembic heads` 为准）。

## 三、决策背景（已全部拍板，不要再问）

来源：Epic `RND-263`（F0 账号体系重构）子任务 F0-3 + 现有账号体系研究 + RND-277/RND-276 已定方案。

- **复用现有 PBKDF2**：`app/auth.py:verify_password`（PBKDF2-HMAC-SHA256 / 260000 iters）即重置后写 `password_hash` 的函数；重置不改存储格式（`pbkdf2:sha256:260000:<salt>:<hash>`）。不引入 argon2 等新依赖。
- **复用 `verify_password` 的哈希路径**：重置成功时 `user.password_hash = hash_password(new_password)`。
- **登录标识 = `email`，且 `email` 非唯一**（RND-277 已定：跨租户唯一无意义，F0-3 不加唯一约束）。因此找回/重置查找在 **default 租户内**按 `func.lower(email)` 匹配，与 RND-276 登录语义一致（`.first()`）。
- **Non-goals**：不做短信；不做绑手机。令牌仅经邮件发送。
- **安全基线（对齐项目 SF-1 数据最小化纪律）**：`password_reset_tokens.token` 列**只存令牌的 SHA-256 哈希**；原始随机令牌只出现在邮件链接与用户浏览器 URL 中，永不入 DB、永不入任何响应体/日志。详见第五·二节。
- **零枚举（anti-enumeration）**：`/forgot` 无论邮箱是否存在/是否激活/是否 per-user 用户，统一返回 `{"ok": true}`；仅在确实存在「active 且含 email 且含 password_hash」的用户时才真正建令牌+发信，否则静默跳过（不报错、不泄露）。
- **SMTP 缺失 fail-closed**：`EmailSettings` 无 host/from 时走「console 传输」（仅 `logger.warning` 打印链接，不真正发信），仍对请求方返回 `{"ok": true}`（避免枚举）；真实发信在配置齐全时进行。这同时让测试可在无 SMTP 服务下确定性断言「邮件已生成」。
- **工程纪律**：Agent 不 git commit/push；代码标识符加反引号；架构冻结 D1（SSR+原生JS，不引 React）。

## 四、项目现状（精确落点）

### 4.1 模型与迁移
- `backend/app/db/models.py`：`AdminUser`（`L132-173`，F0-1 后含 `email`/`password_hash`/`role`/`status`）；`AdminSession`（`L176-201`）。新增 `PasswordResetToken` 加在此文件，`AdminSession` 之后。
- 约定：PK 用 `String(36)`（UUID 字符串）；时间列 `DateTime(timezone=True)` + `server_default=func.now()`；FK 用字符串 `"table.col"`；索引名 `ix_<table>_<col>`，唯一约束 `uq_<table>_<cols>`。见 `AdminSession` 写法。
- 迁移目录 `backend/alembic/versions/`：当前 head = `0016_media_download_attempts.py`。新迁移 `down_revision` = 运行时 `alembic heads`（见第二节提示）。
- 现有模型导入：`from sqlalchemy import ... Boolean, Column, DateTime, Enum, ForeignKey, String, Text, func`（`L7` 区域）——新增 `PasswordResetToken` 无需新增 import（沿用既有）。

### 4.2 既有 helper（复用，不改语义）
- `backend/app/auth.py`：`hash_password`（`L106-120`）、`verify_password`（`L123-143`）、`SESSION_COOKIE`（`L44`）、`get_auth_mode`（`L89-103`）。
- `backend/app/settings.py`：`AuthSettings`（`L44-52`）为范式；新增 `EmailSettings` 参照它（无缓存、每调用新建实例）。

### 4.3 路由落点（`backend/app/routers/auth.py`）
- 模块顶部已 `from app.auth import verify_password` 等（L38-50）；`from app.db.models import AdminSession, AdminUser, Tenant, TenantWecomConfig`（L51）；`from app.settings import get_auth_settings, get_wecom_oauth_settings`（L54）；`from app.web import render_template`（L55）。
- 登录页构造 `_login_page`（L76-172）；登录页路由 `/admin/login`（L175-209）；密码登录路由 `password_login`（L222-326，RND-276 已改 per-user）。
- **要改写**：`auth.py:114` 的「忘记密码？（即将上线）」惰性 `<span>` → 真实链接到 `/admin/forgot-password`。
- **新增**：`POST /api/auth/password/forgot`、`POST /api/auth/password/reset`、`GET /admin/forgot-password`、`GET /admin/reset-password`。本 router 无前缀，路径写全（与现有 `/api/auth/password/login` 一致）。

### 4.4 前端模板
- `backend/app/web/templates/login.html`：`__LOGIN_BODY__` 注入点（L51）；使用 `styles.css` 设计系统（L10）；含 i18n 引导脚本与 `applyI18n()`（L64-107）。新页面须复用同款外壳（aside 营销文案 + `.auth-card` + `styles.css` + i18n 引导）。
- 设计稿参考（仅视觉，不复制实现）：`design/ui-v1/pages/forgot-password.html`（三步流）。

### 4.5 i18n
- `backend/app/assets/i18n.js`：`login.*` 命名空间在 zh-CN 块约 `L214-242`，另含 zh-TW、en 块（同结构）。新增键加入**每个** locale 块，格式严格对齐现有 `login.*` 键（字符串键 → 字符串值）。

### 4.6 架构边界（CI 强制，`backend/tests/test_architecture_boundary.py`）
- `app.email` 是新 flat service 模块 → **必须**加入该测试的 `_FLAT_SERVICE_MODULES` 列表（`L70-93`），否则 CI 失败。
- `email.py` 是叶子服务：不得 `import app.routers.*` 或 `app.main`。令牌生成/校验 helper 放 `app/auth.py`（已在列表内）或 `app/email.py`，HTTP 端点放 `routers/auth.py`，**严禁**在 `app/main.py` 加任何路由。

## 五、实现步骤（GREEN，最小变更）

### 步骤 1 — 新增 `PasswordResetToken` 模型（`backend/app/db/models.py`，`AdminSession` 之后）
```python
# —— RND-278 (F0-3) 密码重置令牌 ——
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(String(36), primary_key=True)            # UUID 字符串
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False, index=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    # 仅存原始令牌的 SHA-256 哈希（hex）；原始令牌只存在于邮件链接/浏览器 URL，不入 DB
    token = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

### 步骤 2 — 新增邮件模块 `backend/app/email.py`（flat service）
```python
"""Password-reset email delivery (RND-278 / F0-3).

Stdlib-only (smtplib + email) to avoid a new dependency. When SMTP host/from
is unset, falls back to a console transport that logs the reset link — this
keeps the forgot endpoint enumeration-safe AND lets tests assert "email sent"
without a real SMTP server.
"""
from __future__ import annotations
import hashlib, logging, smtplib, ssl
from email.message import EmailMessage
from app.settings import get_email_settings

logger = logging.getLogger(__name__)

def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def send_password_reset_email(to_email: str, reset_link: str, locale: str = "zh-CN") -> bool:
    s = get_email_settings()
    subject, body = _render_reset_email(reset_link, locale)
    if not s.smtp_host or not s.smtp_from:
        logger.warning("[email-console] password reset -> %s : %s", to_email, reset_link)
        return True
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    # 默认 465 SSL；smtp_port 可覆盖。失败仅记录，不让调用方崩溃（零枚举优先）。
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(s.smtp_host, int(s.smtp_port or 465), context=ctx) as smtp:
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return True
    except Exception:  # noqa: BLE001 — 邮件失败不泄露给用户，记录后返回
        logger.exception("send_password_reset_email failed for %s", to_email)
        return False

def _render_reset_email(reset_link: str, locale: str) -> tuple[str, str]:
    # zh-CN 为主；同文件 i18n 文案风格。链接为唯一动作。
    subject = "重置您的康冠时代会话存档密码"
    body = (
        "我们收到了您的密码重置请求。\n\n"
        f"请点击以下链接重置密码（1 小时内有效，且仅可使用一次）：\n{reset_link}\n\n"
        "如果您没有请求重置密码，请忽略此邮件，链接将自动失效。"
    )
    return subject, body
```
> 把 `app.email` 加入 `backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（L70-93）。

### 步骤 3 — 新增 `EmailSettings`（`backend/app/settings.py`，紧跟 `AuthSettings` 之后）
```python
class EmailSettings(BaseSettings):
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    reset_base_url: str = ""        # 重置链接基址；为空时回退 admin_domain
    reset_token_ttl_hours: str = "1"

def get_email_settings() -> EmailSettings:
    return EmailSettings()
```
> `reset_base_url` 为空时，在端点内用 `get_wecom_oauth_settings().admin_domain` 作基址（`.env.example` 已有 `admin_domain`）。
> `.env.example` 见第六·三节「B 层纪律」提示——新增 SMTP 段是新增配置的标准做法，但需留意项目对 `.env.example` 的发布导出纪律。

### 步骤 4 — `app/auth.py` 增加令牌 helper（已在该模块，安全）
```python
import secrets, hashlib
from datetime import datetime, timedelta, timezone
from app.db.models import PasswordResetToken

def create_password_reset_token(db, user, ttl_hours: int = 1) -> str:
    raw = secrets.token_urlsafe(32)            # 原始令牌，仅返回给调用方用于发邮件
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    # 失效该用户此前所有未用令牌（防堆积/重放）
    db.query(PasswordResetToken).filter(
        PasswordResetToken.admin_user_id == user.id,
        PasswordResetToken.used.is_(False),
    ).update({PasswordResetToken.used: True})
    row = PasswordResetToken(
        id=str(uuid.uuid4()), admin_user_id=user.id, tenant_id=user.tenant_id,
        token=token_hash, expires_at=now + timedelta(hours=ttl_hours), used=False,
    )
    db.add(row); db.flush()
    return raw   # 调用方拼接链接后发邮件；此后不再持有原始值

def consume_password_reset_token(db, raw_token: str):
    # 返回 (user, token_row) 或 None（不存在/已用/过期）
    h = hashlib.sha256(raw_token.encode()).hexdigest()
    row = db.query(PasswordResetToken).filter(PasswordResetToken.token == h).first()
    if not row or row.used:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        row.used = True; db.flush(); return None
    user = db.query(AdminUser).filter(AdminUser.id == row.admin_user_id).first()
    if not user or user.status != "active":
        return None
    return user, row
```
> `app/auth.py` 顶部已有 `import uuid` / `from datetime import datetime, timedelta, timezone`；按需补 `import secrets, hashlib` 与 `from app.db.models import PasswordResetToken, AdminUser`（注意避免与现有 `AdminUser` 导入冲突——`auth.py` 当前如何从 `app.db.models` 取 `AdminUser` 请先 grep 确认导入方式再补）。

### 步骤 5 — 端点（全部加在 `backend/app/routers/auth.py`）

新增 pydantic body：
```python
class _ForgotBody(BaseModel):
    email: str
class _ResetBody(BaseModel):
    token: str
    password: str
```

`POST /api/auth/password/forgot`（公开）：
```python
@router.post("/api/auth/password/forgot")
def password_forgot(body: _ForgotBody, db: Session = Depends(get_db)):
    from app.auth import create_password_reset_token
    from app.email import send_password_reset_email
    from app.settings import get_email_settings, get_wecom_oauth_settings
    submitted = (body.email or "").strip().lower()
    # 仅在 default 租户内匹配 active 且有 password_hash + email 的 per-user 用户
    user = (
        db.query(AdminUser)
        .join(Tenant, Tenant.id == AdminUser.tenant_id)
        .filter(Tenant.slug == "default", Tenant.is_active.is_(True),
                func.lower(AdminUser.email) == submitted,
                AdminUser.status == "active",
                AdminUser.password_hash.isnot(None))
        .first()
    )
    if user and user.email:
        raw = create_password_reset_token(db, user, int((get_email_settings().reset_token_ttl_hours or "1")))
        base = get_email_settings().reset_base_url or get_wecom_oauth_settings().admin_domain
        link = f"{base.rstrip('/')}/admin/reset-password?token={raw}"
        db.commit()
        send_password_reset_email(user.email, link)
    else:
        db.rollback()   # 无用户：不建令牌、不发信
    # 零枚举：无论是否存在，统一返回成功
    return JSONResponse({"ok": True})
```

`POST /api/auth/password/reset`（公开）：
```python
@router.post("/api/auth/password/reset")
def password_reset(body: _ResetBody, db: Session = Depends(get_db)):
    from app.auth import consume_password_reset_token, hash_password
    result = consume_password_reset_token(db, body.token)
    if result is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")
    user, row = result
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="weak_password")
    user.password_hash = hash_password(body.password)
    row.used = True
    db.commit()
    return JSONResponse({"ok": True})
```

`GET /admin/forgot-password` 与 `GET /admin/reset-password`（公开 HTML，复用 `login.html` 外壳 + `render_template` + `I18N_SCRIPT_TAG`）：
- forgot 页：邮箱输入框 + 提交按钮（vanilla JS `fetch('/api/auth/password/forgot', ...)`，成功显示「邮件已发送」提示，沿用 `login.html` 的 `doLogin` 风格写 `doForgot`）。
- reset 页：读取 `?token=`，隐藏域带 token；新密码 + 确认密码输入；`fetch('/api/auth/password/reset', ...)`；成功提示并引导回登录；`invalid_or_expired_token` 显示「链接无效或已过期」。
- 两个页面必须注入 `__LOGIN_BODY__` 等价内容（直接写表单 HTML，不走 `_login_page`），并带 `styles.css` 与 i18n 引导脚本。

### 步骤 6 — 接上登录页「忘记密码」链接（`backend/app/routers/auth.py:114`）
将惰性 `<span id="forgot-password-disabled" ...>忘记密码？（即将上线）</span>` 改为：
```html
<a class="btn-link" href="/admin/forgot-password" data-i18n="login.forgotPassword">忘记密码</a>
```
并删除 `login.forgotPasswordDisabled` 这一 inert 键（或保留无引用均可，建议同步清理 i18n 中该键以免 `test_i18n_foundation.py` 断言——先 grep 该测试对键的要求再决定）。

### 步骤 7 — i18n 新键（`backend/app/assets/i18n.js`，**每个** locale 块加）
```
login.forgotPassword            = 忘记密码
login.forgotTitle               = 找回密码
login.forgotEmailPlaceholder    = 请输入您的注册邮箱
login.forgotSubmit              = 发送重置邮件
login.forgotEmailSent           = 若该邮箱已注册，重置链接已发送，请查收邮件。
login.forgotBackToLogin         = 返回登录
login.resetTitle                = 重置密码
login.resetNewPassword          = 新密码
login.resetConfirmPassword      = 确认新密码
login.resetSubmit              = 重置密码
login.resetSuccess             = 密码已重置，请使用新密码登录。
login.resetInvalidToken        = 链接无效或已过期，请重新申请。
login.resetWeakPassword        = 密码至少 8 位。
login.resetBackToLogin         = 返回登录
```
zh-TW/en 块给对应译文，格式严格对齐现有 `login.*` 键。

### 步骤 8 — 生成迁移并 `alembic check`
```bash
cd backend
alembic revision --autogenerate -m "RND-278 password_reset_tokens (F0-3)"
# 打开生成的迁移，令 down_revision = `alembic heads` 输出的当前 head；revision 顺延
alembic upgrade head
alembic check        # 必须绿（Status: Success）
```
迁移体应只含 `password_reset_tokens` 建表（PK/String36、两个索引、unique on token、Boolean used、DateTime 两列）。不要动 `uq_admin_users_tenant_wecom` 等任何既有对象。

## 六、阶段三验证（RED/GREEN 记录）

RED 基线（改前）：
```bash
cd backend
grep -n "forgot-password-disabled" app/routers/auth.py          # L114 惰性链接
ls alembic/versions/ | tail -1                                  # 当前 0016_media_download_attempts.py
python -c "from app.db import models; print('PasswordResetToken' in dir(models))"  # False
```

GREEN（改后）：
```bash
cd backend
python -c "from app.db import models; print([c.name for c in models.PasswordResetToken.__table__.columns])"  # 含 id/admin_user_id/tenant_id/token/expires_at/used/created_at
alembic upgrade head && alembic check        # 必须绿
python -c "import app.email; print('email module ok')"
make verify                                  # lint-diff typecheck build test 全绿（含新增 test_rnd278_password_reset.py）
```

### 第六·三节 — `.env.example` / B 层纪律提示（需留意）
项目现行发布纪律（RND-242/RND-237）将 `.env.example` 列为「B 层生产路径、由发布导出处理、功能票不改动」。但 SMTP 是本项目首个邮件配置、无其它文档落点。本票处理建议：
- `settings.py` 改动属于 app 代码，**在范围内**，正常改。
- `.env.example`：仅**追加**一段全新、清晰分隔的 SMTP 区块（`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`/`PASSWORD_RESET_BASE_URL`/`PASSWORD_RESET_TOKEN_TTL_HOURS`），**不改动任何既有条目**。若用户已把 `.env.example` 预留给 RND-237 导出、明确禁止功能票触碰，则改为在 `docs/` 补一份配置说明，并在代码注释指向该文档。**如遇歧义，停下报告，不要擅自改 `.env.example` 既有内容。**

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- F0-1 未落地（第二节断言失败）时不自行改 `models.py`/加迁移/改鉴权；停下报告。
- `password_reset_tokens.token` 只存 SHA-256 哈希；原始令牌不得入 DB、不得出现在任何 API 响应体或日志。
- `/forgot` 零枚举：邮箱不存在/未激活/非 per-user 用户 → 仍返回 `{"ok": true}`，不建令牌、不发信、不报错。
- 不改现有登录路由 `password_login` / `wecom_login` / `wecom_callback` 行为；不改 `/api/auth/password/login` 的 URL/方法/成功体/失败体/404/500 守卫；cookie 标志不变。
- 不改 `/api/auth/me`（RND-276 已改 `role`）；本票不动它。
- 不引入新第三方依赖（邮件用 stdlib `smtplib`/`email`）。
- 不引 React；新页面 SSR + 原生 JS，复用 `styles.css` 设计系统。
- 不碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd 单元名、`deploy.yml`、`backend/scripts`）；`.env.example` 仅按第六·三节追加新段、不动既有。
- `backend/tests/test_architecture_boundary.py` 必须仍 PASS（已把 `app.email` 加入 `_FLAT_SERVICE_MODULES`）；`app/main.py` 不得新增任何路由。
- `alembic check` 必须绿；迁移 `down_revision` 以运行时 `alembic heads` 为准（不要硬编码 0016/0017）。

## 八、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应含 `app/db/models.py` + 新迁移 + `app/email.py` + `app/auth.py`(helper) + `app/settings.py` + `app/routers/auth.py` + 2 个新模板 + `i18n.js` + 架构边界测试 `_FLAT_SERVICE_MODULES` + 新测试 `test_rnd278_password_reset.py`，`.env.example` 视第六·三节决定）、`alembic check` 绿日志、`make verify` 全绿日志、未提交声明。
