# RND-286 开发 agent 执行提示词 —— A3-3 启用/停用 + 管理员重置密码

> 面向开发 agent（单人端到端实现 RND-286）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS（不引 React）。

## 一、任务（一句话）

为管理员提供 **A3-3 用户启用/停用 + 管理员触发重置密码** 两个后端能力：新增 `PATCH /api/admin/users/{id}`（翻转 `AdminUser.status`）与 `POST /api/admin/users/{id}/reset-password`（复用 F0-3 令牌 + 邮件基础设施）。**不新增模型/迁移、不改登录/WeCom 流程、不实现「改自身密码」（那是 A8-1 的 Non-goals）。**

## 二、前置依赖（开工前必查，任一不满足 → 停下并报告）

本任务 `[BLOCKED: F0]`。F0 子票（F0-1/RND-277、F0-2/RND-276、F0-3/RND-278、F0-5/RND-280）均已合并进工作树，但开工前仍需校验：

```bash
cd backend
python -c "from app.db import models; c=[x.name for x in models.AdminUser.__table__.columns]; assert {'password_hash','email','role','status'} <= set(c), c; print('F0-1 OK', c)"
python -c "from app.auth import require_role, create_password_reset_token; print('F0-5/F0-3 helpers OK')"
python -c "from app.email import send_password_reset_email; print('email OK')"
grep -n 'AdminUser.status == "active"' app/routers/auth.py   # 应命中 password_login 内（约 L459），确认禁用用户已被登录拦截
alembic check                                                  # 必须绿（Status: Success）
```

断言失败或出现 `ImportError` → **停下报告**（依赖未落地；严禁自行改 `models.py`/加迁移/改鉴权/改 `auth.py` 既有 helper，那属于 F0 各子票）。
`alembic check` 不绿（有其他未迁移改动）→ 停下报告，不要擅自 `alembic upgrade`。

## 三、决策背景（已全部拍板，不要再问）

来源：Epic `RND-273`（A3 用户管理）子任务 A3-3 + 既有账号体系（F0 各子票）。

- **复用既有能力，零新建安全原语**：
  - `app/auth.py:require_role`（`L378`，F0-5 RBAC 脚手架）→ 本票两个端点用 `Depends(require_role("admin", "owner"))` 做角色门禁，返回 `(AdminUser, tenant_id)`。
  - `app/auth.py:create_password_reset_token`（F0-3）→ 管理员重置复用它生成一次性令牌（自动失效该用户此前未用令牌）。
  - `app/email.py:send_password_reset_email`（F0-3）→ 复用邮件发送（无 SMTP 时走 console 传输返回 `True`）。
  - `app/routers/auth.py:password_login` **已按 `status=="active"` 过滤**（L459）→ 「停用用户无法登录」的 AC 由既有登录逻辑保证，本票**不**改登录代码。
  - `app/auth.py:consume_password_reset_token`（L199）对 `status != "active"` 返回 `None` → 禁用用户的重置链接也无法消费，与本票一致。
- **租户隔离是硬 AC**：目标用户必须经 `AdminUser.tenant_id == 当前管理员租户` 过滤；跨租户 id 一律 404（避免账号枚举）。`tenant_id` 只从 `require_role` 返回的会话身份取，**绝不**接受请求体/查询参数里的租户值。
- **零枚举**：目标不存在 / 不在本租户 → 统一 404（不区分「无此 id」与「跨租户」）。
- **Non-goals（硬边界）**：**不**实现「管理员改自身密码」或「用户自助改密」端点（属 A8-1）；**不**新建 `AdminUser`/迁移/新表；**不**改 `password_login`/`wecom_login`/`wecom_callback`/`auth_me`/`logout`；**不**改 `.env.example`/B 层生产路径。
- **本票范围 = 纯后端 API + 测试**：用户管理列表/邀请 UI 属 A3-1/A3-2；本票**不**新建或改 SSR 页面（D1 冻结前提下也无需此票做页面）。
- **工程纪律**：不 git commit/push；代码标识符加反引号；架构冻结 D1（不引 React）。

## 四、项目现状（精确落点）

### 4.1 模型（只读复用，不改）
- `backend/app/db/models.py`：`AdminUser`（`L132-202`）已含 `status`（`Enum("active","disabled")`，`L190-194`）、`role`、`email`、`password_hash`、`id`(String36 PK)；`AdminSession`（`L204+`）含 `is_revoked`、`admin_user_id`、`expires_at`。
- `PasswordResetToken`（`app/db/models.py`，F0-3 已建）含 `admin_user_id`/`tenant_id`/`token`(SHA-256 哈希)/`expires_at`/`used`，本票只读复用。

### 4.2 既有 helper（导入复用，不改语义）
- `backend/app/auth.py`：`require_role`（`L378`）、`create_password_reset_token`（`L172`）、`get_current_user`（`L319`）、`SESSION_COOKIE`（如需要）。
- `backend/app/email.py`：`send_password_reset_email(to_email, reset_link, locale="zh-CN")` → `bool`（`L20`）。
- `backend/app/settings.py`：`get_email_settings()`（含 `reset_token_ttl_hours` `L?` 与 `reset_base_url`）、`get_wecom_oauth_settings()`（`admin_domain`）。

### 4.3 路由注册（新增模块）
- `backend/app/main.py`：现有 `app.include_router(sync_router, prefix="/api/admin")`（`L99`）范式。本票新增 `users_router` 并以**相同 `prefix="/api/admin"`** 注册，使路由得到字面 URL `/api/admin/users/{id}` 与 `/api/admin/users/{id}/reset-password`：
  ```python
  from app.routers.users import router as users_router
  # ...在 L99 之后、L100 之前（或同类 include_router 区）插入：
  app.include_router(users_router, prefix="/api/admin")
  ```

### 4.4 架构边界（`backend/tests/test_architecture_boundary.py`，CI 强制）
- `routers/users.py` 是**路由模块**（非 flat service），**无需**加入 `_FLAT_SERVICE_MODULES`。
- 约束：`users.py` **不得** `import app.routers.*` 或 `app.main`；只允许 `from app.auth import ...`、`from app.email import ...`、`from app.db.models import ...`、`from app.db.session import get_db`、`from app.settings import ...`、`from fastapi import ...`。保持 router 叶子边界。

### 4.5 HTTP 契约测试（必同步更新，否则 `make verify` 红）
`backend/tests/test_http_contract.py` 三处必须同步更新（新增 2 条路由）：
1. **`L325`** `assert route_count == 42` → `== 44`（注释补 `RND-286: +2 admin user routes`）。
2. **`L334-377`** `test_routers_are_registered` 的 `expected` 集合：追加 `"/api/admin/users/{id}"` 与 `"/api/admin/users/{id}/reset-password"`。
3. **`L403-492`** `test_route_snapshot_with_real_model_names` 的 `expected` 列表：追加两条（顺序无关，断言为 `sorted(actual) == sorted(expected)`，`L493`）：
   ```python
   ("/api/admin/users/{id}", frozenset({"PATCH"}), "None", "None"),
   ("/api/admin/users/{id}/reset-password", frozenset({"POST"}), "None", "None"),
   ```
   > 本票两个端点**不**设 `response_model`、返回普通 dict（FastAPI 自动包 `JSONResponse`）→ 快照 `response_model="None"`、`response_class="None"`。若你选择加 `response_model`，须同步改此快照字符串，二者必须一致。

## 五、实现步骤（GREEN，最小变更）

### 步骤 1 — 新建 `backend/app/routers/users.py`
```python
"""Admin user lifecycle management (RND-286 / A3-3): enable/disable + admin-triggered password reset.

Tenant-scoped and role-gated (require_role("admin","owner")). Reuses F0-3
password-reset token + email infrastructure; no new models/migrations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import create_password_reset_token, require_role
from app.db.models import AdminSession, AdminUser
from app.db.session import get_db
from app.email import send_password_reset_email
from app.settings import get_email_settings, get_wecom_oauth_settings

router = APIRouter()


class _StatusUpdate(BaseModel):
    status: str  # "active" | "disabled"


def _resolve_target(db: Session, user_id: str, tenant_id: str) -> AdminUser:
    """Tenant-scoped lookup; 404 (not 403) on miss to avoid account enumeration."""
    user = (
        db.query(AdminUser)
        .filter(AdminUser.id == user_id, AdminUser.tenant_id == tenant_id)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _user_dto(user: AdminUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
    }


@router.patch("/users/{user_id}")
def update_user_status(
    user_id: str,
    body: _StatusUpdate,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enable or disable a user within the admin's tenant."""
    if body.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="invalid_status")
    current_user, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    # Lockout prevention: an admin may not disable their own account.
    if target.id == current_user.id and body.status == "disabled":
        raise HTTPException(status_code=400, detail="cannot_disable_self")
    target.status = body.status
    # (Recommended hardening, optional) revoke the target's live sessions on disable.
    if body.status == "disabled":
        now = datetime.now(timezone.utc)
        db.query(AdminSession).filter(
            AdminSession.admin_user_id == target.id,
            AdminSession.is_revoked.is_(False),
            AdminSession.expires_at > now,
        ).update({AdminSession.is_revoked: True})
    db.commit()
    return _user_dto(target)


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: str,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Trigger a password reset for another user: email them a reset link."""
    current_user, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    if target.status != "active":
        raise HTTPException(status_code=409, detail="user_not_active")
    if not target.email:
        raise HTTPException(status_code=400, detail="user_has_no_email")
    email_settings = get_email_settings()
    try:
        ttl = int(email_settings.reset_token_ttl_hours or "1")
    except ValueError:
        ttl = 1
    raw_token = create_password_reset_token(db, target, ttl)  # flush, not commit
    base_url = email_settings.reset_base_url or get_wecom_oauth_settings().admin_domain
    reset_link = f"{base_url.rstrip('/')}/admin/reset-password?token={raw_token}"
    delivered = send_password_reset_email(target.email, reset_link)
    if not delivered:
        db.rollback()
        raise HTTPException(status_code=500, detail="email_delivery_failed")
    db.commit()
    return {"ok": True}
```

> 注意：`AdminSession` 已在 `app/db/models.py` 定义，直接导入即可；`datetime`/`timezone` 为 Python 标准库。
> 「Recommended hardening」处的 session revoke 是**可选加固**（纵深防御）。实现它时务必 `db.commit()` 包含在 PATCH 的事务内；若选择不实现，删除该 `if` 块即可，不影响 AC。

### 步骤 2 — 注册路由（`backend/app/main.py`）
见 4.3：加 `from app.routers.users import router as users_router` + `app.include_router(users_router, prefix="/api/admin")`（紧邻 `sync_router` 那行）。

### 步骤 3 — 同步 HTTP 契约测试（`backend/tests/test_http_contract.py`）
见 4.5 三处：route_count 42→44、path 集合追加 2 条、snapshot 列表追加 2 条（顺序无关）。

### 步骤 4 — 新增测试 `backend/tests/test_rnd286_user_admin.py`
覆盖（沿用 `test_password_auth.py` 的 `dependency_overrides[get_db]` 工厂模式，或复用 `backend/tests/conftest.py` 的 DB fixture；无 `DATABASE_URL` 时优雅 skip）：
- PATCH 翻转同租户用户 `status` active↔disabled → 200 且返回 DTO 的 `status` 正确。
- **AC 核心**：PATCH 置 `disabled` 后，该用户走 `POST /api/auth/password/login`（per-user 路径，F0-2）→ 401（验证「停用用户无法登录」由 auth.py:459 保障）。
- PATCH 非法 `status` 值（如 `"foo"`）→ 400 `invalid_status`。
- PATCH 跨租户 id → 404（零枚举，不泄露是否存在）。
- PATCH 不存在 id → 404。
- PATCH 自己为 `disabled` → 400 `cannot_disable_self`；自己置 `active` 放行。
- POST reset-password 对 active 且有 email 的用户 → 200 `{"ok": true}`；`PasswordResetToken` 新增一行（used=False、expires_at 未来）；邮件模块被调用（console 传输下 `logger.warning("[email-console] ...")` 可见链接）。
- POST reset-password 对 `disabled` 用户 → 409 `user_not_active`。
- POST reset-password 对无 `email` 用户 → 400 `user_has_no_email`。
- POST reset-password 跨租户 id → 404。
- RBAC：以 `readonlyaudit`/`compliance`/`legal` 角色调用 → 403 `Insufficient role for this operation`。
- 未认证调用 → 401。

## 六、阶段三验证（RED/GREEN 记录）

RED 基线（改前）：
```bash
cd backend
ls app/routers/users.py              # 不存在
grep -n "api/admin/users" app/main.py   # 无
python -c "from app.main import app; print(sum(1 for r in app.routes if hasattr(r,'methods')))"  # 42
```

GREEN（改后）：
```bash
cd backend
python -c "from app.routers.users import router; print('users router ok')"
python -c "from app.main import app; print(sum(1 for r in app.routes if hasattr(r,'methods')))"  # 44
alembic check                        # 必须绿（无 schema 变更）
make verify                          # lint-diff typecheck build test 全绿（含 test_rnd286_user_admin.py）
```

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- F0 前置断言失败时不自行改 `models.py`/加迁移/改鉴权/改 `auth.py` 既有 helper；停下报告。
- **租户隔离**：目标用户必须经 `tenant_id == 当前会话租户` 过滤；跨租户一律 404，绝不接受请求体/参数里的租户值。
- **RBAC**：两端点必须 `Depends(require_role("admin", "owner"))`；非 admin/owner → 403。
- 不新增模型/迁移/表；`alembic check` 必须保持绿（本票无 schema 变更）。
- 不改 `password_login`/`wecom_login`/`wecom_callback`/`auth_me`/`logout` 行为与 URL；不改 `/api/auth/me`；不改 cookie 标志。
- **Non-goals**：不实现「改自身密码 / 用户自助改密」端点（A8-1）；不新建 SSR 页面（列表/邀请 UI 归 A3-1/A3-2）。
- 不引 React；本票纯后端 + 测试。
- 不碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd、`.env.example`、`deploy.yml`、`backend/scripts`）；不新增第三方依赖。
- `backend/tests/test_architecture_boundary.py` 必须仍 PASS（新 router 未违反边界）；`app/main.py` 仅新增 `include_router` 调用，不新增内联路由。
- `test_http_contract.py` 三处必须同步更新（route_count 44、path 集合、snapshot 列表），否则 `make verify` 红。
- 原始重置令牌只经 `create_password_reset_token` 返回、拼入邮件链接；不得写入任何响应体或日志。

## 八、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应含 `app/routers/users.py`(新) + `app/main.py`(注册) + `tests/test_http_contract.py`(3 处) + `tests/test_rnd286_user_admin.py`(新)）、`alembic check` 绿日志、`make verify` 全绿日志、未提交声明。
