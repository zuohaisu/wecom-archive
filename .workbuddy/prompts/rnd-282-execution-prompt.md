# RND-282 开发 agent 执行提示词 —— A1-2 Dashboard 聚合统计 API

> 面向开发 agent（单人端到端实现 RND-282）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

实现 `GET /api/admin/dashboard` 聚合统计 API：按租户隔离，返回
「已归档天数 / 消息总量 / 存储占用 / 监控员工数 / 同步健康度 / 近 N 天每日归档量(文本 vs 媒体) / 最近活动(来自 AuditLog，A7 未落地前返回空)」；
支持 `?range=14|30|90` 区间参数。
**复用 A1-1 的 `app.services.usageservice` 概览原语**；本票只新增 dashboard 专属的窗口聚合与服务 / 路由 / Schema。
**不新建 schema / migration，不碰 B 层生产路径，不自行补 A1-1 模块。**

## 二、决策背景（已全部拍板，不要再问）

来源：Epic `RND-262`（A1 概览首页 / Dashboard）子任务 **A1-2** + 设计稿 `design/ui-v1/pages/dashboard.html`。

- **区间参数固定三档 `{14, 30, 90}`，默认 30。** 验收标准 = 「区间切换改变查询区间」。
- **聚合原语复用 A1-1**：`backend/app/services/usageservice.py`（导入名 `app.services.usageservice`，RND-281 / A1-1 落地）= 概览聚合原语 `count_messages` / `sum_storage` / `count_monitored_employees` / `sync_health` / `get_archived_days`。其确切函数签名以 RND-281 落地代码为准（实现前先读该模块确认）。**RND-282 不重新实现这些概览原语**，只新增「窗口（14/30/90）专属」的每日分桶、窗口内总量 / 天数、最近活动降级。
- **同步健康**：只有 `SyncState.status` 是真实数据。设计稿里 5 行健康条（会话拉取任务 / 媒体下载队列 / 解密 worker / 通讯录同步 / 冷存储归档）**纯 mockup，无后端数据源** → A1-2 **不得编造**这 5 行，只返回真实可得的 `sync_status` 与 `sync_healthy`（来自 `usageservice.sync_health`）。
- **最近活动**：来自 A7 的 `AuditLog`，**当前后端无任何 AuditLog 模型 / 表 / migration**（已 grep 全仓确认）。→ 采用「优雅降级」策略，见第三节 A7 协调。
- **不实时推送**（依赖 RND-211 轮询刷新），**不暴露消息内容**（聚合只算 count/sum/分桶，绝不返回 `content_text` 或任何消息体）。
- 工程纪律：Agent 不 git commit/push；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS（本票无 SSR 改动，仅 JSON API）。代码标识符加反引号。

## 三、项目现状（基线，2026-07-28 扫描）

### 3.1 数据模型（已确认路径与列）
- `archive_messages`（`backend/app/db/models.py:320`，表 `archive_messages`）：
  - `msgtime` `BigInteger` **epoch 毫秒**（索引，L404）；区间过滤一律用 epoch-ms 整数。
  - `tenant_id` `String(36)` 索引、可空（L418-420）← 租户作用域。
  - `msgtype` `String(32)` 索引（L401）；`content_text` `Text`（L400，**绝不 SELECT**）；`sender`（L402）；`is_revoked`（L415）；`decrypt_status`（L389）；`roomid`（L403）。
  - 复合索引 `ix_archive_messages_tenant_msgtime_id (tenant_id, msgtime, id)`（L368-375）适合租户区间查询。
  - 伴生表 `archive_message_recipients`（`models.py:435`）：`receiver_userid`、`receiver_type`、`tenant_id`、`message_id` —— 用于收件人侧活跃度。
- `media_files`（`models.py:461`，表 `media_files`）：
  - `file_size` `BigInteger` **可空**（L538）← **存储 = `func.sum(file_size)`**。
  - `tenant_id` 可空索引（L530-532）；`archive_message_id` FK（L527）。
  - 注意 `file_size` 可空且历史上 `tenant_id` 可能未回填 → 用 `coalesce(file_size, 0)`，并 JOIN `archive_messages.tenant_id` 兜底过滤。
- `sync_states`（`models.py:272`，表 `sync_states`）：`status` `Enum("idle","syncing","error")` 非空默认 `idle`（L286-290）；`tenant_id` 可空索引（L294-296）；`corp_id`（L281）。
- **员工无独立表**：派生函数 `backend/app/conversation_membership.py` 的 `_collect_staff_ids(db, tenant_id)`（L48-78）。RND-281 的 `count_monitored_employees(tenant_id)` 即封装它。
- **AuditLog：不存在**（grep `AuditLog|audit_logs|class Audit` 全仓 0 命中；18 个迁移无 audit）。`backend/app/web/templates/audit-log.html` 是静态页，无后端。

### 3.2 鉴权依赖
- `backend/app/auth.py:319`：`get_current_user(session_id: Optional[str] = Cookie(...), db) -> Tuple[AdminUser, str]`，返回 `(user, tenant_id)`；
  缺失 / 过期 / 吊销 → `HTTPException(401)`。**`tenant_id` 是唯一授权作用域，绝不可从请求参数取**（见 docstring L324-328）。
- 导入路径：`from app.auth import get_current_user`。

### 3.3 路由约定
- `backend/app/routers/reachability_audit.py:65-96` 用「装饰器写全路径 + `include_router` 无 prefix」模式。
- **新路由沿用此模式**：`@router.get("/api/admin/dashboard", response_model=DashboardOut)`，在 `app/main.py` 加 `app.include_router(dashboard_router)`（无 prefix）。
- 管理员 / 鉴权端点统一前缀 `/api/admin/...`（与 reachability-audit、sync-status 一致）。

### 3.4 架构边界（已确认无需改 allowlist）
- `backend/tests/test_architecture_boundary.py:55-60`：`_LAYER_PACKAGES` 已含 `"app.services": "service"` 与 `"app.routers": "router"`。
- 新建 `app/services/dashboard_service.py` / `app/routers/dashboard.py` **自动分层，无需改动 `_FLAT_SERVICE_MODULES` 或任何 allowlist**（与 RND-283 的 `analytics_service.py` 同级兄弟模块）。
- 依赖方向：**服务层不得 import `routers` / `main`**；**路由层不得 import `app.main`**；`app/main.py` 是唯一组合根，只做 `include_router(...)`。

### 3.5 Alembic
- 当前 head = `0018_password_reset_tokens.py`。
- **RND-282 不新建任何 migration**（AuditLog 属 A7）。`alembic check` 必须仍绿。

### 3.6 ⚠️ 重叠协调：与 RND-281(A1-1 UsageService) —— 严禁自补模块
- A1-1 的概览原语模块 = **`backend/app/services/usageservice.py`**（导入名 `app.services.usageservice`）。RND-281 自身 `[BLOCKED: F0]`，故真实前置链 = **F0 → RND-281 → RND-282**。
- **前置校验（开工第一步，RED 前）**：`python -c "import app.services.usageservice"` 必须成功，且 `alembic check` 绿。
  - **若失败（A1-1 未落地）→ 立即停下，在交付说明里报告「A1-1 未就绪（其自身 blocker 为 F0），RND-282 保持 BLOCKED，未实现」，绝不继续，也绝不自行创建 `usageservice.py` 或任何 `usage.py` / `UsageService` 模块**（与 RND-283 硬约束一致：不重复实现 A1-1 职责）。
  - **若成功** → `from app.services.usageservice import ...` 复用其概览原语（确切签名以落地代码为准，先读模块）。

### 3.7 ⚠️ 重叠协调：与 A7（Audit 基础设施）
- A7 是独立 blocked 票（建 `audit_logs` 表 + 写审计行）。**RND-282 不建 AuditLog。**
- `recent_activity` 采用优雅降级：服务内用 `getattr(models, "AuditLog", None)` 在**调用时**判断（**绝不顶层 import AuditLog**，否则 A7 未落地时模块加载即失败）。有则按租户查最近 N 条映射成 `ActivityItem`；无则返回 `[]`。
- QA 必须验证：A7 缺失时端点返回 200 且 `recent_activity == []`，**绝不 500**。

## 四、实现步骤（GREEN，最小变更）

参考 `backend/app/routers/reachability_audit.py` 与 `backend/app/services/analytics_service.py`（RND-283，同级兄弟）风格。

### 步骤 1 — 前置校验（RED 前确认；A1-1 未落地即停下）
```bash
cd backend
python -c "import app.services.usageservice" && echo "A1-1 OK" || { echo "A1-1 MISSING -> STOP, report BLOCKED"; exit 1; }
alembic check                                            # 预期绿（无 pending）
grep -rn "class AuditLog\|audit_logs" app/db/models.py   # 预期：无（A7 未落地）；若有，停下报告
```
若 A1-1 缺失 → 停下，不写任何文件，交付 BLOCKED 报告。

### 步骤 2 — 响应 Schema：`backend/app/schemas/dashboard.py`（新建）
```python
from typing import List, Optional
from pydantic import BaseModel


class DayBucket(BaseModel):
    date: str            # "YYYY-MM-DD"（北京时间）
    text_count: int      # msgtype 非媒体类
    media_count: int     # 媒体类（图片/文件/音视频）


class ActivityItem(BaseModel):
    id: str              # "audit#a9f31c" 形式（A7 落地后）
    badge: str           # export / view / config / fail / sync
    actor: str           # 姓名 或 "system"
    description: str
    scope: Optional[str] = None   # conversation / group ×3 / department:market
    ip: Optional[str] = None
    time: str            # "14:12" 或 ISO8601


class DashboardOut(BaseModel):
    range_days: int
    days: int                     # 窗口内「有 ≥1 条消息」的 distinct 自然日数
    total_messages: int           # 窗口内 COUNT(archive_messages)
    storage_bytes: int            # 租户累计 SUM(media_files.file_size)（来自 usageservice）
    staff_count: int              # len(_collect_staff_ids)（来自 usageservice）
    silent_staff_30d: int         # 近 30 天零收发的员工数（派生）
    sync_status: str              # idle / syncing / error（来自 usageservice.sync_health）
    sync_healthy: bool            # status != "error"
    daily_series: List[DayBucket]
    recent_activity: List[ActivityItem]   # A7 缺失时为 []
    generated_at: str             # ISO8601（UTC）
```
若 `backend/app/schemas/__init__.py` 需要显式导出，补一行 `from .dashboard import *`（该文件已存在）。

### 步骤 3 — Dashboard 专属聚合服务：`backend/app/services/dashboard_service.py`（新建；复用 A1-1 原语）
**只放 dashboard 专属的窗口逻辑 + 最近活动降级**；概览原语一律 `from app.services.usageservice import ...`。
```python
from typing import List
from datetime import datetime, timezone
from sqlalchemy import func
from app.db.models import ArchiveMessage
from app.db import models
# 概览原语来自 A1-1（确切签名以 usageservice 落地代码为准，先读该模块）：
from app.services.usageservice import (
    sum_storage, count_monitored_employees, sync_health,
)

MS_PER_DAY = 86_400_000
# 媒体类 msgtype：以项目真实取值为准（grep `msgtype` 取值或参考
# backend/app/message_type_registry.py），不要硬编码错值。
MEDIA_MSGTYPES = {"image", "file", "voice", "video", "emotion", "link", "mixed"}


def _now_ms() -> int: ...


def daily_message_series(db, tenant_id, from_ms, to_ms, range_days) -> List[DayBucket]:
    # group by msgtime // MS_PER_DAY；media_count = msgtype IN MEDIA_MSGTYPES，
    # text_count = 其余（穷尽二分，保证 media+text == 当天总数）；
    # 补齐窗口内无消息的日期 text=0/media=0；date 转为 YYYY-MM-DD（北京时间）


def count_silent_staff(db, tenant_id, since_ms) -> int:
    # staff_ids = conversation_membership._collect_staff_ids(db, tenant_id)
    # active_ids = distinct(sender in window) ∪ distinct(receiver_userid in window)
    # return len(staff_ids - active_ids)


def recent_activity(db, tenant_id, limit=20) -> List[ActivityItem]:
    AuditLog = getattr(models, "AuditLog", None)
    if AuditLog is None:
        return []                      # A7 未落地，优雅降级
    # else: 租户查询最近 limit 条 → 映射 ActivityItem


def build_dashboard(db, tenant_id, range_days: int) -> DashboardOut:
    from_ms = _now_ms() - range_days * MS_PER_DAY
    to_ms = _now_ms()
    series = daily_message_series(db, tenant_id, from_ms, to_ms, range_days)
    total = sum(b.text_count + b.media_count for b in series)
    active_days = sum(1 for b in series if (b.text_count + b.media_count) > 0)
    status, healthy = sync_health(tenant_id)        # 来自 A1-1；必要时传 db
    return DashboardOut(
        range_days=range_days,
        days=active_days,
        total_messages=total,
        storage_bytes=sum_storage(tenant_id),       # 来自 A1-1；必要时传 db
        staff_count=count_monitored_employees(tenant_id),  # 来自 A1-1；必要时传 db
        silent_staff_30d=count_silent_staff(db, tenant_id, _now_ms() - 30 * MS_PER_DAY),
        sync_status=status,
        sync_healthy=healthy,
        daily_series=series,
        recent_activity=recent_activity(db, tenant_id),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
```
- **不要 import `routers` / `main`**；可 import `app.db.models`、`app.db`、`app.conversation_membership`、`app.schemas.dashboard`、`app.services.usageservice`。
- `sync_health` / `sum_storage` / `count_monitored_employees` 的**真实签名以 RND-281 落地代码为准**——若它们需要 `db` 参数，按实际签名传 `db`。实现前先 `python -c "import inspect, app.services.usageservice; print(inspect.signature(app.services.usageservice.sync_health))"` 确认。
- `daily_message_series` 的 msgtype 二分必须**穷尽**（media + text == 当天消息总数），保证与 `total_messages` 自洽。

### 步骤 4 — 路由：`backend/app/routers/dashboard.py`（新建）
```python
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.db.models import AdminUser
from app.db.session import get_db
from app.services.dashboard_service import build_dashboard
from app.schemas.dashboard import DashboardOut

router = APIRouter()


@router.get("/api/admin/dashboard", response_model=DashboardOut)
def get_dashboard(
    range_days: int = Query(30, ge=1, le=90, description="仅接受 14/30/90，见下方归一化"),
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(get_current_user),
):
    _, tenant_id = auth
    if range_days not in (14, 30, 90):
        range_days = 30   # 归一化非法值，避免 422 打扰前端
    return build_dashboard(db, tenant_id, range_days)
```
- **不要 import `app.main`。**

### 步骤 5 — 接线 `backend/app/main.py`
- 顶部 router import 区（搜 `reachability_audit_router` 定位 import 行）新增：
  `from app.routers.dashboard import router as dashboard_router`
- 在 `app/include_router` 组（L94-102，紧邻 `app.include_router(reachability_audit_router)` 或 L101 `web_router` 之后）新增：
  `app.include_router(dashboard_router)`   # 无 prefix，全路径在装饰器
- 不改任何既有 `include_router` / 中间件。

### 步骤 6 — 测试：`backend/tests/test_dashboard_api.py`（新建，镜像 `backend/tests/test_reachability_audit.py`）
- 手写 sqlite schema（`_SCHEMA_SQL`）含：`tenants`、`archive_messages`（含 `msgtime` BigInteger、`tenant_id`、`msgtype`、`sender`、`is_revoked`、`decrypt_status`）、`archive_message_recipients`、`media_files`（`file_size`、`tenant_id`、`archive_message_id`）、`sync_states`、`contacts`、`admin_users`；**不含 `audit_logs`**（验证 A7 缺失路径）。
- `db` fixture（sqlite `Session`）、`client` fixture（`TestClient(app)`、`raise_server_exceptions=False`）。
- 鉴权 + DB override 模式（`test_reachability_audit.py:627-646`）：
  `app.dependency_overrides[get_current_user] = lambda: (mock_user, _TENANT_A)`；
  `app.dependency_overrides[get_db] = _override_db`。
- **A1-1 桩（关键）**：测试要让 `from app.services.usageservice import ...` 成功，且 `build_dashboard` 能拿到概览原语。两种做法择一：
  - (a) **真实依赖**：测试环境 `app.services.usageservice` 已随 RND-281 合并存在 → 直接用它（推荐，最接近生产）。
  - (b) **桩模块**：若测试隔离需要，可在测试内 `sys.modules["app.services.usageservice"]` 注入一个提供 `sum_storage`/`count_monitored_employees`/`sync_health` 的轻量桩（返回固定值），断言 `DashboardOut` 透传了这些值。无论哪种，都**不得**让测试绕过「A1-1 必须存在」的前提。
- 用例：
  - **T1 区间切换**：seed 不同 `msgtime` 分布在 14 / 30 / 90 天内 → `GET ?range=14` vs `?range=90`，`total_messages` 与 `daily_series` 长度明显不同 → 断言区间切换改变结果。
  - **T2 租户隔离**：seed 两个租户数据 → 用 tenantA 的 auth override → 断言返回仅含 tenantA 数据（`total_messages` / `staff_count` 不含 tenantB）。
  - **T3 聚合真实**：seed 已知消息 / 媒体 / 员工 / sync → 断言 `total_messages`、`storage_bytes`、`staff_count`、`sync_healthy`、`days` 与手算一致（概览原语来自 A1-1）。
  - **T4 最近活动降级**：A7 缺失（无 `audit_logs`）→ `GET` → 200 且 `recent_activity == []`，**绝不 500**。
  - **T5 未认证**：override `get_db` 返回无 session → 断言 401。
  - **T6 daily_series 形状**：长度 == `range_days`；每项含 `text_count`/`media_count`；无消息日期补 0；且 Σ(text+media) 跨天 == `total_messages`（自洽）。
  - **T7 非法 range 归一化**：`GET ?range=7` → 200 且 `range_days == 30`。
  - **T8 storage 稳定**：`range=14` 与 `range=90` 返回的 `storage_bytes` 相同（累计非窗口）。
- `make verify` 全绿（`lint-diff` 仅扫改动文件；`typecheck`/`build` 会 import app，须确保 main.py 接线正确）。
- 不要建 `conftest.py`（仓库现状无 conftest）。

### 步骤 7 — 验证
```bash
cd backend
python -m pytest tests/test_dashboard_api.py -q
make verify          # 全绿
alembic check        # 仍绿（无 schema 改动）
```

## 五、阶段三验证（RED/GREEN 记录）

RED（改前）：
```bash
cd backend
grep -rn "api/admin/dashboard" app/routers/        # 应无
python -c "import app.services.usageservice" 2>/dev/null && echo "A1-1 OK" || echo "A1-1 MISSING (must STOP)"
grep -rn "class AuditLog\|audit_logs" app/db/models.py   # 应无
```
GREEN（改后）：
```bash
cd backend
grep -n "/api/admin/dashboard" app/routers/dashboard.py     # 出现
grep -n "app.include_router(dashboard_router)" app/main.py  # 出现
python -c "import app.services.dashboard_service, app.schemas.dashboard" # 成功
python -m pytest tests/test_dashboard_api.py -q             # 全过
make verify                                               # 全绿
alembic check                                             # 绿
```

## 六、硬约束（违反即判失败）

- 不 git commit / push。
- **不新建 / 修改任何 schema 或 Alembic migration**（`alembic check` 必须仍绿）。AuditLog 属 A7，RND-282 不建。
- **A1-1 未落地（无 `app.services.usageservice`）→ 停下报告，绝不自行创建 `usageservice.py` / `usage.py` / `UsageService` 模块**（与 RND-283 硬约束一致：不重复实现 A1-1 职责）。
- **绝不返回消息内容**：聚合只算 count/sum/分桶，绝不 `SELECT content_text`、绝不回传任何消息体。
- **租户隔离**：每个聚合查询必须 `filter(tenant_id == auth[1])`；不得接受请求里的 tenant 参数。
- 同步健康**不得编造**设计稿的 5 行组件条（无数据源）；只返回 `sync_status` + `sync_healthy`。
- `recent_activity` 必须优雅降级（A7 缺失返回 `[]`），**不得顶层 import AuditLog**，不得 500。
- 不改动 `get_current_user` / `require_html_session` 语义；不碰 WeCom OAuth / 登录写入路径。
- 架构边界：`app/services/dashboard_service.py` 不得 import `routers`/`main`；`app/routers/dashboard.py` 不得 import `app.main`；`app/main.py` 仅做 `include_router`（不动既有路由 / 中间件）。
- 不触碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd 单元名、deploy.yml、`.env.example`、`backend/scripts` 一个字符不动）。
- 测试不得建 `conftest.py`；`make verify` 必须全绿。

## 七、收尾（交付物）

向用户交付：
- **A1-1 就绪确认**（或 BLOCKED 报告：含「其自身 blocker 为 F0」）/ RND-281 实际函数签名清单（供 A2 复用对照）。
- RED/GREEN 记录、`git diff --stat`（应仅含 `app/schemas/dashboard.py` + `app/services/dashboard_service.py` + `app/routers/dashboard.py` + `app/main.py`(两行) + `tests/test_dashboard_api.py`）、`make verify` 日志、`alembic check` 绿日志。
- 明确声明：本票**未**新建任何 A1-1 / AuditLog 模块或迁移；未提交。
