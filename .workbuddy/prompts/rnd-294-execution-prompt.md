# RND-294 开发 agent 执行提示词 —— A7-2 审计写入钩子（write_audit 工具）

> 面向开发 agent（单人端到端实现 RND-294）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

提供 `write_audit(...)` 写入钩子 + 规范动作/对象词表（新建 `backend/app/audit.py`），并**additive** 接入认证生命周期（`wecom_callback` 登录成功、`auth_logout` 退出，以及 `password_login` 登录成功——与 RND-276 协调），把动作写入 **A7-1（RND-293）已建好的 `audit_logs` 表**。本票**不新建表 / 不写迁移 / 不新建路由 / 不改既有行为语义**。

## 二、决策背景（已全部拍板，不要再问）

来源：Epic `RND-274`（A7 审计日志）+ 子任务 `RND-293`（A7-1 建表）/ `RND-294`（A7-2 本票）/ `RND-295`（A7-3 列表 API）。

- **强前置依赖 A7-1（RND-293）**：`AuditLog` 模型 + 迁移 `0018` 必须**已合并**且 `alembic check` 绿。本票只消费该表，**严禁**自己建模型/迁移（那属于 A7-1，越界即判失败）。
- **`action` / `object_type` 是 `Text`（A7-1 故意不锁枚举）**：本票定义**规范词表常量** `AuditAction` / `AuditObjectType` 作为全仓统一用词，供后续各功能票复用。
- **`write_audit` 必须 fail-safe（核心）**：审计写入失败**绝不能**影响主请求。用 **SQLAlchemy savepoint（`db.begin_nested()`）** 隔离——审计失败只回滚 savepoint，外层事务（含主动作）不受影响，且绝不向外抛异常。
- **`detail` 绝不存消息正文 / `decrypted_payload`**（与 SF-1 数据最小化一致）：仅存结构化上下文（如 corp_id、mode、来源 IP）。
- **架构边界**：新建 `app/audit.py` 是 flat service 模块，必须加入 `backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（参照 RND-278 对 `app/email.py` 的做法），否则架构测试会判违反。
- **工程纪律**：Agent 不 git commit/push；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。代码标识符加反引号。

## 三、项目现状（基线，2026-07-28 扫描）

### 3.1 A7-1 尚未合并 → 本票真实 BLOCKED
- 全仓 grep `AuditLog|audit_log` **无任何命中** → `AuditLog` 模型/迁移不在工作树。
- 迁移链 head = `0017`（`0017_admin_users_account_fields.py`，RND-277 F0-1）；A7-1 预期 `0018_audit_log.py`（`down_revision="0017"`）。
- **结论**：开工前必须先校验 A7-1 已落地（见第四节步骤 0）。若未落地，停下报告，**不要**自行补 `AuditLog` 模型或迁移。

### 3.2 `routers/auth.py` 接入落点（已读，精确行号）
- `wecom_callback`：`L376–579`。成功建 session：`db.add(session)` 在 `L549`、`db.flush()` 在 `L550`；`db.commit()` 在 `L571`（且 `L552–559` 注释要求 commit 必须是该块最后一条语句）。`user` / `tenant_id` / `corp_id` 在此作用域可用。
- `auth_logout`：`L636–656`。`session.is_revoked = True` 在 `L650`，`db.commit()` 在 `L651`，均在 `if session:` 块内（L649）。`session.tenant_id` / `session.admin_user_id` 可用。
- `password_login`：`L222–326`。`db.add(session)` 在 `L311`，`db.commit()` 在 `L312`。**该函数在 RND-276（F0-2 per-user 密码鉴权）的工作树里被改动**（见第六节协调规则）。
- `routers/auth.py` 顶部已 `from app.db.models import AdminUser, AdminSession, Tenant, TenantWecomConfig`（及 `uuid`、`timedelta`、`datetime`、`timezone`）→ 新增 `AuditLog` 到该 import 即可。

### 3.3 `get_db` / Session 注入范式
- 路由签名已有 `db: Session = Depends(get_db)`。`write_audit(db, ...)` 复用同一 `db`。

## 四、目标实现（精确落点）

### 步骤 0 — 开工前置校验（A7-1 已落地？）
```bash
cd backend
python -c "from app.db.models import AuditLog; print([c.name for c in AuditLog.__table__.columns])"  # 应列出 id/tenant_id/admin_user_id/action/object_type/object_id/detail/created_at
alembic check   # 必须绿（A7-1 已迁移且一致）
```
- 若任一步失败 → **停下报告**，不要补模型/迁移，不要继续。
- 若通过 → 继续。

### 4.1 新模块 `backend/app/audit.py`
```python
"""
Audit-log write hook (RND-294 / A7-2).

Provides `write_audit(...)` for recording admin actions into the immutable,
append-only `audit_logs` table created by RND-293 (A7-1). See that ticket for
the schema and the immutable contract.

Design rules
------------
- FAIL-SAFE: any failure while recording an audit row is isolated inside a
  SAVEPOINT and swallowed, so a broken audit sink can NEVER break the primary
  request or poison its transaction.
- `write_audit` adds + flushes the row but does NOT commit; the calling
  route's existing `db.commit()` persists it atomically with the action.
- `detail` holds ONLY structured, non-sensitive context. Never pass message
  bodies or decrypted payloads (SF-1 data minimization).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.db.models import AuditLog

logger = logging.getLogger(__name__)


class AuditAction:
    """Canonical action verbs. DB column is Text (A7-1 deliberately did not
    lock an enum); these constants are the agreed word list for all tickets."""
    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    USER_INVITED = "user.invited"
    USER_DISABLED = "user.disabled"
    USER_ENABLED = "user.enabled"
    CONFIG_VIEWED = "config.viewed"
    CONFIG_CHANGED = "config.changed"


class AuditObjectType:
    USER = "admin_user"
    SESSION = "admin_session"
    TENANT_CONFIG = "tenant_config"
    PASSWORD_RESET_TOKEN = "password_reset_token"


def write_audit(
    db: Session,
    *,
    tenant_id: str,
    action: str,
    object_type: str,
    admin_user_id: Optional[str] = None,
    object_id: Optional[str] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append an immutable audit row. FAIL-SAFE: never raises."""
    try:
        with db.begin_nested():
            db.add(
                AuditLog(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    admin_user_id=admin_user_id,
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                    detail=dict(detail) if detail else None,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.flush()
    except Exception:
        # Isolated by the savepoint above — outer transaction is untouched.
        logger.exception("write_audit: failed to record audit row (action=%s)", action)
```

> `created_at` 显式赋值（模型也有 `server_default=func.now()`，二者一致，显式值优先，测试更确定）。
> 不要给 `write_audit` 加 `db.commit()`——保持与主动作同一事务原子提交。

### 4.2 注册架构边界
编辑 `backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（约 `L71`，紧邻 `"app.auth",` 之后）加入：
```python
    "app.audit",
```
> 否则 `app.audit` 被归类为 `other`，虽不触发反向依赖报错，但按 RND-278 约定应显式登记为 flat service 模块。

### 4.3 接入认证生命周期（additive，仅插入调用）
- **`wecom_callback`**：在 `L550` `db.flush()` 之后、`L552` 注释块之前插入：
  ```python
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=user.id,
            action=AuditAction.LOGIN,
            object_type=AuditObjectType.USER,
            object_id=user.id,
            detail={"corp_id": corp_id, "method": "wecom_oauth"},
        )
  ```
  （位于 `L571` `db.commit()` 之前 → 与 session 同事务原子提交；savepoint 保证即使审计失败也不破坏登录。）
- **`auth_logout`**：在 `L650` `session.is_revoked = True` 之后、`L651` `db.commit()` 之前插入（仍在 `if session:` 块内）：
  ```python
            write_audit(
                db,
                tenant_id=session.tenant_id,
                admin_user_id=session.admin_user_id,
                action=AuditAction.LOGOUT,
                object_type=AuditObjectType.SESSION,
                object_id=session_id,
            )
  ```
- **`password_login`**（与 RND-276 协调，见第六节）：在 `L311` `db.add(session)` 之后、`L312` `db.commit()` 之前插入：
  ```python
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        action=AuditAction.LOGIN,
        object_type=AuditObjectType.USER,
        object_id=user.id,
        detail={"mode": "password"},
    )
  ```
- `routers/auth.py` 顶部 import 增加：`from app.audit import write_audit, AuditAction, AuditObjectType`（及 `AuditLog` 并入已有 `from app.db.models import ...`）。

### 4.4 测试：新建 `backend/tests/test_rnd294_audit_hook.py`
沿用 `test_rnd277_admin_user_extension.py` / `test_rnd293_audit_log.py` 风格：
- **词表单测（无需 DB）**：`from app.audit import write_audit, AuditAction, AuditObjectType`；断言 `AuditAction.LOGIN == "auth.login"`、`AuditAction.LOGOUT == "auth.logout"`、`AuditObjectType.USER == "admin_user"` 等常量存在且为预期字符串。
- **写入+回环（DB 门控 `_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))`）**：用 `Session` 调 `write_audit(db, tenant_id="t1", admin_user_id="u1", action=AuditAction.LOGIN, object_type=AuditObjectType.USER, object_id="u1", detail={"ip": "127.0.0.1"})` → `db.commit()` → 查回 `AuditLog` 断言全部字段 + `detail["ip"]=="127.0.0.1"`（JSONB 字典回环）。finally 清理。
- **fail-safe 单测（无需真实 DB）**：构造一个 `db` stub，其 `begin_nested` 抛异常（或 `add` 抛异常），断言 `write_audit(stub, ...)` **不向外抛**（被吞掉）。
- **集成（DB 门控）**：seed 一个 `admin_sessions` 行 → `client.post("/api/auth/logout", cookies={"session_id": ...})` → 断言 `audit_logs` 出现 `action == AuditAction.LOGOUT` 的行（复用契约测试库 schema，A7-1 合并后该表已存在）。
- 无 DB 时上述 DB 类测试按 `_DB_AVAILABLE` 自动 `skip`，不得 FAIL。

## 五、验证（GREEN + 回归 + make verify）

1. **前置校验（步骤 0）**：A7-1 已合并、`alembic check` 绿——证据：命令输出。
2. **功能验证**：有 DB 环境下，登录（WeCom/密码）与退出后查 `audit_logs` 均出现对应 `action` 行。
3. **回归**：`cd backend && make verify` 全绿（含 `test_architecture_boundary.py`：`app.audit` 已入 allowlist，无反向依赖）。
4. **失败先修实现**：若 `make verify` 因 `app.audit` 未登记 allowlist 而失败 → 补 4.2；若 savepoint 在 sqlite 报错 → 确认 `Session` 处于活跃事务（`db.begin_nested()` 需外层 autobegin，路由 `db` 已满足）。

## 六、与 RND-276 / RND-278 的协调（避免冲突）

- `routers/auth.py` 同时被 **RND-276（F0-2，改 `password_login` 为 per-user + `auth_me` 返 role）** 与 **RND-278（F0-3，新增 `/api/auth/password/forgot`、`/reset` 路由）** 改动。
- 本票对 `routers/auth.py` 的改动**全部是 additive 调用插入**：
  - `wecom_callback`、`auth_logout` 两处**未被** RND-276/278 触碰 → 直接插入。
  - `password_login` 被 RND-276 改动 → **协调规则**：若 RND-276 已将其改为 per-user，将 `write_audit` 调用置于其「session 创建块」之后（语义不变，additive）；若 RND-276 尚未应用，置于 `L311` 之后。两票互不回退对方逻辑。
- 若工作树中 `password_login` 状态难以判断，**优先保证 `wecom_callback` + `auth_logout` 两处接入**，并在交付说明中注明 `password_login` 审计接入状态（已接 / 待 RND-276 合并后补），**不要**为接而破坏 RND-276 的改动。

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- **不新建 / 不改 `AuditLog` 模型或任何迁移**（属 A7-1）；`alembic check` 必须仍绿（本票零 schema 变更）。
- 不新建路由（route count 基线不变，`test_http_contract` 不受影响）；不改 `get_current_user` / WeCom OAuth / 登录语义（仅插入审计调用）。
- `write_audit` 必须 fail-safe：`db.begin_nested()` 隔离 + 吞异常，绝不向外抛、绝不破坏主事务。
- `detail` 不得含消息正文 / `decrypted_payload`（SF-1）。
- 仅 additive 接入 `routers/auth.py`，不回退 RND-276/278 的改动。
- 不碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd 单元名、deploy.yml、`.env.example`、`backend/scripts`）。
- 架构边界：`app.audit` 加入 `_FLAT_SERVICE_MODULES`；无 `service→router`、无 `router→main`。

## 八、收尾（交付物）

向用户交付：
- 前置校验证据（A7-1 已合并 / `alembic check` 绿）。
- 接入落点清单（`wecom_callback` L550 后 / `auth_logout` L650 后 / `password_login` 协调状态）。
- `git diff --stat`（应仅含 `backend/app/audit.py` 新文件 + `backend/app/routers/auth.py` + `backend/tests/test_architecture_boundary.py` + `backend/tests/test_rnd294_audit_hook.py`）。
- `make verify` 日志（全绿）。
- 未提交声明。
