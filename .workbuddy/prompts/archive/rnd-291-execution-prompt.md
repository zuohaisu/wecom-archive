# RND-291 开发 agent 执行提示词
> 面向开发 agent（单人端到端实现 RND-291 / A6-1 媒体列表/筛选 API）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
> 代码标识符加反引号；架构冻结 D1：SSR + 原生 JS（本票纯后端 API，不碰前端）。

## 一、任务（一句话）
新增 `GET /api/admin/media` —— 在租户内列出 `media_files`（按 `file_type` / 时间筛选 + 分页），返回给前端「媒体库」网格用的**安全元数据**（绝不暴露存储引用 / sdkfileid）。保持既有媒体服务路由、WeCom、登录、审计、契约全部不变。

## 二、精确落点 / 根因（已定位，附 文件:行号:函数）
### 根因 A — 缺媒体库列表端点（主因）
- `backend/app/routers/media.py`（`router = APIRouter()`，L46）只提供**逐条消息**媒体访问路由：`GET /api/conversations/{conversation_id}/messages/{msgid}/media`、`/media/access`、`/nested-media/...`（L91 / L201 / L342 / L426）。这些路由由 `MediaAccessNoStoreMiddleware`（L76，路径正则 L71）覆盖，**路径前缀是 `/api/conversations/`**，与计划新增的 `/api/admin/media` 完全不重叠 —— 因此**不要**往 `media.py` 里加列表路由，新建独立文件即可，互不干扰。
- 过滤维度对齐设计稿 `design/ui-v1/pages/media.html`：类型 chips（图片/文件/语音/视频，L41-45）、时间（近30/90天/全部/自定义，L47）、排序（最新/最早/体积，L49）。本票 AC 只硬性要求**类型 + 时间**两类筛选（见 issue 原文「按类型/时间筛选」）；文件名搜索 / 发送人筛选 / 排序为可选增强（见第四节路 A 的「可选」标注）。

### 根因 B — `MediaFile` 无 filename 列（派生事实）
- `backend/app/db/models.py:493` `class MediaFile` 列：`id`(L557) / `sdkfileid`(L558) / `archive_message_id`(FK→archive_messages.id, L559) / `tenant_id`(L562) / `file_type`(L565, 取值 `image`/`video`/`voice`/`file`) / `file_size`(L570) / `download_status`(L571) / `storage_backend`(L568, 取值 `local`/`qiniu_kodo`) / `image_width`(L591) / `image_height`(L592) / `thumbnail_ref`(L590) / `created_at`(L606)。
- **没有 filename 列**：`backend/app/schemas/media.py:56` 明确注释 `filename is always None today`。网格里展示的文件名（`IMG_20260728_0931.jpg` 等）来自关联 `ArchiveMessage` 的 `structured_content` JSONB（attachment 的 `title`/`filename`）。列表响应需 JOIN `archive_messages` 派生 `name` + `msgtime`(显示/排序) + `roomid`。
- `file_type` 取值已确认：`backend/app/media_classification.py:56-59` 与 `backend/app/media_storage.py:830-833` 使用 `image`/`video`/`voice`/`file` 四值 —— 类型筛选的合法枚举即此四值。

### 根因 C — 缺 RBAC 门禁 + 契约登记（依赖事实）
- 端点必须门禁：复用 F0-5 的 `require_role`（`backend/app/auth.py:378`，返回 `Tuple[AdminUser, str]`，L391-395 的 `_checker` 返回 `auth` 原元组）。无参调用 `require_role()` = 允许全部 `ADMIN_ROLES`（含 `readonlyaudit`，敏感数据读与 A7-3 一致姿态）。
- 注册范式：`backend/app/main.py:102-103` 的 admin 路由用 `app.include_router(xxx_router, prefix="/api/admin")`；新路由照此注册。
- 契约测试 `backend/tests/test_http_contract.py`：`route_count == 46`(L325) / path 集合(L334-381) / snapshot(L407-500) 三处必须同步（详见第四节路 B）。

## 三、阶段一：复现 + 测量（RED）
1. 启动环境：`cd backend && make dev`（或 `uvicorn app.main:app`）。确认 `import app.auth` 中 `require_role` 可解析（F0 已合）。
2. 基线测量：
   - `curl -b "<admin cookie>" http://localhost:8000/api/admin/media` → 预期 **404**（端点不存在）或不在契约 path 集合。
   - 跑 `make verify` → `test_router_count` 当前 `== 46`（以此为基线 N）。
3. 记录 RED 数字：端点不存在（404）、`route_count` 基线 = 46。

## 四、阶段二：实现（GREEN，最小变更）
### 路 A — 新路由 `backend/app/routers/media_library.py`（新建）
```python
"""A6-1 (RND-291): 媒体库列表/筛选 API。
只读、租户隔离、offset/limit + has_more。复用 F0-5 require_role 门禁。
tenant_id 仅来自 require_role 返回的元组，绝不接受请求参数。
不写审计（A6-2 拥有下载审计钩子）；不改既有媒体服务路由。
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

from app.auth import require_role
from app.db.models import AdminUser, MediaFile, ArchiveMessage
from app.db.session import get_db
from app.schemas.media_library import MediaLibraryPage

router = APIRouter()

_ALLOWED_TYPES = {"image", "video", "voice", "file"}

def _media_label(sc) -> Optional[str]:
    """从 ArchiveMessage.structured_content 防御性提取附件显示名。"""
    if not isinstance(sc, dict):
        return None
    for k in ("title", "filename"):
        v = sc.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    items = sc.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        t = items[0].get("title") or items[0].get("filename")
        if isinstance(t, str) and t.strip():
            return t.strip()
    return None

@router.get("/api/admin/media", response_model=MediaLibraryPage)
def list_media(
    file_type: Optional[str] = Query(None, description="逗号分隔: image,file,voice,video"),
    days: Optional[int] = Query(None, description="仅返回近 N 天（按 MediaFile.created_at）"),
    since: Optional[datetime] = Query(None, description="ISO8601 下界"),
    until: Optional[datetime] = Query(None, description="ISO8601 上界"),
    q: Optional[str] = Query(None, description="可选：文件名/内容模糊（ILIKE archive_messages.content_text）"),
    sort: Optional[str] = Query("newest", description="newest|oldest|size_desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=200),
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(require_role()),
):
    _, tenant_id = auth
    # ---- 校验类型枚举 ----
    types = None
    if file_type:
        types = [t.strip() for t in file_type.split(",") if t.strip()]
        bad = set(types) - _ALLOWED_TYPES
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown file_type: {sorted(bad)}")
    # ---- 租户内基础查询（JOIN 父消息取 name/msgtime/roomid）----
    stmt = (
        select(MediaFile, ArchiveMessage)
        .join(ArchiveMessage, MediaFile.archive_message_id == ArchiveMessage.id)
        .where(MediaFile.tenant_id == tenant_id)
    )
    if types:
        stmt = stmt.where(MediaFile.file_type.in_(types))
    if days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=days)
    if since is not None:
        stmt = stmt.where(MediaFile.created_at >= since)
    if until is not None:
        stmt = stmt.where(MediaFile.created_at <= until)
    if q:  # 可选增强
        stmt = stmt.where(ArchiveMessage.content_text.ilike(f"%{q}%"))
    # ---- 排序 ----
    if sort == "oldest":
        stmt = stmt.order_by(MediaFile.created_at.asc(), MediaFile.id.asc())
    elif sort == "size_desc":
        stmt = stmt.order_by(MediaFile.file_size.desc().nullslast(), MediaFile.id.asc())
    else:  # newest
        stmt = stmt.order_by(MediaFile.created_at.desc(), MediaFile.id.asc())
    # ---- 总数（同过滤条件，无分页）----
    total = db.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0
    # ---- 分页取页 ----
    rows = db.execute(stmt.offset(offset).limit(limit)).all()
    items = []
    for mf, msg in rows:
        items.append({
            "id": mf.id,
            "file_type": mf.file_type,
            "mime_type": mf.mime_type,
            "file_size": mf.file_size,
            "image_width": mf.image_width,
            "image_height": mf.image_height,
            "download_status": mf.download_status,
            "storage_backend": mf.storage_backend,          # 仅类型串，非引用
            "has_thumbnail": bool(mf.thumbnail_ref),
            "created_at": mf.created_at,
            "message_id": mf.archive_message_id,
            "room_id": msg.roomid if msg else None,
            "msgtime": msg.msgtime if msg else None,
            "name": _media_label(msg.structured_content) if msg else None,
        })
    return MediaLibraryPage(items=items, total=total, has_more=len(items) == limit)
```
> 说明：`q` / `sort` 为可选增强，AC 不强求；若实现，保持如上。JOIN 用 `ArchiveMessage.id`（FK 目标，唯一），单查询取回 `(mf, msg)` 对，避免 N+1。

### 路 B — schema + main 注册 + 契约测试（三处必同步）
1. **新建 `backend/app/schemas/media_library.py`**：
```python
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel

class MediaFileListItem(BaseModel):
    id: int
    file_type: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    download_status: str
    storage_backend: Optional[str] = None
    has_thumbnail: bool = False
    created_at: datetime
    message_id: int
    room_id: Optional[str] = None
    msgtime: Optional[int] = None
    name: Optional[str] = None

class MediaLibraryPage(BaseModel):
    items: List[MediaFileListItem]
    total: int
    has_more: bool
```
2. **`backend/app/main.py` 注册**（仿 L9 / L102-103）：
   - L9 区域加：`from app.routers.media_library import router as media_library_router`
   - `create_app()` 内（L102 之后）加：`app.include_router(media_library_router, prefix="/api/admin")`
3. **`backend/tests/test_http_contract.py` 三处同步**（否则 `make verify` 红）：
   - L325 `assert route_count == 46` → 改为 `== <N+1>`。**禁止硬编码死值**：先 `make verify` 看 `test_router_count` 报错给出的真实 count（今天 46，若实现时已有其它路由合并会更高），设为 `真实值`；A6-1 只加 1 条路由，delta=1。
   - L334-381 `expected` path 集合：追加 `"/api/admin/media"`。
   - L407-500 `expected` snapshot：追加 `("/api/admin/media", frozenset({"GET"}), "MediaLibraryPage", "None")`（snapshot 用 `sorted` 比较，顺序无关；response_model 名须与类 `__name__` 一致）。

## 五、阶段三：验证（GREEN + 回归 + make verify）
1. 功能验证（回到阶段一）：
   - 带 admin cookie `GET /api/admin/media?file_type=image&days=30&limit=10` → 200，返回 `{items,total,has_more}`，`items` 仅含 `image` 且 `created_at` 在近 30 天。
   - `GET /api/admin/media?file_type=image,voice` → 仅两类。
   - 跨租户隔离：用 tenant B 的 cookie 请求，结果集 `tenant_id` 全部 == B（通过 DB 抽查或响应不泄漏其它租户数据验证）。
   - 非 admin / 无 cookie → 401；`readonlyaudit` 角色 → 200（require_role 无参放行）；低权被拒角色 → 403。
   - 记录 GREEN 数字：200 + 正确过滤 + 隔离成立。
2. 回归测试：`make verify`（lint-diff + typecheck + build + test）全绿；重点套件：`test_http_contract.py`、`test_architecture_boundary.py`、`backend/tests/test_password_auth.py`、`backend/tests/test_auth.py`。
3. 失败先修实现，不迁就测试（除非测试断言旧路径，需标注）。

## 六、硬约束（违反即判失败）
- **不改** `routers/media.py` 任何路由 / 中间件 / 路径；不碰登录、WeCom、审计、`/api/auth/me`。
- **零泄露（SF-1）**：响应**绝不**含 `sdkfileid` / `local_path` / `storage_ref` / `oss_key` / `bucket` / `migration_error` / `checksum_sha256` / `thumbnail_ref`(原值) / `playback_ref` / 任一 migration_* 列。只暴露 `storage_backend` 类型串 + `has_thumbnail` 布尔。
- **租户隔离 fail-closed**：`tenant_id` 仅来自 `require_role()` 返回的元组（`auth.py:391`），禁请求参数；JOIN 与 WHERE 均带 `tenant_id == tenant_id`。
- **无模型 / 无迁移**（schema 全已存在）→ `alembic check` 仍绿；不新建 `app/` flat service 模块（查询内联在路由，仿 `reachability_audit.py`），故**不改** `test_architecture_boundary.py`。
- **不写审计**（A6-2 职责）→ 不 import `app/audit`、不调用 `write_audit`；A7 仅作 epic 级前置校验（见收尾）。
- **不改 URL / status / 既有 response body / i18n / OpenAPI 形状**（除新增本条路由外）。
- **不执行 git commit / push**。

## 七、收尾（交付物）
向用户交付：RED 基线（404 + route_count=46）、GREEN 数字（200 + 过滤/隔离证据）、改动文件清单（`routers/media_library.py` / `schemas/media_library.py` / `main.py` / `test_http_contract.py`）、`make verify` 日志、`alembic check` 绿、未提交声明。
**前置依赖自检（开工 Step 0）**：
- F0 已合：`from app.auth import require_role` 成功（RBAC 门禁就绪）。
- A7（epic 级）：确认 A7-1（`AuditLog` 表，RND-293）已合并（`from app.db.models import AuditLog` 成功）—— 本票代码**不**依赖它，但若未合，A6 epic 整体前置未就绪，停下报告「A7-1 未合并」。
- 若任一前置缺失 → **停下报告**，严禁自行补模型/迁移/审计（越界即判失败）。
