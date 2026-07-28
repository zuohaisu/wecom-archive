# RND-284 开发执行提示词（A3-1 用户列表/筛选 API）

> 本文件是交给**开发 agent** 的端到端实现 brief。请严格按"落点 → RED→GREEN → 硬约束"执行。
> 工程纪律（全局）：Agent **绝不 `git commit` / `git push`**；实现后交独立 QA agent 验收，由用户决定是否提交。架构冻结 **D1：复用 SSR + 原生 JS，不引 React**（本票为纯后端 API，不涉及前端）。代码标识符一律加反引号。

---

## 0. 任务身份与结论

- **Linear**: `RND-284` — `[BLOCKED: F0] A3-1 用户列表/筛选 API`
- **Parent**: `RND-273` Epic · A3 用户管理
- **类型 / 优先级**: feature / Medium (3)
- **现状判定**: **未实现**（代码核查确认，见 §1）。应**创建**本开发 + QA 双提示词。
- **范围**: 纯后端。新增 `GET /api/admin/users`，列 `admin_users`（角色 / 状态 / 最后活跃 / 近 30 天消息数），含分页与筛选。**不含**前端页面（设计稿 `design/ui-v1/pages/users.html` 仅作字段参照；前端接线是 `RND-273` 下其他子票职责，本票 Non-goals 明确"不改角色逻辑"）。
- **依赖 `F0` 状态**: F0 的数据模型前提已满足 —— `AdminUser.role` / `status` / `last_active_at` 三列均已随 **F0-1（RND-277，已合并）** 落地；F0-2（RND-276 per-user 密码鉴权，已合并）亦不影响本票只读聚合。故 `[BLOCKED: F0]` 对数据模型**已不构成硬阻塞**，可直接开工。若执行中发现 `last_active_at` 实际未被维护（极端情况），属数据质量范畴、非阻塞——端点仍返回已存储值（可能为 `null`），并如实报告。

---

## 1. 现状核查（为什么判定未实现）

| 检查项 | 结果 |
|---|---|
| `GET /api/admin/users` 路由 | **不存在**（全仓 grep `api/admin/users` / `/admin/user` 零命中） |
| `users` router 文件 | **不存在**（`app/routers/` 无 `users.py`，`main.py` 未注册） |
| `AdminUser` 模型字段 | `id` / `tenant_id` / `wecom_user_id` / `name` / `email` / `department` / `role`(枚举 owner/admin/compliance/legal/readonlyaudit) / `status`(枚举 active/disabled) / `last_active_at` / `avatar_url` —— **全部已存在**（`app/db/models.py:132` 起，role/status/last_active_at 为 RND-277 F0-1 增加） |
| 近 30 天消息数来源 | `ArchiveMessage.sender`(`app/db/models.py:98`) + `tenant_id`(`app/db/models.py:114`) + `msgtime`(毫秒 epoch, `app/db/models.py:100`) —— 需**新写聚合**，无现成 |
| 路由基线 | `tests/test_http_contract.py:325` 当前 `assert route_count == 42`（含 RND-321 +2）。本票 +1 → 须改 `== 43` |
| migration | **无需**（所有列已存在） |

**结论**：端点与聚合逻辑均缺失，但底层数据列齐备 → 本票是"拼装 + 1 个新聚合查询"，无 schema 变更、无前端。

---

## 2. 精确落点（`文件:行号:函数`）

| 落点 | 动作 |
|---|---|
| `backend/app/routers/users.py`（**新建**） | 定义 `users_router = APIRouter()` + `GET /api/admin/users` 处理函数 |
| `backend/app/main.py`（注册区，约 `:99` 后） | `app.include_router(users_router, prefix="/api/admin")` |
| `backend/app/schemas/admin_users.py`（**新建**） | Pydantic 响应模型 `AdminUserListItem` + `AdminUserListOut`（信封） |
| `backend/app/auth.py:319` `get_current_user` | 依赖注入，返回 `(AdminUser, tenant_id)` —— **唯一授权作用域** |
| `backend/app/db/models.py` `AdminUser`（`role`/`status`/`last_active_at`）、`ArchiveMessage`（`sender`/`tenant_id`/`msgtime`） | 只读引用，不修改 |
| `backend/tests/test_http_contract.py:325` | `route_count == 42` → `== 43`（注释 `# RND-284: +1 admin users list route.`） |

> 聚合查询可内联于 router（单端点，风险最低），也可遵循 RND-219 约定放入 `app/services/listing_service.py`（新增 `list_admin_users`）。**推荐内联于 router**以减小改动面；若团队偏好 RND-219 约定，移到 `listing_service` 亦可（须同步在 router 内调用）。

---

## 3. 实现方案（RED → GREEN）

### 3.1 响应模型（`app/schemas/admin_users.py`）
参照 `app/schemas/listing.py` 的 `MonitoredAccountOut` / `ContactOut` 风格：
```python
from pydantic import BaseModel
from typing import Optional, Literal

Role = Literal["owner","admin","compliance","legal","readonlyaudit"]
UserStatus = Literal["active","disabled"]

class AdminUserListItem(BaseModel):
    id: str
    name: Optional[str] = None
    wecom_user_id: str          # 设计稿"账号"列
    email: Optional[str] = None
    department: Optional[str] = None
    role: Role
    status: UserStatus
    last_active_at: Optional[str] = None   # ISO-8601，null 允许
    msg_count_30d: int = 0

class AdminUserListOut(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    per_page: int
```

### 3.2 端点（`app/routers/users.py`）
```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from datetime import datetime, timedelta, timezone
from app.auth import get_current_user
from app.db.models import AdminUser, ArchiveMessage
from app.db.session import get_db
from app.schemas.admin_users import AdminUserListItem, AdminUserListOut

users_router = APIRouter()

@users_router.get("/api/admin/users", response_model=AdminUserListOut)
def list_admin_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(None, pattern="^(owner|admin|compliance|legal|readonlyaudit)$"),
    status: Optional[str] = Query(None, pattern="^(active|disabled)$"),
    q: Optional[str] = Query(None, max_length=128),
    silent_days: Optional[int] = Query(None, ge=1),   # 派生"静默"筛选（可选/次要）
    auth: tuple = Depends(get_current_user),
    db = Depends(get_db),
):
    _, tenant_id = auth                     # 唯一授权作用域，绝不接受请求体里的 tenant_id
    stmt = db.query(AdminUser).filter(AdminUser.tenant_id == tenant_id)
    if role:
        stmt = stmt.filter(AdminUser.role == role)
    if status:
        stmt = stmt.filter(AdminUser.status == status)
    if silent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=silent_days)
        stmt = stmt.filter(AdminUser.last_active_at < cutoff)
    if q:
        like = f"%{q}%"
        stmt = stmt.filter(or_(
            AdminUser.name.ilike(like),
            AdminUser.wecom_user_id.ilike(like),
            AdminUser.email.ilike(like),
            AdminUser.department.ilike(like),
        ))
    total = stmt.with_entities(func.count(AdminUser.id)).scalar() or 0
    rows = (stmt
            .order_by(AdminUser.last_active_at.desc().nullslast(), AdminUser.name.asc())
            .offset((page - 1) * per_page).limit(per_page).all())

    # —— 近 30 天消息数：单条分组聚合，避免 N+1 ——
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
    agg = (db.query(ArchiveMessage.sender, func.count(ArchiveMessage.id))
           .filter(ArchiveMessage.tenant_id == tenant_id,
                   ArchiveMessage.msgtime >= cutoff_ms)
           .group_by(ArchiveMessage.sender).all())
    msg_counts = {sender: cnt for sender, cnt in agg}

    items = [AdminUserListItem(
        id=u.id, name=u.name, wecom_user_id=u.wecom_user_id, email=u.email,
        department=u.department, role=u.role, status=u.status,
        last_active_at=u.last_active_at.isoformat() if u.last_active_at else None,
        msg_count_30d=msg_counts.get(u.wecom_user_id, 0),
    ) for u in rows]
    return AdminUserListOut(items=items, total=total, page=page, per_page=per_page)
```

### 3.3 注册（`app/main.py`）
在 `app.include_router(sync_router, prefix="/api/admin")`（约 `:99`）之后追加：
```python
from app.routers.users import users_router
app.include_router(users_router, prefix="/api/admin")
```

### 3.4 路由基线（`tests/test_http_contract.py:325`）
`assert route_count == 42  # RND-321...` → `assert route_count == 43  # RND-284: +1 admin users list route.`

---

## 4. 字段口径（与 `design/ui-v1/pages/users.html` 对齐）

| 设计稿列 | API 字段 | 来源 |
|---|---|---|
| 姓名 / 账号 | `name` / `wecom_user_id` | `AdminUser` |
| 部门 | `department` | `AdminUser` |
| 角色 | `role` | `AdminUser.role` 枚举 |
| 存档状态 | `status` | `AdminUser.status` 枚举（active/disabled）—— 即验收标准中的"状态" |
| 最后活跃 | `last_active_at` | `AdminUser.last_active_at`（ISO-8601，可 null） |
| 近 30 天消息 | `msg_count_30d` | `ArchiveMessage` 聚合：`tenant_id` + `sender == wecom_user_id` + `msgtime >= now-30d`（毫秒） |

> **口径声明（写进代码注释与交付报告）**：`msg_count_30d` 默认定义为"该员工**发出**的归档消息数"（`sender` 匹配）。若后续产品要求含接收消息，须扩展为 `sender` 或 `tolist` 包含（成本高，非本票范围）。当前采用 `sender` 口径。

---

## 5. 硬约束（不可妥协）

1. **租户隔离 fail-closed**：所有查询（`AdminUser` 列表 + `ArchiveMessage` 聚合）必须 `tenant_id == get_current_user` 返回的 `tenant_id`。**绝不**接受请求参数里的 `tenant_id`。零跨租户泄露。
2. **SF-1 数据最小化**：`msg_count_30d` 仅做行计数（`func.count(id)`），**绝不**读取 `content_text` / `decrypted_payload` / `structured_content`。合规。
3. **不改角色逻辑（Non-goals）**：本票只**读** `role`/`status`，不新增/修改角色枚举、不改权限判定、不做邀请/禁用写操作。禁用/邀请是 `RND-273` 其他子票职责。
4. **无 migration**：所有列已存在，禁止新增/修改表结构或 Alembic 迁移。
5. **路由基线**：`tests/test_http_contract.py:325` 必须同步 `== 43` 并注释 `RND-284`，否则 CI 失败。
6. **D1 冻结**：纯后端，不新增任何前端/React/路由页面；i18n 不需改动（前端页面属其他子票）。
7. **零泄露**：日志/响应不得包含 `password_hash`、`invite_token`、secret。
8. **分页上限**：`per_page` 上限 100；`q` 长度上限 128，并参数化防注入（SQLAlchemy `ilike` 已参数化）。
9. **代码标识符加反引号**（Linear markdown 约定）：如 `AdminUser`、`msg_count_30d`、`get_current_user`。

---

## 6. 验证（GREEN 判定）

- `make test`（或 `pytest backend/tests`）全绿，含 `test_http_contract.py::test_router_count`。
- 新增单测/集成测（`tests/test_admin_users_api.py` 或并入现有 admin API 测试）：
  - 租户 A 的用户不出现在租户 B 的响应（隔离）。
  - `role` / `status` / `q` / `silent_days` 筛选正确。
  - `msg_count_30d` 与直接 `func.count(ArchiveMessage...)` 手工核对一致（用 seed 数据）。
  - 分页 `total` / `page` / `per_page` 正确；越界 `page` 返回空列表不报错。
  - 未认证请求返回 401/重定向（依赖 `get_current_user`）。
- 手动：`curl -H "Cookie: ..." http://localhost:8000/api/admin/users?page=1&per_page=20` 返回信封 + 字段齐全。

---

## 7. 交付物（交用户）

实现后**不提交**，输出交付报告（见 `agent-dev-qa-brief` 格式）：
- 新增/修改文件清单（`app/routers/users.py`、`app/schemas/admin_users.py`、`app/main.py`、`tests/test_http_contract.py` + 新增测试）。
- `msg_count_30d` 口径声明。
- 测试结果摘要（通过数 / 失败数）。
- 未决项（如 `last_active_at` 实际填充情况）。
- 提示用户：由本人决定是否 `git commit` / 提交 QA。
