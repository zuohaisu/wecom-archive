# RND-279 开发 agent 执行提示词 —— F0-4 会话生命周期自动化（清理 + last_active_at 维护）

> 面向开发 agent（单人端到端实现 RND-279）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。

## 一、任务（一句话）

为管理员会话体系补齐「生命周期自动化」：① 清理过期（`expires_at` 已过）与已吊销（`is_revoked=True`）的 `admin_sessions` 行；② 在每次已认证活动（API 经 `get_current_user`、HTML 页经 `require_html_session`）时维护 `AdminUser.last_active_at`（区别于 `last_login_at`，节流写入）；③ 把当前硬编码的会话 TTL（`SESSION_TTL_HOURS = 8`）变为可配置项（env `SESSION_TTL_HOURS`，默认 8h，带边界 clamp）。**不新建任何数据库表 / 不新增 Alembic 迁移**（见第二节）。

## 二、前置依赖（开工前必查，不满足 → 停下并报告）

本任务 **BLOCKED on F0-1**（RND-277 `AdminUser` 扩展）。当前工作树里 F0-1 的迁移 `0017_admin_users_account_fields.py` 与 `test_rnd277_admin_user_extension.py` 已存在，故基线大概率已就绪，但仍请断言：

```bash
cd backend
python -c "from app.db import models; c=[x.name for x in models.AdminUser.__table__.columns]; assert 'last_active_at' in c, c; print('last_active_at OK', c)"
python -c "from app.db import models; c=[x.name for c in models.AdminSession.__table__.columns]; assert {'id','admin_user_id','tenant_id','wecom_user_id','expires_at','is_revoked'} <= set(c), c; print('AdminSession OK', c)"
```

- 断言失败（列缺失）→ **停下报告**，禁止自行改 `models.py` / 加迁移 / 改鉴权（那属于 F0-1）。
- 关键事实：**`AdminSession` 表（迁移在 `0002_tenant_foundation.py`）与 `AdminUser.last_active_at` 列（F0-1 加）都已存在**。因此本票**不需要任何 Alembic 迁移**。如果实现过程中你认为需要新增列/表，请**停下报告**，不要擅自 `alembic revision`（RND-278 的教训：迁移编号易冲突，且本票设计上无需 schema 变更）。

## 三、决策背景（已全部拍板，不要再问）

来源：Epic `RND-263`（F0 账号体系重构）子任务 F0-4。

- **`last_active_at` 语义已锁定**：在 `AdminUser` 上，由「用户活动」更新，与 `last_login_at`（仅登录时更新）是两个独立字段，互不覆盖。本票只维护 `last_active_at`，**绝不**写 `last_login_at`。
- **清理 = 物理删除**：直接 `DELETE` 满足 `expires_at <= now OR is_revoked == True` 的 `admin_sessions` 行。不做软删除、不保留审计列（会话行本身非业务实体，过期即弃）。
- **触发方式（尊重 B 层纪律）**：`backend/scripts/` 是 B 层生产路径、**禁止功能票改动**，所以不能把清理做成 `scripts/*_once.py` + systemd timer（那是 RND-237 发布导出范畴）。本票采用双触发：
  1. **每次成功登录后**（在 `password_login` / `wecom_callback` 创建新会话之后）调用 `cleanup_expired_sessions(db)` —— 零新增基建即可持续收敛表增长；
  2. **可 cron 的 CLI**：`python -m app.session_lifecycle` 直接清一轮（供运维定时跑），不依赖 `backend/scripts`。
- **`SESSION_TTL_HOURS` 配置化**：RND-244 配置中心（DB>env>default + Fernet）**尚未实现**（`backend/app/config*.py` 不存在），所以本票走现有 `settings.py`（pydantic-settings 包裹 env）模式，新增 `AuthSettings.session_ttl_hours` 字段 + `auth.py` 内 `get_session_ttl_hours()` 读取/解析，默认 8、夹取 `[1, 8760]`。不依赖未建的配置中心。
- **节流写入**：`get_current_user` 是每个已认证请求的第一依赖，若每次都写库会把 `updated_at` 也跟着刷爆。维护一个 `LAST_ACTIVE_TOUCH_INTERVAL_SECONDS = 300`（5 分钟）阈值：仅当 `last_active_at` 为 `None` 或已过去 ≥ 300s 才写。用「条件 UPDATE」语义保证幂等。
- **提交策略（连接池友好）**：`touch_last_active` 复用**请求注入的 `db`** 并显式 `db.commit()`，**不开新连接**（引擎 `pool_size=2`、并发下开第二条连接会耗尽池）。`get_current_user`/`require_html_session` 是请求首个依赖，此时无任何业务写入待提交，故提交安全；若下游路由后续 rollback，已提交的 `last_active_at` 不受影响（活动已被记录，正是期望行为）。
- **工程纪律**：Agent 不 git commit/push；代码标识符加反引号；架构冻结 D1（本票无前端）。

## 四、项目现状（精确落点）

### 4.1 模型（已存在，勿改）
- `backend/app/db/models.py`：`AdminUser`（`L132-201`，含 `last_active_at` `L198`）；`AdminSession`（`L204-229`，列 `id`/`admin_user_id`/`tenant_id`/`wecom_user_id`/`created_at`/`expires_at`/`is_revoked`）。**本票不改这两张表**。
- 会话主键 `id` 即 cookie 值（`SESSION_COOKIE = "session_id"`，`auth.py:44`）。

### 4.2 会话解析依赖（要改写）
- `backend/app/auth.py`：
  - `L45`：`SESSION_TTL_HOURS = 8`（硬编码常量，本票替换为 `get_session_ttl_hours()`）。
  - `L238-284`：`get_current_user(session_id, db)` —— 校验 `expires_at > now` 且 `is_revoked == False`，返回 `(user, tenant_id)`。**在此函数取到 `user` 之后、`return` 之前调用 `touch_last_active(user, db)`**。
  - `L287-317`：`require_html_session(request, db)` —— 仅返回 `tenant_id`（不取 user）。**在此函数确认 session 有效后，额外 `db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()` 取 user 并调用 `touch_last_active(user, db)`**，返回类型仍为 `Optional[str]` 不变。
  - 已导入：`from app.db.models import AdminSession, AdminUser`（`L38`）、`from app.db.session import get_db`（`L39`）、`from app.settings import get_auth_settings`（`L40`）、`from datetime import datetime, timezone`（`L31`）。需补 `from datetime import timedelta` 与 `from app.session_lifecycle import touch_last_active, cleanup_expired_sessions`（注意 `session_lifecycle` 是新增模块，见 4.4）。
- `backend/app/routers/auth.py`：
  - `L41` 顶部 `from app.auth import (..., SESSION_TTL_HOURS, ...)` → 改为 `get_session_ttl_hours`。
  - `L302` / `L324`（`password_login`）：`expires_at = now + timedelta(hours=SESSION_TTL_HOURS)` 与 `max_age=SESSION_TTL_HOURS * 3600` → 改用 `get_session_ttl_hours()`。
  - `L540` / `L568`（`wecom_callback`）：同上两处。
  - 在两条登录路径 `db.commit()` 之前（或之后，幂等）插入 `cleanup_expired_sessions(db)` 调用。
  - 现有登录/WeCom/OAuth/logout 行为、URL、cookie 标志、404/500 守卫**一律不动**（硬约束见第七节）。

### 4.3 配置落点
- `backend/app/settings.py`：`AuthSettings`（`L44-52`）。在其后新增字段 `session_ttl_hours: str = ""`（pydantic-settings 字段名 → env `SESSION_TTL_HOURS`，大小写不敏感，与现有 `auth_mode`→`AUTH_MODE` 同机制）。保持「无缓存、每调用新建实例」范式。

### 4.4 新模块落点（flat service）
- 新建 `backend/app/session_lifecycle.py`（项目根 `app/` 下扁平 service 模块，参照 `app.auth` 风格）。内容见第五节。
- `backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（`L70-93`）**必须**加入 `"app.session_lifecycle"`，否则 CI 架构边界测试失败。`session_lifecycle` 只允许 `import app.db.models` / `app.db.session`（db 层）与 `sqlalchemy`，**严禁** `import app.routers.*` 或 `app.main`（否则反向依赖检查失败）。

## 五、实现步骤（GREEN，最小变更）

### 步骤 1 — 新增 `backend/app/session_lifecycle.py`（flat service + CLI）
```python
"""
Session lifecycle automation (RND-279 / F0-4).

- touch_last_active: throttled maintenance of AdminUser.last_active_at on
  authenticated activity (distinct from last_login_at, which is set only on
  login). Uses the request's db session and commits on it (no separate
  connection, to respect the small engine pool); get_current_user runs
  first so no business writes are pending when we commit.
- cleanup_expired_sessions: physically DELETE admin_sessions rows that are
  expired (expires_at <= now) or revoked (is_revoked == True). Triggered
  after each successful login and via `python -m app.session_lifecycle`.

Never logs session tokens, passwords, or wecom identity beyond what the
existing auth module already logs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import AdminSession, AdminUser
from app.db.session import get_engine

logger = logging.getLogger(__name__)

# Only write last_active_at if it's been at least this long since the last
# touch — avoids hammering updated_at on every request.
LAST_ACTIVE_TOUCH_INTERVAL_SECONDS = 300


def touch_last_active(user, db: Session, now: datetime | None = None) -> None:
    """Update user.last_active_at if throttled window elapsed (or never set).

    Uses a single conditional UPDATE so it's idempotent and race-free: the
    write happens only when (last_active_at IS NULL) OR
    (last_active_at < now - interval). Commits on the passed request db.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=LAST_ACTIVE_TOUCH_INTERVAL_SECONDS)
    db.execute(
        update(AdminUser)
        .where(AdminUser.id == user.id)
        .where(AdminUser.last_active_at.is_(None) | (AdminUser.last_active_at < cutoff))
        .values(last_active_at=now)
    )
    db.commit()


def cleanup_expired_sessions(db: Session, now: datetime | None = None) -> int:
    """Delete admin_sessions rows that are expired or revoked. Returns count."""
    now = now or datetime.now(timezone.utc)
    deleted = (
        db.query(AdminSession)
        .filter(
            (AdminSession.expires_at <= now) | (AdminSession.is_revoked.is_(True))
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        logger.info("cleanup_expired_sessions: removed %d row(s)", deleted)
    return deleted


if __name__ == "__main__":
    # Ops entry point: `python -m app.session_lifecycle` clears a batch.
    # Requires DATABASE_URL (get_engine() raises if unset).
    engine = get_engine()
    with Session(engine) as db:
        count = cleanup_expired_sessions(db)
    print(f"cleanup_expired_sessions: removed {count} expired/revoked session(s)")
```

### 步骤 2 — `app/auth.py` 改写
- 顶部 `L45` 把 `SESSION_TTL_HOURS = 8` 改为保留为默认值常量（避免破坏其它引用），并新增：
  ```python
  DEFAULT_SESSION_TTL_HOURS = 8

  def get_session_ttl_hours() -> int:
      """Read SESSION_TTL_HOURS env via AuthSettings; default 8, clamp [1, 8760]."""
      raw = get_auth_settings().session_ttl_hours.strip()
      if not raw:
          return DEFAULT_SESSION_TTL_HOURS
      try:
          val = int(raw)
      except ValueError:
          logger.warning("SESSION_TTL_HOURS=%r not an int; using default %d", raw, DEFAULT_SESSION_TTL_HOURS)
          return DEFAULT_SESSION_TTL_HOURS
      return max(1, min(8760, val))
  ```
  （`get_auth_settings` 已在 `auth.py` 导入；`logger` 已存在。）
- 在 `get_current_user`（取 `user` 之后、`return user, session.tenant_id` 之前）插入：
  ```python
  from app.session_lifecycle import touch_last_active
  touch_last_active(user, db)
  ```
  建议放在 `try: user = db.query(AdminUser)...` 成功分支内、`return` 前（user 非空时）。
- 在 `require_html_session`（确认 `session is not None` 分支内、`return session.tenant_id` 之前）插入取 user + touch：
  ```python
  from app.session_lifecycle import touch_last_active
  _u = db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
  if _u is not None:
      touch_last_active(_u, db)
  ```
  （为避免循环导入，`from app.session_lifecycle import ...` 可放在模块顶部统一导入；`session_lifecycle` 不反向 import `auth`，无环。）

### 步骤 3 — `app/settings.py` 新增字段
`AuthSettings`（`L44-52`）内追加 `session_ttl_hours: str = ""`。

### 步骤 4 — `app/routers/auth.py` 改写
- 顶部 import（约 `L41`）把 `SESSION_TTL_HOURS` 换成 `get_session_ttl_hours`。
- `password_login`：`expires_at = now + timedelta(hours=get_session_ttl_hours())`；`max_age=get_session_ttl_hours() * 3600`。
- `wecom_callback`：同样两处替换为 `get_session_ttl_hours()`。
- 在两条登录路径的 `db.commit()` 附近插入 `from app.session_lifecycle import cleanup_expired_sessions; cleanup_expired_sessions(db)`（幂等，可放在 `db.add(session)` 之后、`db.commit()` 之前或之后均可）。

### 步骤 5 — 注册架构边界模块
`backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（`L70-93`）加入 `"app.session_lifecycle",`（与 `app.auth` 同组）。

### 步骤 6 — 新增测试 `backend/tests/test_rnd279_session_lifecycle.py`
参照 `test_tenant_foundation.py` 的 `_DB_AVAILABLE` 门控模式（无 `DATABASE_URL` 时 DB 用例 skip；`make verify` 默认无 DB 也会绿）。覆盖：
- **配置单测（无 DB）**：`get_session_ttl_hours()` —— env 未设→8；`"12"`→12；`"0"`→1；`"99999"`→8760；`"-3"`→1；`"abc"`→8（warning）。用 `monkeypatch.setenv("SESSION_TTL_HOURS", ...)`。
- **节流单测（无 DB，用 fake db/user）**：构造一个带 `last_active_at` 的假 `AdminUser` 与记录 execute/commit 的 fake `Session`，断言首次调用执行 UPDATE、立即二次调用（now 未推进）**不**执行 UPDATE（被 `cutoff` 挡住）。
- **DB 门控集成**：
  - 建 `AdminUser` + 若干 `AdminSession`（1 个有效未过期、1 个 `expires_at` 在过去、1 个 `is_revoked=True`）→ 调 `cleanup_expired_sessions(db)` → 断言过期/吊销被删、有效保留、返回计数=2。
  - 建 `AdminUser`（`last_active_at=None`）→ 调 `touch_last_active` → 断言 `last_active_at` 近似 `now`；立即再调（同 `now`）→ 断言 `last_active_at` **不变**（节流）；用 `now + 400s` 再调 → 断言更新。
  - 断言 `touch_last_active` **未**改动 `last_login_at`（取另一行先设 `last_login_at`，touch 后该值不变）。

## 六、阶段三验证（RED/GREEN 记录）

RED 基线（改前）：
```bash
cd backend
grep -n "SESSION_TTL_HOURS = 8" app/auth.py                 # L45 硬编码常量
grep -n "SESSION_TTL_HOURS" app/routers/auth.py             # L41,302,324,540,568
python -c "import app.session_lifecycle" 2>&1 | head -1      # ModuleNotFoundError（模块未建）
grep -c "session_lifecycle" tests/test_architecture_boundary.py  # 0
python -c "from app.db.models import AdminSession; print('last_active_at' in [c.name for c in AdminSession.__table__.columns])"  # False（确认会话表无该列，正确）
```

GREEN（改后）：
```bash
cd backend
python -c "import app.session_lifecycle; print('module ok')"
python -c "from app.db.models import AdminUser; assert 'last_active_at' in [c.name for c in AdminUser.__table__.columns]"
python -m app.session_lifecycle 2>&1 | head -1            # 需 DATABASE_URL；输出 cleanup 行
make verify                                                # lint-diff typecheck build test 全绿（含 test_rnd279_session_lifecycle.py）
git diff --stat                                            # 应仅含 app/session_lifecycle.py + app/auth.py + app/settings.py + app/routers/auth.py + 架构边界测试 + 新测试文件
```

### 第六·一节 — `.env.example` / B 层纪律提示
`SESSION_TTL_HOURS` 是本项目新增 env 配置。项目发布纪律（RND-242/RND-237）将 `.env.example` 列为 B 层生产路径、功能票不改动。本票处理：
- `settings.py` 改动属于 app 代码，**在范围内**，正常改。
- `.env.example`：**不要改**（B 层）。若需在文档标注该变量，仅可在 `docs/`（非 B 层，如 `docs/DEPLOYMENT.md` 列 env 处）追加一行 `SESSION_TTL_HOURS`（默认 8，会话有效期小时数）说明；如 `docs/` 无可落点则跳过，**不要**为加注释去碰 `.env.example`。
- `backend/scripts/`、`deploy.yml`、systemd 单元名、`/srv/apps/wecom-archive-365`：一律不碰。

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- F0-1 未落地（第二节断言失败）时不自行改 `models.py` / 加迁移 / 改鉴权；停下报告。
- **不新建任何表、不新增 Alembic 迁移**。`AdminSession` 与 `AdminUser.last_active_at` 已存在；如认为需要 schema 变更，停下报告。
- `touch_last_active` 只写 `AdminUser.last_active_at`，**绝不**写 `last_login_at`；节流 300s。
- 不改现有登录路由 `password_login` / `wecom_login` / `wecom_callback` / `auth_me` / `logout` 的行为、URL、cookie 标志、404/500 守卫；清理只作为新增调用插入，不删改既有路由体。
- 不引入新第三方依赖（纯 stdlib + sqlalchemy）。
- 不碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd 单元名、`deploy.yml`、`backend/scripts`）；`.env.example` 仅按第六·一节处理（docs 注释可选，不改动文件）。
- `backend/tests/test_architecture_boundary.py` 必须仍 PASS（已把 `app.session_lifecycle` 加入 `_FLAT_SERVICE_MODULES`）；`app.main.py` 不得新增任何路由；`session_lifecycle.py` 不得 `import app.routers.*` / `app.main`。
- 安全延续：不得把 session token / 密码 / wecom 身份写入日志（沿用现有 `auth.py` 的脱敏纪律）。

## 八、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应含 `app/session_lifecycle.py`(新) + `app/auth.py` + `app/settings.py` + `app/routers/auth.py` + `tests/test_architecture_boundary.py` + 新测试 `test_rnd279_session_lifecycle.py`，**无迁移文件**）、`make verify` 全绿日志、未提交声明。
