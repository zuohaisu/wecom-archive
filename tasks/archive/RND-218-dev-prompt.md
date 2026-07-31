# RND-218 执行提示词（开发 / 实现）

> 用途：粘贴给实现智能体（按 `DEV_AGENT_RULES.md` 的 Claude Code 实现角色），由其独立完成 RND-218。
> 验收由独立 QA 智能体按 `.workbuddy/prompts/rnd-218-qa-prompt.md` 复验。
> 本任务独立于 RND-226 / RND-210（触碰不同文件，无 overlap zone），可在 `main` 上单独执行；不碰 DB migration / CI / 企业名变更。

---

## 0. 任务与来源

- **Linear 工单**：RND-218「将 Web 与 Legacy Message Routes 移出 main.py」，优先级 P2，负责人 Haisu Zuo。
- **在重构链中的位置**：父任务 **RND-212**（渐进式重构为 AI 友好的模块化单体）；**阻塞 RND-223**（引入 App Factory 与分域 Typed Settings）。RND-218 必须先于 RND-223 合入。
- **目标**：把 `main.py` 里的 **Web 路由**（HTML 管理页）与 **Legacy Message 路由**（消息相关路由）抽到独立 router / schema，`main.py` 进一步收敛为 composition root。
- **目标文件（工单点名）**：
  - `backend/app/routers/web.py` — Web 路由（`/admin/conversations`、`/admin/search`、`/admin/diagnostics/reachability`）
  - `backend/app/routers/messages.py` — Legacy Message 路由（`/admin/messages`、`/admin/messages/{msgid}`、`/api/messages`、`/api/messages/{msgid}`）
  - `backend/app/schemas/messages.py` — 消息相关 Pydantic schema（`MessageOut`、`RecipientOut`、`MessageDetailOut`）
  - 配套新增：`backend/app/html_helpers.py`（`_e`/`_fmt_msgtime`/`_badge` 共享工具）
- **统一 HTML auth/session dependency**：把 5 条 HTML 路由里重复的「`_resolve_session_tenant_id(request, db)` + 若为 None 则 `return RedirectResponse("/admin/login", 302)`」模式，收敛为一个共享的 FastAPI 依赖 `require_html_session`（落在 `app/auth.py`，紧邻 `get_current_user`）。

### 0.5 前置条件（必须，开工前确认）

> **RND-217 必须已经 commit 到 `main`；本任务从干净的 `main` 工作树开始。**

理由（务必遵守，否则会踩坑）：
- 本提示词的行号与结构基于 **post-RND-216 / RND-217** 的 `main.py`。RND-216 已将 HTML/CSS/JS 常量外置为 `web/templates/*.html` + `web/static/*`；RND-217 进一步把 review console 的 JS 拆成 8 个模块。当前 `main.py` 里 Web 路由已是 `render_template("review_console", ...)` 形式，**不再持有 `_REVIEW_CONSOLE_HTML` 等巨型常量**。
- 仓库现状（执行本任务前请 `git status` 复核）：RND-217 的改动**仍在未提交的工作树中**（`backend/app/main.py`、`backend/app/web/static/console/*.js`、`backend/app/web/templates/review_console.html`、`backend/tests/test_http_contract.py` 等均 `M`/`A`）。若直接在其上叠加 RND-218，两套未提交改动会互相缠绕，违反 `DEV_AGENT_RULES.md` 的「One Linear Issue = One Implementation Conversation / One Commit」。
- **动作**：先请 Haisu 把 RND-217 的经验证改动 commit/push 到 `main`，确认 `git status` 干净且 `main.py` 为 RND-217 合并后的状态后，再开始本任务。若发现 RND-217 尚未 commit，停下并在评论里说明，不要自行提交 RND-217。

---

## 1. 精确范围（必须移出 / 必须保留 / 严禁）

### 1.1 必须移出 `main.py` 的路由（共 7 条，行号为 RND-217 合并后的当前 `main.py`）

- **Web（3 条，迁入 `routers/web.py`）**：
  - `GET /admin/conversations`（`main.py:336` → handler `admin_conversations`）
  - `GET /admin/search`（`main.py:350` → handler `admin_search_page`）
  - `GET /admin/diagnostics/reachability`（`main.py:364` → handler `admin_diagnostics_reachability`）
- **Message（4 条，迁入 `routers/messages.py`）**：
  - `GET /admin/messages`（`main.py:219` → handler `admin_messages`）
  - `GET /admin/messages/{msgid}`（`main.py:265` → handler `admin_message_detail`）
  - `GET /api/messages`（`main.py:379` → handler `get_messages`）
  - `GET /api/messages/{msgid}`（`main.py:402` → handler `get_message`）

> 行号仅作切片指引；实施时以函数体边界为准（handler 签名、装饰器、`return` 整体搬移），不要按行号硬编码。

### 1.2 必须随路由一起迁走的依赖（保持行为不变）

- **Pydantic schema**（`main.py:73–105`）：`MessageOut` / `RecipientOut` / `MessageDetailOut` → 迁入 `schemas/messages.py`，类名、`model_config = {"from_attributes": True}`、字段类型**一字不改**。
- **HTML 字符串工具**（仅被 2 条 message HTML 路由使用，`main.py:108–138`）：`_e`（108）、`_fmt_msgtime`（118）、`_badge`（135）→ 迁入新建 `app/html_helpers.py`。**Web 三条路由（`admin_conversations` / `admin_search_page` / `admin_diagnostics_reachability`）不直接用这些工具**——它们只调 `render_template(...)` 并传 `i18n_script` 与 JSON 注入，所以这些 helper 只归 `routers/messages.py` 引用即可。
- **Web 路由依赖的模块级 JSON 常量**（`main.py:311–332`，仅这两个 Web 路由用到）：
  - `_KNOWN_PLACEHOLDER_I18N_KEYS`（由 `I18N_JS_SOURCE` 算得）
  - `_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON`（由 `build_frontend_registry_entries(...)` 算得，供 `admin_conversations`）
  - `_SEARCH_MSGTYPE_OPTIONS_JSON`（由 `build_filterable_type_options()` 算得，供 `admin_search_page`）
  → 连同其依赖（`from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG`、`from app.message_type_registry import build_frontend_registry_entries, build_filterable_type_options`）一并迁入 `routers/web.py`。`main.py` 里 `re` / `json` 若仅被这些块使用则随之迁走，否则保留（不要破坏 `main.py` 其它用法）。
- **HTML 页面本身已在 `web/templates/*.html`**（RND-216 外置），本任务**不**动这些模板文件：
  - `web/templates/review_console.html`、`web/templates/search.html`、`web/templates/diagnostics.html`、`web/templates/messages.html`、`web/templates/message_detail.html`、`web/templates/message_detail_404.html`
  - 路由只是 `render_template("review_console", i18n_script=I18N_SCRIPT_TAG, mtr_entries_json=...)` 等形式，迁入 router 后调用方式不变。
- **`/api/messages*` 认证**：依赖 `get_current_user`（来自 `app.auth`，返回 `(user, tenant_id)`、未认证抛 `401`）→ 保持不变，仅在 `routers/messages.py` 里 `auth: Tuple = Depends(get_current_user)`。
- **tenant 范围**：所有查询都以 `ArchiveMessage.tenant_id == tenant_id` 过滤；`/api/messages/{msgid}` 的 recipients 同样按 `ArchiveMessageRecipient.tenant_id == tenant_id` 过滤——逐字保留。

### 1.3 统一 HTML session 依赖（新增）

在 `app/auth.py` 紧邻 `get_current_user` 新增：

```python
from typing import Optional
from fastapi import Request
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import AdminSession

def require_html_session(
    request: Request, db: Session = Depends(get_db)
) -> Optional[str]:
    """HTML 路由共享依赖：返回 tenant_id；未认证/会话失效则返回 None。
    行为必须等价于原 _resolve_session_tenant_id（含 DB 失败→None、绝不记 bound 参数）。
    路由内据此 return RedirectResponse('/admin/login', 302)。"""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    now = datetime.now(timezone.utc)
    try:
        session = (
            db.query(AdminSession)
            .filter(
                AdminSession.id == session_id,
                AdminSession.expires_at > now,
                AdminSession.is_revoked.is_(False),
            )
            .first()
        )
    except Exception as exc:
        logger.error("require_html_session: session lookup failed: %s", type(exc).__name__)
        return None
    return session.tenant_id if session is not None else None
```

> 逻辑 = 现 `main.py:140–167` 的 `_resolve_session_tenant_id` 原样迁移（含 `SESSION_COOKIE`、`logger`、DB 失败→None）。`require_html_session` 落在 `app/auth.py` 后可复用同模块的 `SESSION_COOKIE` 与 `logger`。

**路由内写法（逐字节等价于现状，302 body 一致）**：

```python
tenant_id: Optional[str] = Depends(require_html_session)
if tenant_id is None:
    return RedirectResponse("/admin/login", status_code=302)
```

> **不要用** `HTTPException(302, headers={"Location": ...})` 来统一——那会把 redirect body 从 `RedirectResponse` 的默认 HTML 改成 `{"detail": ...}` JSON，已在 QA 提示词 §5 标为边界风险。本任务要求 302 行为与改造前**一致**，故采用「依赖返回 `Optional[str]` + 路由内 `RedirectResponse`」形式。

### 1.4 必须保留在 `main.py`（显式不在本期范围）

- `app = FastAPI(title="365 WeCom Archive")`
- `MediaAccessNoStoreMiddleware` 中间件注册（`app.add_middleware`）
- `_RedactOAuthCallbackQueryFilter` 及 `uvicorn.access` 过滤器
- 健康检查 `GET /health`、`/health/live`、`/health/ready`
- 既有 `include_router(...)` 调用（auth / conversations / reachability_audit / search / wecom_events）+ **新增** `web_router`、`messages_router` 的 `include_router`

### 1.5 非目标（严禁）

- 不删除任何 legacy 页面；不改动任何查询语义（SQL 过滤条件、`limit` 范围、排序、`response_model` 形状）。
- 不改 `/api/messages*` 的认证方式（保持 `401` 而非改成 `redirect`）。
- 不引入 DB migration / schema 变更、不碰 CI、不碰企业微信企业名变更。
- **不碰 `web/templates/*.html`**（RND-216 已外置）。
- 不做超出「移出 + 统一依赖」的整体重构。

---

## 2. 执行步骤（严格按顺序）

### 阶段一：建支撑模块（零行为变化）

1. 新建 `backend/app/schemas/__init__.py`（空）与 `backend/app/schemas/messages.py`，把 `MessageOut` / `RecipientOut` / `MessageDetailOut` 原样迁入（类名、`model_config`、`from_attributes` 一字不改）。
2. 新建 `backend/app/html_helpers.py`，把 `_e` / `_fmt_msgtime` / `_badge` 原样迁入（含其 `import html as _html`、`from datetime import datetime, timedelta, timezone` 等依赖；**不 import `app.main`**，避免循环依赖）。
3. 在 `app/auth.py` 新增 §1.3 的 `require_html_session`（逻辑 = 现 `_resolve_session_tenant_id`）。

### 阶段二：建 `routers/messages.py`（4 条消息路由）

4. 新建 `backend/app/routers/messages.py`，把 4 条 message 路由整段迁入：

   ```python
   from typing import Optional, Tuple
   from fastapi import APIRouter, Depends, HTTPException, Query, Request
   from fastapi.responses import HTMLResponse
   from sqlalchemy.orm import Session
   from app.auth import get_current_user, require_html_session
   from app.db.models import ArchiveMessage, ArchiveMessageRecipient
   from app.db.session import get_db
   from app.html_helpers import _e, _fmt_msgtime, _badge
   from app.schemas.messages import MessageOut, RecipientOut, MessageDetailOut
   from app.web import render_template

   router = APIRouter()
   ```

   - `GET /admin/messages` 与 `GET /admin/messages/{msgid}`：`response_class=HTMLResponse`；`tenant_id: Optional[str] = Depends(require_html_session)` + None→`RedirectResponse`；内部查询与 `row_html` / `recipient_html` 拼接**逐字保留**（`_e`/`_fmt_msgtime`/`_badge` 现由 `app.html_helpers` 导入）；`render_template("messages"|"message_detail"|"message_detail_404", ...)` 调用不变。
   - `GET /api/messages` 与 `GET /api/messages/{msgid}`：`response_model=list[MessageOut]` / `MessageDetailOut`、`auth: Tuple = Depends(get_current_user)`、`_`、`tenant_id = auth[1]`；404 用 `HTTPException(404)`；recipients 的 `tenant_id` 过滤——**逐字保留**。参数（`sender`/`q`/`msgtype`/`roomid`/`limit`，`limit` `Query(20, ge=1, le=_MAX_LIMIT)`）与排序 `msgtime.desc()` 不变。
   - handler 函数名可保持原名（`admin_messages`/`admin_message_detail`/`get_messages`/`get_message`）。

### 阶段三：建 `routers/web.py`（3 条 Web 路由 + 模块级常量）

5. 新建 `backend/app/routers/web.py`，把 3 条 Web 路由 + 其依赖的模块级 JSON 常量（§1.2）整段迁入：

   ```python
   import re, json
   from typing import Optional
   from fastapi import APIRouter, Depends, Request
   from fastapi.responses import HTMLResponse
   from sqlalchemy.orm import Session
   from app.auth import require_html_session
   from app.db.session import get_db
   from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG
   from app.message_type_registry import build_frontend_registry_entries, build_filterable_type_options
   from app.web import render_template

   router = APIRouter()

   _KNOWN_PLACEHOLDER_I18N_KEYS = frozenset(re.findall(r'"(placeholder\.[a-zA-Z0-9_]+)"', I18N_JS_SOURCE))
   _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON = json.dumps(build_frontend_registry_entries(known_placeholder_keys=_KNOWN_PLACEHOLDER_I18N_KEYS))
   _SEARCH_MSGTYPE_OPTIONS_JSON = json.dumps(build_filterable_type_options())
   ```

   - 路由体：`tenant_id: Optional[str] = Depends(require_html_session)` + None→`RedirectResponse("/admin/login", 302)`；返回 `HTMLResponse(content=render_template("review_console"|"search"|"diagnostics", i18n_script=I18N_SCRIPT_TAG, ...))`，参数（`mtr_entries_json=` / `msgtype_options_json=`）与现状一致。`response_class=HTMLResponse` 保留。

### 阶段四：收敛 `main.py`

6. 在 `main.py` 顶部（既有 `from app.routers.* import router as ...` 之后）新增：

   ```python
   from app.routers.web import router as web_router
   from app.routers.messages import router as messages_router
   ```

   并在既有 `include_router` 块后追加：

   ```python
   app.include_router(web_router)
   app.include_router(messages_router)
   ```

7. 从 `main.py` **彻底删除**被迁走的 7 条路由定义、3 个 schema 类（73–105）、`_e`/`_fmt_msgtime`/`_badge`（108–138）、`_resolve_session_tenant_id`（140–167）、两个 JSON 常量及其周边 import（若仅此处用）。**严禁在 main.py 留下任何副本**——否则 `test_router_count` 会从 33 变成 34 而失败。
8. 清理 `main.py` 顶部 import：若 `re`/`json`/`I18N_JS_SOURCE`/`I18N_SCRIPT_TAG`/`build_frontend_registry_entries`/`build_filterable_type_options` 等仅被迁出代码使用，则一并删除对应 import，避免 unused / 循环 import。注意 `app/routers/web.py` 与 `app/routers/messages.py` 都只 import `app.auth` / `app.db.*` / `app.web` / `app.html_helpers` / `app.schemas.messages` / `app.i18n_assets` / `app.message_type_registry`——**绝不反向 import `app.main`**。

### 阶段五：自测（不达标不收工）

9. 先跑最关键的契约测试：

   ```bash
   cd backend && python -m pytest tests/test_http_contract.py -q
   ```

   必须全绿，尤其 `test_router_count`（断言 `== 33`）与 `test_route_snapshot_with_real_model_names`（response_model 名 / response_class / path / methods 完全匹配）、`test_routers_are_registered`（path 集合不增不减）。

10. 跑本期相关回归套件：

    ```bash
    python -m pytest tests/test_auth.py tests/test_password_auth.py tests/test_rnd225_auth_fail_closed.py tests/test_tenant_isolation.py tests/test_admin_timestamp_formatting.py tests/test_admin_auto_refresh.py tests/test_admin_auto_load_older.py tests/test_reachability_diagnostics_page.py tests/test_reachability_diagnostics_render.py tests/test_rnd_206_top_level_image.py tests/test_rnd229_focus_locate.py tests/test_rnd_210_msgtype_and_card.py -q
    ```

11. 收口跑 `make verify`（lint-diff + typecheck + build + 全量 pytest），确认全绿、无 unused import / 循环 import 告警。

---

## 3. 兼容性契约（不可违反，验收据此判定）

- **路由总数 == 33**（`tests/test_http_contract.py:315` `test_router_count`）。移出 ≠ 新增；若变 34 即证明 `main.py` 留了副本 → 立即修。
- **路由快照逐字段相等**（`test_route_snapshot_with_real_model_names`）：
  - `/admin/conversations`、`/admin/messages`、`/admin/messages/{msgid}`、`/admin/search`、`/admin/diagnostics/reachability` → `response_class=HTMLResponse`，`response_model=None`。
  - `/api/messages` → `response_model=list[MessageOut]`；`/api/messages/{msgid}` → `response_model=MessageDetailOut`。
  - methods 全为 `GET`；path 与 `test_routers_are_registered` 期望集合完全一致（不增不减、不重名）。
- **认证语义不变**：HTML 路由未认证 → **302** 跳 `/admin/login`（原行为，依赖返回 None + `RedirectResponse`）；`/api/messages*` 未认证 → **401**（`get_current_user` 原行为）。tenant_id 一律从 session 解析，绝不接受请求里的 tenant 参数。
- **租户隔离不变**：每条查询都以 `tenant_id == session.tenant_id` 过滤；`/api/messages/{msgid}` 的 recipients 同样按 tenant 过滤；不存在的消息 → HTML 404 / API 404。
- **查询语义不变**：`/admin/messages` 的 `sender`/`q`/`limit(50,1–100)`；`/api/messages` 的 `sender`/`q`/`msgtype`/`roomid`/`limit(20,1–100)`；排序 `msgtime.desc()`——与改造前逐字一致。
- **响应体兼容**：HTML 页由 `render_template` + `web/templates/*.html` 渲染，模板未动 → 渲染结果与改造前一致；JSON 字段名（`msgid/seq/msgtype/sender/roomid/msgtime/content_text/decrypt_status/recipients`）不变。

---

## 4. 硬性约束（实现 agent 自身也要守）

- 不修改任何查询语义、不删除 legacy 页面、不改 API 认证方式、不碰 `web/templates/*.html`。
- 不引入 DB migration / schema 变更；不碰 CI、企业名变更、WeCom 回调逻辑。
- 不引入后台进程；假设开发服务器已在运行；所有命令前台运行。
- 循环依赖防控：新建 `html_helpers.py` / `schemas/messages.py` / `routers/web.py` / `routers/messages.py` **不得 import `app.main`**。
- 不自行 `git commit` / `push` / 开 PR——保留给用户（Haisu）人工 merge 关卡。
- **必须满足 §0.5 前置条件**（RND-217 已 commit 到 `main`、工作树干净）才可开工。

---

## 5. 收尾动作

- 在 Linear 把 RND-218 状态 Todo → In Progress（如尚未）。
- 写一条评论：迁移了哪些路由到哪几个文件、统一了哪个 HTML session 依赖（落在 `app/auth.py` 的 `require_html_session`）、新增了哪些模块（`schemas/messages.py`、`html_helpers.py`）、`main.py` 删除了哪些内容、`make verify` 结论、是否保留了 33 路由与 route snapshot。
- 不要自动合入/提交；交还用户决策是否 merge（本任务为 RND-212 链一环，且阻塞 RND-223）。
