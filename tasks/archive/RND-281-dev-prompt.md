# RND-281 开发 agent 执行提示词 —— A1-1 UsageService 聚合模块

> 面向开发 agent（单人端到端实现 RND-281）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

新建 `backend/app/services/usageservice.py`，提供 5 个**纯聚合函数**（真实 SQL 聚合，只读现有表），供 dashboard（A1-2）、analytics（A2-1）、平台总控台（B1-3）复用。**不实现任何路由/端点、不改 schema、不加迁移、不写任何 F0 鉴权代码**。最终 `make verify` 全绿。

## 二、决策背景（已全部拍板，不要再问）

来源：Epic `RND-262`（A1 概览首页/Dashboard）+ 子任务 `RND-281`（A1-1）+ planner 设计文档 `deliverables/planner-prompt-linear-tickets-2026-07-28.md`（L47 A1 后端 Scope）。

- **本票只建聚合模块**，dashboard UI / 列表 API（A1-2）、趋势图表（A2-1）、跨租户聚合（B1-3）是独立 ticket，**严禁**在本票实现端点或前端。
- **Non-goals（明确排除）**：不含审计（见 A7 / RND-293）；不含外部联系人（A4）；不写任何 `AuditLog`（A7-1 另行建）；不引入路由、不引 FastAPI `Depends`、不碰 `app/main.py`、不加 Alembic 迁移（无 schema 变更）。
- **可空 `tenant_id` 以支持跨租户复用（关键）**：planner B1-3 明确「跨租户聚合（复用 `UsageService`，tenant=None）」。因此**每个函数都必须接受 `tenant_id: Optional[str] = None`**；当为 `None` 时**不加 tenant 过滤**，对全量数据聚合。这是本票能被 B1-3 直接复用的前提，务必实现。
- **F0 依赖是「软」的**：本票只聚合现有表（`archive_messages` / `media_files` / `contacts` / `sync_states` / `tenants`），全部已存在、与 F0 无关。Linear 上 `[BLOCKED: F0]` 是从 Epic 继承的状态（dashboard 端点需要 F0 鉴权），**A1-1 自身无 F0 代码依赖**——不要在此引入 `get_current_user` / 鉴权 / `app.auth` 的任何调用，tenant 由调用方以参数传入。
- **`msgtime` 单位已确认 = epoch 毫秒**（依据 `html_helpers.py:25` `ms/1000`、`structured_message_parser.py:208`「expect milliseconds」、`revoke_reconciliation.py:363` `msgtime_ms/1000`）。换算用 `to_timestamp(msgtime/1000)`（Postgres）或 Python `datetime.fromtimestamp(msgtime/1000, tz=timezone.utc)`。
- **工程纪律**：Agent 不 git commit/push；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。代码标识符加反引号。

## 三、项目现状（基线，2026-07-28 扫描）

### 3.1 目标文件尚未存在
- `backend/app/services/usageservice.py` **不存在**（glob `backend/app/services/**/*.py` 无此文件）。本票从零新建。
- `app/services/` 包已存在（`listing_service.py` / `timeline_service.py` / `media_access.py` 等），本票文件放入该包。

### 3.2 架构边界（务必遵守，详见 `backend/tests/test_architecture_boundary.py`）
- `app/services` 在 `_LAYER_PACKAGES` 中归类为 `"service"` 层（`test_architecture_boundary.py:55-60`）。**因此 `backend/app/services/usageservice.py` 自动被归为 service 层，无需、也千万不要**把它加进 `_FLAT_SERVICE_MODULES`（那是给 `app/` 根下「扁平模块」如 `app.email` 用的；本票文件在包内，规则不同）。
- 反向依赖规则（`find_reverse_dependency_violations`）：service 层**不得** `import app.routers.*` 或 `import app.main`。本票只 `import app.db.models` + `sqlalchemy` + 标准库（`datetime` 等），完全合规。
- 本票**无 schema 变更** → 不触发 `alembic check`；但 `make verify` 含 `test_architecture_boundary.py`，新文件会被扫描，必须零违规。

### 3.3 现有模型（聚合来源，列名精确）
- `Tenant`（`models.py:30`）：`id`(String36 PK) / `created_at`(DateTime tz)。`get_archived_days` 用 `created_at` 作起点锚。
- `ArchiveMessage`（`models.py:305`，表 `archive_messages`）：`id`(BigInteger PK) / `tenant_id`(String36, FK tenants, 可空, 索引) / `msgid` / `msgtime`(BigInteger, **epoch-ms**, 索引) / `is_revoked`(Boolean) / `created_at`。
- `MediaFile`（`models.py:461`，表 `media_files`）：`id` / `tenant_id`(可空, 索引) / `file_size`(BigInteger, **可空**)。
- `Contact`（`models.py:729`，表 `contacts`）：`id` / `wecom_userid`(String64) / `tenant_id`(可空, 索引) / `name`。即「被监控员工/WeCom 用户身份登记」，去重键 = `wecom_userid`。
- `SyncState`（`models.py:272`，表 `sync_states`）：`id` / `tenant_id`(可空, 索引) / `status`(Enum `sync_state_status` = `idle`/`syncing`/`error`, **非空**) / `error_message`(Text, 可空) / `last_seq`(BigInteger) / `updated_at`(DateTime tz)。
- 注意：上述表的 `tenant_id` 均为**可空**（历史迁移遗留，`models.py` 注释「nullable during migration, backfilled」）。聚合时 `tenant_id == tenant_id` 过滤即正确租户隔离；`tenant_id=None` 时不过滤 → 全量。

### 3.4 服务函数风格（对齐 `listing_service.py`）
- `listing_service.py:39` 起的函数签名均为 `(db: Session, entity_id: str, tenant_id: str)` 风格，纯函数、无副作用、用 SQLAlchemy `select`/`func`。本票沿用：`(db: Session, tenant_id: Optional[str] = None)`。

## 四、目标实现（精确落点）

### 4.1 文件：`backend/app/services/usageservice.py`

```python
"""UsageService (RND-281 / A1-1) — tenant-scoped aggregation for the
dashboard / analytics / platform-console.

Every function takes an optional ``tenant_id``: when ``None`` the query is
run across ALL tenants (cross-tenant aggregation, reused by B1-3). No
routing, no schema change, no F0/auth code — pure read-only aggregation.

``msgtime`` on ArchiveMessage is epoch-MILLISECONDS (see
html_helpers.py / structured_message_parser.py); divide by 1000 before
converting to a timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Date, func, select
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, Contact, MediaFile, SyncState, Tenant

# SyncState.status enum values (models.py: SyncState.status).
_SYNC_ERROR = "error"
_SYNC_SYNCING = "syncing"


def count_messages(db: Session, tenant_id: Optional[str] = None) -> int:
    """Total archived messages for the tenant (or all tenants)."""
    stmt = select(func.count(ArchiveMessage.id))
    if tenant_id is not None:
        stmt = stmt.where(ArchiveMessage.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def sum_storage(db: Session, tenant_id: Optional[str] = None) -> int:
    """Total media bytes stored (sum of media_files.file_size, in bytes).

    Scoped to MEDIA bytes only. The planner's "+ DB 估算" (per-tenant DB
    size estimate) is deliberately OUT OF SCOPE for A1-1 — it needs a
    separate, potentially expensive approach and belongs in A1-2 if
    wanted. file_size is nullable, so coalesce to 0.
    """
    stmt = select(func.coalesce(func.sum(MediaFile.file_size), 0))
    if tenant_id is not None:
        stmt = stmt.where(MediaFile.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def count_monitored_employees(db: Session, tenant_id: Optional[str] = None) -> int:
    """Distinct monitored WeCom employees (dedup on contacts.wecom_userid)."""
    stmt = select(func.count(func.distinct(Contact.wecom_userid)))
    if tenant_id is not None:
        stmt = stmt.where(Contact.tenant_id == tenant_id)
    return int(db.execute(stmt).scalar() or 0)


def get_archived_days(db: Session, tenant_id: Optional[str] = None) -> int:
    """Number of archived days.

    Per-tenant (tenant_id given): span from the tenant's ``created_at`` to
    the first archived message's msgtime (or ``now`` when the tenant has no
    messages yet) — literal reading of the planner spec
    "tenant 创建→首条消息或 now".

    Global (tenant_id is None): number of DISTINCT calendar days across all
    messages (no single tenant anchor exists at platform scope).

    Both return an int >= 0.
    """
    if tenant_id is None:
        stmt = select(
            func.count(
                func.distinct(
                    func.to_timestamp(ArchiveMessage.msgtime / 1000.0).cast(Date)
                )
            )
        ).where(ArchiveMessage.msgtime.isnot(None))
        return int(db.execute(stmt).scalar() or 0)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None or tenant.created_at is None:
        return 0
    start = tenant.created_at

    first_msg = db.execute(
        select(func.min(ArchiveMessage.msgtime)).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime.isnot(None),
        )
    ).scalar()
    end = (
        datetime.fromtimestamp(first_msg / 1000.0, tz=timezone.utc)
        if first_msg
        else datetime.now(timezone.utc)
    )
    return max(0, (end.date() - start.date()).days)


def sync_health(db: Session, tenant_id: Optional[str] = None) -> dict:
    """Aggregate SyncState health for the tenant (or all tenants).

    Returns a dict: {status, error_message, last_seq, updated_at}.
      status: "error" if any row errors, else "syncing" if any syncing,
              else "idle"; "unknown" when there are no rows at all.
    """
    stmt = select(SyncState)
    if tenant_id is not None:
        stmt = stmt.where(SyncState.tenant_id == tenant_id)
    rows = db.execute(stmt).scalars().all()
    if not rows:
        return {
            "status": "unknown",
            "error_message": None,
            "last_seq": None,
            "updated_at": None,
        }

    statuses = {r.status for r in rows}
    if _SYNC_ERROR in statuses:
        overall = _SYNC_ERROR
    elif _SYNC_SYNCING in statuses:
        overall = _SYNC_SYNCING
    else:
        overall = "idle"

    error_row = next((r for r in rows if r.status == _SYNC_ERROR), None)
    last_seq = max((r.last_seq for r in rows), default=None)
    updated_ats = [r.updated_at for r in rows if r.updated_at is not None]
    updated_at = max(updated_ats).isoformat() if updated_ats else None

    return {
        "status": overall,
        "error_message": error_row.error_message if error_row else None,
        "last_seq": last_seq,
        "updated_at": updated_at,
    }
```

### 4.2 语义决策（写进代码，避免返工）
- `count_messages`：统计**全部** `archive_messages`（含 `is_revoked=True`）。若 A1-2 后续要排除撤回消息，加 `where(is_revoked == False)` 即可——本票默认「总数」。
- `sum_storage`：仅 media 字节（`file_size` 求和，coalesce 0）。**不含** DB 体积估算（planner 提到的「+ DB 估算」属 A1-2 范畴，本票不做）。
- `count_monitored_employees`：`count(distinct contacts.wecom_userid)`（去重），非 `count(*)`。
- `get_archived_days`：见 4.1 docstring。**若 A1-2 团队认定「归档天数」应为「有消息覆盖的不同日历天数」(distinct days) 而非「租户创建→首条消息跨度」，只需把 per-tenant 分支也改用 `count(distinct date(to_timestamp(msgtime/1000)))`，二者仅一行差异**——本票默认采用 planner 字面语义。
- `sync_health`：多 `SyncState` 行（每 corp 一行）聚合为单值；`error` 优先于 `syncing` 优先于 `idle`；无行 → `unknown`。

### 4.3 导入（无需新增 app 层注册）
- 仅 `from app.db.models import ArchiveMessage, Contact, MediaFile, SyncState, Tenant` + `from sqlalchemy import Date, func, select` + `from sqlalchemy.orm import Session` + 标准库 `datetime`/`typing`。**不 import `app.routers.*` / `app.main`**（架构边界硬约束）。
- 文件置于 `app/services/` 包内 → 自动归 service 层；**不要**把它加入 `test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES`（那是 `app/` 扁平模块专用，本文件不适用）。

## 五、验收门槛（实现后自查）

- [ ] `cd backend && make verify` 全绿（lint-diff → typecheck → build → test，含 `test_architecture_boundary.py`：新文件零反向依赖）。
- [ ] `python -c "from app.services import usageservice as u; print([f for f in ('count_messages','sum_storage','count_monitored_employees','get_archived_days','sync_health') if hasattr(u, f)])"` 五个函数均可导入。

## 六、范围守门（本票只建：一个 service 模块 + 一个测试文件）

- [ ] `git diff --name-only` 仅含：`backend/app/services/usageservice.py` + `backend/tests/test_rnd281_usageservice.py`。
- [ ] **不**新增/修改任何 `routers/*.py`（无 dashboard/usage 端点——属 A1-2）。
- [ ] **不**新增 Alembic 迁移 / 不改 `models.py`（无 schema 变更）。
- [ ] **不**引入 FastAPI `Depends` / `get_current_user` / `app.auth` 鉴权（属 F0/A1-2）。
- [ ] **不**写 `AuditLog` 或任何审计逻辑（属 A7）。
- [ ] **不**把本模块加入 `_FLAT_SERVICE_MODULES`。
- [ ] **不** git commit / push（由用户本人操作）。

## 七、测试（新建 `backend/tests/test_rnd281_usageservice.py`）

沿用 `test_rnd277_admin_user_extension.py` 的 `_DB_AVAILABLE` 门控风格：

- 顶部：`_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))`。
- **无 DB 冒烟测试（必跑，不门控）**：`test_module_import_and_functions_present` —— `from app.services import usageservice` 成功，且五个函数可调用（签名含 `db, tenant_id=None`）。这保证 `make verify` 在无 DB 环境也绿。
- **DB 支撑测试（`@pytest.mark.skipif(not _DB_AVAILABLE, ...)`）**：用 `create_engine(DATABASE_URL)` + `Session`：
  1. 建一个 `Tenant`（记录 `created_at` 为确定值，如 `datetime(2026,1,1,tzinfo=utc)`）。
  2. 插 N 条 `ArchiveMessage`：跨多个不同日历日（用 epoch-ms 的 `msgtime`，如 2026-01-01/01-03/01-03 三天各若干条），`tenant_id` 设为该租户；另插 1 条 `tenant_id=None`（验证不过滤时影响全局聚合）。
  3. 插 M 条 `MediaFile`：`file_size` 已知值（含一个 NULL 验证 coalesce 0），部分 `tenant_id=None`。
  4. 插若干 `Contact`：`wecom_userid` 含重复（验证 distinct 去重），部分 `tenant_id=None`。
  5. 插 `SyncState`：该租户 2 行（一行 `status='error'` 带 `error_message`、一行 `status='syncing'`），全局另插 1 行 `status='idle'`。
  6. 断言：
     - `count_messages(db, tenant_id)` == N（仅该租户）；`count_messages(db, None)` == N+1（含 NULL 租户行）。
     - `sum_storage(db, tenant_id)` == 该租户 media 字节和；`sum_storage(db, None)` == 全量和（NULL 行的 file_size 若为 NULL 被 coalesce 0 忽略）。
     - `count_monitored_employees(db, tenant_id)` == 该租户 distinct wecom_userid 数；`(db, None)` == 全量 distinct。
     - `get_archived_days(db, tenant_id)` == `max(0, (first_msg_date - tenant.created_at.date()).days)`（用确定值断言）；`get_archived_days(db, None)` == 全量消息 distinct 日历天数。
     - `sync_health(db, tenant_id)` == `{"status":"error","error_message":<该行>,"last_seq":<max>,"updated_at":<max iso>}`；`sync_health(db, None)` == 整体 `error`（因含 error 行）。
  7. `finally` 中清理（DELETE 测试 tenant/messages/media/contacts/syncstates）。

> 回归点：所有聚合 SQL 必须走 `select`/`func`，不得出现 `db.query(...)` 之外的 ORM 反模式；`msgtime` 换算统一 ÷1000。
