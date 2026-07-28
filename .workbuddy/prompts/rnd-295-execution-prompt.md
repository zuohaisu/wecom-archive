# RND-295 开发 agent 执行提示词 —— A7-3 审计列表/筛选/分页 API

> 面向开发 agent（单人端到端实现 RND-295）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS（本票无 SSR 页面改动）。

## 一、任务（一句话）

新增 `GET /api/admin/audit-logs`：返回**当前租户**的审计日志，支持按 `动作(action)` / `时间(created_at)` / `操作人(operator)` / 关键词(`q`) 筛选与 `limit`/`offset` 分页，**只读、不可篡改**。不新建任何写入/删除端点；不改动 `AuditLog` 表或 A7-1/A7-2 既有代码。

## 二、前置依赖（开工前必查，不满足 → 停下并报告）

本任务 **BLOCKED on A7-1**（RND-293，建 `audit_logs` 表）：

> ⚠️ 解释器与 DB 约定（同 RND-294）：后端依赖装在仓库根 `.venv`（Python 3.9），裸 `python` / 托管 python 3.13 不可用。`alembic check` 需要**可用**的 Postgres（`DATABASE_URL` 指向可达实例）；无可用 DB 时跳过，属环境限制而非代码问题，不要因此停下。

```bash
cd backend
PY=../.venv/bin/python
test -x "$PY" || { echo "venv 缺失 — 先在仓库根运行: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt alembic" >&2; exit 1; }
"$PY" -c "from app.db import models; c=[x.name for x in models.AuditLog.__table__.columns]; assert {'id','tenant_id','admin_user_id','action','object_type','object_id','detail','created_at'} <= set(c), c; assert 'updated_at' not in c; print('A7-1 OK', c)"
# alembic check 需可用 DB；仅当 DB 实际可达时才跑，红=越界改了 schema → 停下；不可达则跳过
if "$PY" -c "import os,psycopg2; psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=3).close()" >/dev/null 2>&1; then
  "$PY" -m alembic check   # 必须绿（Status: Success）；红 → 先判是否越界改了 schema
else
  echo "DATABASE_URL 未设或 DB 不可达 — 跳过 alembic check（环境限制，非代码问题；A7-1 是否已合并以 git log + 模型/迁移文件为准）"
fi
```

导入断言失败（`AuditLog` 导入报错 / `updated_at` 存在）→ **停下报告**，禁止自行建表 / 加迁移 / 改 A7-1（那属于 RND-293）。`alembic check` 仅在有可用 DB 时校验；无 DB 时跳过，不因此停下。
本票**不依赖 A7-2**（RND-294 写入钩子）——只读取 `audit_logs` 中已存在的行，无论由谁写入。

## 三、决策背景（已全部拍板，不要再问）

来源：Epic `RND-274`（A7 审计日志）+ 子任务 `RND-295`（A7-3）。

- **只读追加 / 不可篡改**：A7-1 已保证 `AuditLog` 在**应用层无 update/delete 路径**（无 `updated_at`、无写函数）。本票是**纯读取**端点，AC「不可篡改」= 端点仅 `GET`、绝不提供任何增/删/改入口、响应只是既存行的只读投影。设计稿 `audit-log.html` 顶部明确「日志为只读追加，任何人（含超管）不可删除或修改」——本票落地这条契约。
- **哈希链 seal 是 UI 展示特性，非本票范围**：设计稿的「链路完整 / head sha256 / 重新校验」seal 是前端展示元素；A7-1 schema **没有** `record_hash` 列，本票**不新增**哈希链/校验字段，也不实现「重新校验」端点（属独立后续票）。只保证返回既存不可变行。
- **租户隔离**：`tenant_id` 永远来自 `require_role()` 会话，绝不来自请求参数（同 `reachability_audit.py:81-84` 约定）。跨租户数据物理不可见。
- **角色门禁**：审计日志面向所有管理员角色（含 `readonlyaudit`「只读审计」角色，本就是为此设计），故用 `require_role()`（无参 = 全部 `ADMIN_ROLES`），比 `get_current_user` 略紧，契合敏感数据。
- **筛选维度对齐设计稿**：动作下拉（查看/检索/导出/配置变更/登录）、时间范围、操作人下拉（含「系统」= `admin_user_id IS NULL`）、关键词（操作人/对象/audit ID）。
- **工程纪律**：Agent 不 git commit/push；代码标识符加反引号；架构冻结 D1（本项目本票无前端改动，纯 API）。

## 四、项目现状（精确落点）

### 4.1 `AuditLog` 模型（A7-1 已定义，本票只读取）
`backend/app/db/models.py`（约 L232 之后）：`id`(String36 PK) / `tenant_id`(String36, FK→tenants.id, NOT NULL, idx) / `admin_user_id`(String36, FK→admin_users.id, **nullable**, idx) / `action`(Text, NOT NULL) / `object_type`(Text, NOT NULL) / `object_id`(Text, nullable) / `detail`(JSONB, nullable) / `created_at`(DateTime(timezone=True), NOT NULL, server_default=func.now())。**无 `updated_at`**。
- `AdminUser`（models.py:132）含 `name`/`email`（nullable）——用于 `operator`/`q` 的展示名与搜索（`admin_users` 上有 pg_trgm GIN 索引：`ix_admin_users_name_trgm`/`ix_admin_users_wecom_user_id_trgm`，ILIKE 走索引）。

### 4.2 现成范式（复用，不改语义）
- `backend/app/routers/reachability_audit.py`：**只读 + 租户隔离 + Query 筛选 + offset/limit + `has_more`** 的完美术式。`get_reachability_audit`（L65-96）用 `get_current_user` 取 `_, tenant_id = auth`；`ReachabilityAuditOut`（L50-62）为 pydantic `response_model`。本票端点照搬这套结构（仅把 `get_current_user` 换成 `require_role()`）。
- `backend/app/auth.py:378` `require_role(*allowed_roles)`：无参 → 允许全部 `ADMIN_ROLES`，返回 `(AdminUser, tenant_id)`。
- `backend/app/db/session.py` `get_db`；`backend/app/db/models.py` `AuditLog, AdminUser`。

### 4.3 路由注册（`backend/app/main.py`）
现有 `app.include_router(sync_router, prefix="/api/admin")`（L99）等。新路由**仿照**：
```python
from app.routers.audit import router as audit_router
...
app.include_router(audit_router, prefix="/api/admin")   # 与 sync_router 同前缀
```
→ 端点路径字面量 `/api/admin/audit-logs`。**`app/main.py` 不得内联任何业务路由。**

### 4.4 HTTP 契约测试（`backend/tests/test_http_contract.py`，必同步）
- L325 `assert route_count == 42`：本票 +1 路由 → **改为实际值**（运行 `make verify` 时该断言会报真实 count，抄过去即可；若 RND-286 等兄弟票已先合，count 会更高，以运行时为准）。
- L334-377 `test_routers_are_registered` 的 `expected` 集合：追加 `"/api/admin/audit-logs"`。
- L403-492 `test_route_snapshot_with_real_model_names` 的 `expected` 列表：追加 `("/api/admin/audit-logs", frozenset({"GET"}), "AuditLogListOut", "None")`（断言为 `sorted` 比较，顺序无关）。

### 4.5 架构边界（`backend/tests/test_architecture_boundary.py`）
新模块 `routers/audit.py` 是**路由**（非 flat service），**不**需加入 `_FLAT_SERVICE_MODULES`。但必须：不 `import app.routers.*` / `app.main`；仅 `import app.auth`/`app.db.models`/`app.db.session`/`app.settings`（如需）。

## 五、实现步骤（GREEN，最小变更）

### 步骤 1 — 新建 `backend/app/routers/audit.py`
```python
"""Audit-log list / filter / pagination API (RND-295 / A7-3).

Read-only, role-gated, tenant-scoped. The audit trail is immutable at the
application layer (A7-1): this endpoint NEVER mutates rows — no create /
update / delete path exists. `tenant_id` comes only from the authenticated
session, never a request param.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, AuditLog
from app.db.session import get_db

router = APIRouter()


class AuditLogOut(BaseModel):
    id: str
    admin_user_id: Optional[str] = None
    actor_name: Optional[str] = None          # 来自 AdminUser.name；系统动作=None
    action: str
    object_type: str
    object_id: Optional[str] = None
    detail: Optional[dict] = None             # 原样返回；A7-1 已保证不含消息正文
    created_at: str                           # ISO 8601（UTC，含 +00:00）


class AuditLogListOut(BaseModel):
    items: list[AuditLogOut]
    total: int
    limit: int
    offset: int
    has_more: bool


def _parse_ts(value: str) -> datetime:
    """ISO-8601 → tz-aware datetime；naive 视为 UTC。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("/audit-logs", response_model=AuditLogListOut)
def list_audit_logs(
    action: Optional[str] = Query(None, description="精确匹配；逗号分隔支持多值"),
    object_type: Optional[str] = Query(None),
    operator: Optional[str] = Query(
        None, description="admin_user_id 精确值；'system' = admin_user_id IS NULL"
    ),
    q: Optional[str] = Query(
        None, description="ILIKE 搜索 object_id / id / 操作人 name / email"
    ),
    from_: Optional[str] = Query(None, alias="from", description="created_at 下界 ISO-8601"),
    to: Optional[str] = Query(None, alias="to", description="created_at 上界 ISO-8601"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(require_role()),
):
    _, tenant_id = auth
    qry = db.query(AuditLog).filter(AuditLog.tenant_id == tenant_id)

    if action:
        acts = [a.strip() for a in action.split(",") if a.strip()]
        if acts:
            qry = qry.filter(AuditLog.action.in_(acts))
    if object_type:
        qry = qry.filter(AuditLog.object_type == object_type)
    if operator == "system":
        qry = qry.filter(AuditLog.admin_user_id.is_(None))
    elif operator:
        qry = qry.filter(AuditLog.admin_user_id == operator)

    if q:
        like = f"%{q}%"
        qry = qry.outerjoin(AdminUser, AdminUser.id == AuditLog.admin_user_id)
        qry = qry.filter(
            or_(
                AuditLog.object_id.ilike(like),
                AuditLog.id.ilike(like),
                AdminUser.name.ilike(like),
                AdminUser.email.ilike(like),
            )
        )
    if from_:
        qry = qry.filter(AuditLog.created_at >= _parse_ts(from_))
    if to:
        qry = qry.filter(AuditLog.created_at <= _parse_ts(to))

    total = qry.count()
    rows = (
        qry.order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    # 批量取操作人展示名，避免 N+1 且与上面的 outerjoin 解耦
    actor_ids = [r.admin_user_id for r in rows if r.admin_user_id]
    name_map = {}
    if actor_ids:
        names = (
            db.query(AdminUser.id, AdminUser.name)
            .filter(AdminUser.id.in_(actor_ids))
            .all()
        )
        name_map = {uid: nm for uid, nm in names}

    items = [
        AuditLogOut(
            id=r.id,
            admin_user_id=r.admin_user_id,
            actor_name=name_map.get(r.admin_user_id),
            action=r.action,
            object_type=r.object_type,
            object_id=r.object_id,
            detail=r.detail,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]
    return AuditLogListOut(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )
```

> 说明：`response_model` 名为 `AuditLogListOut`（与 `test_http_contract.py` 快照期望值一致）。`detail` 原样返回 JSONB（dict），A7-1 已保证其不含消息正文/decrypted_payload（SF-1）；本票不对其做内容校验。

### 步骤 2 — `backend/app/main.py` 注册（仿 L99 范式）
`from app.routers.audit import router as audit_router` + `app.include_router(audit_router, prefix="/api/admin")`。

### 步骤 3 — 同步 `backend/tests/test_http_contract.py`（三处，见 4.4）
`route_count` 改为实际值；`expected` 路径集合 + snapshot 列表各追加一条。

### 步骤 4 — 新建 `backend/tests/test_rnd295_audit_list.py`
沿用 `test_rnd293_audit_log.py` 风格（`DATABASE_URL` 门控，无 DB 自动 skip；参照 `test_password_auth.py` 的 `dependency_overrides[get_db]` 工厂做无 DB 路径）。覆盖：
- 同租户写入 N 条 `AuditLog`（含 `admin_user_id=None` 的系统动作 + 有 actor 的动作 + 多种 `action`/`object_type`/`created_at`/`object_id`/`detail`）→ `GET /api/admin/audit-logs` 返回 N 条、`total==N`、`has_more` 按 `limit/offset` 正确。
- **租户隔离**：建第二租户写入 M 条 → 当前租户查询 `total` 仍为 N（跨租户不可见）。
- **筛选**：`action=export` 仅返该 action；`operator=system` 仅返 `admin_user_id IS NULL`；`operator=<某 id>` 仅返该 actor；`q=<object_id 片段>` / `q=<actor name>` 命中；`from`/`to` 时间窗正确截断。
- **分页**：`limit=2&offset=0` → `has_more=True`、`items` 长度 2、`created_at` 降序；`offset=2` 取后半。
- **不可篡改 / 只读**：端点仅 `GET`（OPTIONS 除外无其它方法）；`audit_logs` 行数在调用前后不变（无副作用）。
- **角色门禁**：`readonlyaudit` 角色可访问（200）；未认证 → 401。
- **零内容泄露**：随机抽样 `detail` 字段 `grep` 确认不含消息正文/`decrypted_payload`/`msgid`（结构性断言；A7-1 已保证，这里做回归护栏）。

## 六、阶段三验证（RED/GREEN 记录）

RED 基线（改前）：
```bash
grep -rn "audit-logs" backend/app/routers/                         # 无结果
.venv/bin/python -c "from app.db import models; print('AuditLog' in dir(models))"   # 视 A7-1 是否已合（用仓库根 .venv 的 python）
cd backend && grep -n "route_count ==" tests/test_http_contract.py               # == 42
```

GREEN（改后）：
```bash
cd backend
../.venv/bin/python -c "from app.routers.audit import list_audit_logs; print('audit router ok')"
alembic check                                                          # 需可用 DB；无 DB 时跳过（同第二节约定，本票无 schema 变更）
make verify                                                            # lint-diff typecheck build test 全绿
# 重点：test_http_contract.py 三处已同步；test_rnd295_audit_list.py 全绿
```

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- A7-1 未落地（第二节断言失败）时不自行建表/加迁移/改 A7-1；停下报告。
- **只读、不可篡改**：端点仅 `GET`；不得新增任何 POST/PATCH/DELETE 或 `audit_logs` 的 write/delete 路径；响应只是既存行投影。
- `tenant_id` 仅来自 `require_role()` 会话，绝不来自请求参数（跨租户 404/不可见）。
- 不改 A7-1 的 `AuditLog` 模型 / 迁移；不改动 A7-2 的 `app/audit.py` 或任何写入钩子；不改 `password_login`/`wecom_*`/`auth_me` 等既有路由。
- 不新增 `record_hash` / 哈希链 / 「重新校验」端点（设计稿 seal 属 UI，非本票）。
- 不引 React；本票无前端页面改动（审计日志 SSR 控制台页面属前端票，非 A7-3）。
- 不碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd、`deploy.yml`、`backend/scripts`、`.env.example`）。
- `backend/tests/test_architecture_boundary.py` 必须仍 PASS（`app/main.py` 无内联路由；`routers/audit.py` 未 `import app.routers.*`/`app.main`）；`alembic check` 在有可用 DB 时必须绿（无 DB 环境跳过并记录）。

## 八、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应含 `app/routers/audit.py` + `app/main.py`(注册) + `test_http_contract.py`(三处) + 新测试 `test_rnd295_audit_list.py`）、`alembic check` 绿日志（若有可用 DB；无 DB 则注明「需 DB 环境复测」）、`make verify` 全绿日志、未提交声明。
