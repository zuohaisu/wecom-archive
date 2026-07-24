# RND-218 执行提示词（开发 / 实现）

> 用途：粘贴给实现智能体（按 `DEV_AGENT_RULES.md` 的 Claude Code 实现角色），由其在单一分支上完成 RND-218。
> 验收由独立 QA 智能体按 `.workbuddy/prompts/rnd-218-qa-prompt.md` 复验。
> 本任务独立于 RND-226 / RND-210（触碰不同文件，无 overlap zone），可在 `main` 上单独开分支并行执行；不碰 DB migration / CI / 企业名变更。

---

## 0. 任务与来源
- Linear 工单：**RND-218「将 Web 与 Legacy Message Routes 移出 main.py」**，优先级 P2，状态 Todo，负责人 Haisu Zuo。
- 在重构链中的位置：父任务 **RND-212**（渐进式重构为 AI 友好的模块化单体）；**阻塞 RND-223**（引入 App Factory 与分域 Typed Settings）。RND-218 必须先于 RND-223 合入。
- 目标：把 `main.py` 里的 **Web 路由**（HTML 管理页）与 **Legacy Message 路由**（消息相关路由）抽到独立 router / schema，`main.py` 进一步收敛为 composition root。
- 目标文件（工单点名）：
  - `backend/app/routers/web.py` — Web 路由（`/admin/conversations`、`/admin/search`、`/admin/diagnostics/reachability`）
  - `backend/app/routers/messages.py` — Legacy Message 路由（`/admin/messages`、`/admin/messages/{msgid}`、`/api/messages`、`/api/messages/{msgid}`）
  - `backend/app/schemas/messages.py` — 消息相关 Pydantic schema（`MessageOut`、`RecipientOut`、`MessageDetailOut`）
- **统一 HTML auth/session dependency**：把所有 HTML 路由里重复的「`_resolve_session_tenant_id(request, db)` + 若为 None 则 `return RedirectResponse("/admin/login", 302)`」模式，收敛为一个共享的 FastAPI 依赖。

## 1. 精确范围（必须移出 / 必须保留 / 严禁）

### 1.1 必须移出 `main.py` 的路由（共 7 条）
- Web（3 条，迁入 `routers/web.py`）：
  - `GET /admin/conversations`（当前 main.py ~3550）
  - `GET /admin/search`（当前 main.py ~3558）
  - `GET /admin/diagnostics/reachability`（当前 main.py ~3566）
- Message（4 条，迁入 `routers/messages.py`）：
  - `GET /admin/messages`（当前 main.py ~235）
  - `GET /admin/messages/{msgid}`（当前 main.py ~300）
  - `GET /api/messages`（当前 main.py ~3581）
  - `GET /api/messages/{msgid}`（当前 main.py ~3604）

### 1.2 必须随路由一起迁走的依赖（保持行为不变）
- Pydantic schema：`MessageOut`、`RecipientOut`、`MessageDetailOut`（main.py ~70–102）→ 迁入 `schemas/messages.py`，类名、`from_attributes=True`、`model_config` 原样保留。
- Web 路由引用的巨型 HTML 模板常量 `_REVIEW_CONSOLE_HTML` / `_SEARCH_PAGE_HTML` / `_DIAGNOSTICS_HTML`，以及它们依赖的 `_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON`、`I18N_SCRIPT_TAG`、`_PAGE_CSS`、`_e`、`_fmt_msgtime`、`_badge`、（已统一后的）session 依赖 —— 一并迁入 `routers/web.py`（或写法上合理的共享模块），使 `main.py` 真正收敛。**渲染出的 HTML 响应体必须逐字节一致**（这是验收硬契约）。
- Message HTML 路由（detail 页）引用的 `_e`、`_fmt_msgtime`、`_badge`、`_PAGE_CSS`、session 依赖 —— 从共享模块 import。
- `/api/messages*` 路由依赖 `get_current_user`（来自 `app.auth`，返回 `(user, tenant_id)`、未认证抛 401）—— 保持不变，仅在 `routers/messages.py` 里 `Depends(get_current_user)`。

### 1.3 必须保留在 `main.py`（显式不在本期范围）
- `app = FastAPI(title="365 WeCom Archive")`
- `MediaAccessNoStoreMiddleware` 中间件注册
- `_RedactOAuthCallbackQueryFilter` 及 `uvicorn.access` 过滤器
- 健康检查 `GET /health`、`/health/live`、`/health/ready`（既非 Web 页也非消息路由，不属本期目标文件）
- 既有 `include_router(...)` 调用（auth / conversations / reachability_audit / search / wecom_events）+ **新增** `web_router`、`messages_router` 的 `include_router`

### 1.4 非目标（严禁）
- 不删除任何 legacy 页面；不改动任何查询语义（SQL 过滤条件、`limit` 范围、排序、response 形状）。
- 不改 `/api/messages*` 的认证方式（保持 401 而非改成 redirect）。
- 不引入 DB migration / schema 变更、不碰 CI、不碰企业微信企业名变更。
- 不做超出「移出 + 统一依赖」的整体重构。

## 2. 执行步骤（严格按顺序）

### 阶段一：提取共享依赖与 schema（先建支撑，零行为变化）
1. 新建 `backend/app/schemas/__init__.py`（空）与 `backend/app/schemas/messages.py`，把 `MessageOut` / `RecipientOut` / `MessageDetailOut` 原样迁入（类名、`model_config`、`from_attributes` 一字不改）。
2. 在 `app/auth.py` 内（紧邻 `get_current_user`）新增统一的 HTML session 依赖，例如：
   ```python
   from fastapi import HTTPException
   from fastapi.responses import RedirectResponse

   def require_html_session(
       request: Request, db: Session = Depends(get_db)
   ) -> str:
       """HTML 路由共享依赖：返回 tenant_id；未认证/会话失效则 302 跳 /admin/login。
       行为必须等价于原 _resolve_session_tenant_id + RedirectResponse(..., 302)。"""
       tenant_id = _resolve_session(... )  # 复用原逻辑（见下）
       if tenant_id is None:
           # 必须产生 status_code=302 且 Location=/admin/login 的响应
           raise HTTPException(
               status_code=302,
               headers={"Location": "/admin/login"},
           )
       return tenant_id
   ```
   - 原 `_resolve_session_tenant_id`（main.py ~156，含 DB 失通用 None→未认证、绝不记 bound 参数）的逻辑**整体迁入**此依赖内部（或保留为私有函数被其调用）。DB 失败 → 视未认证 → 302，绝不记 session token。
   - 注意：用 `HTTPException(302, headers={"Location": "/admin/login"})` 时，验收要确认产生的是 302 + `location: /admin/login`（可用 `test_http_contract.py` / `test_auth.py` / `test_rnd225_auth_fail_closed.py` 验证）。若发现某项 legacy 测试对 redirect body 敏感，**回退方案**为：依赖返回 `Optional[str]`，路由内 `if tid is None: return RedirectResponse("/admin/login", status_code=302)`，逻辑与原代码一致即可 —— 但优先用统一依赖形式。

### 阶段二：建 `routers/messages.py`
3. 新建 `backend/app/routers/messages.py`，把 4 条 message 路由整段迁入：
   - `GET /admin/messages` 与 `GET /admin/messages/{msgid}`：签名、查询参数（`sender`/`q`/`limit` 的 `ge=1, le=_MAX_LIMIT`）、`response_class=HTMLResponse`、内部查询与 HTML 拼接**逐字保留**；把 `_resolve_session_tenant_id` + 手写 `RedirectResponse` 换成 `tenant_id: str = Depends(require_html_session)`。
   - `GET /api/messages` 与 `GET /api/messages/{msgid}`：`response_model=list[MessageOut]` / `MessageDetailOut`、参数（`sender`/`q`/`msgtype`/`roomid`/`limit`，`limit` `Query(20, ge=1, le=_MAX_LIMIT)`）、`Depends(get_current_user)`、404 用 `HTTPException(404)`、recipients 的 `tenant_id` 过滤——**逐字保留**。
   - 从 `app.schemas.messages` 引入 `MessageOut`/`RecipientOut`/`MessageDetailOut`；从 `app.auth` 引入 `get_current_user`/`require_html_session`；从共享模块引入 `_e`/`_fmt_msgtime`/`_badge`/`_PAGE_CSS`。
   - 路由函数名可保持原名（`admin_messages`/`admin_message_detail`/`get_messages`/`get_message`），`include_router` 后不影响 path 与 snapshot。

### 阶段三：建 `routers/web.py`
4. 新建 `backend/app/routers/web.py`，把 3 条 Web 路由 + 它们依赖的 HTML 模板常量（`_REVIEW_CONSOLE_HTML` 等）整段迁入；路由体里 `_resolve_session_tenant_id(request, db)` 换成 `tenant_id: str = Depends(require_html_session)`，`response_class=HTMLResponse` 保留。HTML 模板常量内容**逐字节保留**（含嵌入 JS / i18n 注入点）。

### 阶段四：收敛 `main.py`
5. 在 `main.py` 顶部新增 `from app.routers.web import router as web_router` 与 `from app.routers.messages import router as messages_router`，并在既有 `include_router` 块后追加 `app.include_router(web_router)` / `app.include_router(messages_router)`。
6. 从 `main.py` **彻底删除**被迁走的 7 条路由定义、对应 schema 类、以及随之迁走的 HTML 常量与私有 helper（它们现已在 `routers/web.py` / `schemas/messages.py` / 共享模块里）。**严禁在 main.py 留下副本**——否则 `test_router_count` 会从 33 变成 34 而失败。
7. 确认 `main.py` 仍 `import` 到所有 helper（若 helper 已迁出，删除对应 import，避免 unused/循环 import）。注意 `app/routers/web.py` 与 `app/main.py` 的 import 顺序，避免循环依赖（共享模块不要反向 import main）。

### 阶段五：自测（不达标不收工）
8. 先跑最关键的契约测试：
   - `cd backend && python -m pytest tests/test_http_contract.py -q`
   - 必须全绿，尤其 `test_router_count`（断言 == 33）与 `test_route_snapshot_with_real_model_names`（response_model 名/response_class/path/methods 完全匹配）。
9. 跑本期相关回归套件：
   - `python -m pytest tests/test_auth.py tests/test_password_auth.py tests/test_rnd225_auth_fail_closed.py tests/test_tenant_isolation.py tests/test_admin_timestamp_formatting.py tests/test_admin_auto_refresh.py tests/test_admin_auto_load_older.py tests/test_reachability_diagnostics_page.py tests/test_rnd_206_top_level_image.py -q`
10. 收口跑 `make verify`（lint-diff + typecheck + build + 全量 pytest），确认全绿。

## 3. 兼容性契约（不可违反，验收据此判定）
- **路由总数 == 33**（`test_http_contract.py:318`）。移出 ≠ 新增；若变 34 即证明 main.py 留了副本 → 立即修。
- **路由快照逐字段相等**（`test_route_snapshot_with_real_model_names`）：
  - `/admin/conversations`、`/admin/messages`、`/admin/messages/{msgid}`、`/admin/search`、`/admin/diagnostics/reachability` → `response_class=HTMLResponse`，`response_model=None`。
  - `/api/messages` → `response_model=list[MessageOut]`；`/api/messages/{msgid}` → `response_model=MessageDetailOut`。
  - methods 全为 `GET`；path 与 `test_routers_are_registered` 期望集合完全一致（不增不减、不重名）。
- **认证语义不变**：HTML 路由未认证 → **302** 跳 `/admin/login`（原行为）；`/api/messages*` 未认证 → **401**（`get_current_user` 原行为）。tenant_id 一律从 session 解析，绝不接受请求里的 tenant 参数。
- **租户隔离不变**：每条查询都以 `tenant_id == session.tenant_id` 过滤；`/api/messages/{msgid}` 的 recipients 同样按 tenant 过滤；不存在的消息 → HTML 404 / API 404。
- **查询语义不变**：`/admin/messages` 的 `sender`/`q`/`limit(50,1–100)`；`/api/messages` 的 `sender`/`q`/`msgtype`/`roomid`/`limit(20,1–100)`；排序 `msgtime.desc()`——与改造前逐字一致。
- **响应体兼容**：HTML 页与 JSON 响应形状、字段名、文案不变（legacy tests 与 route snapshot 共同保证）。

## 4. 硬性约束（实现 agent 自身也要守）
- 不修改任何查询语义、不删除 legacy 页面、不改 API 认证方式。
- 不引入 DB migration / schema 变更；不碰 CI、企业名变更、WeCom 回调逻辑。
- 不引入后台进程；假设开发服务器已在运行；所有命令前台运行。
- 不自行 `git commit` / `push` / 开 PR——保留给用户（Haisu）人工 merge 关卡。

## 5. 收尾动作
- 在 Linear 把 RND-218 状态 Todo→In Progress（如尚未）。
- 写一条评论：迁移了哪些路由到哪几个文件、统一了哪个 HTML session 依赖、移除了哪些 main.py 内容、`make verify` 结论、是否保留了 33 路由与 route snapshot。
- 不要自动合入/提交；交还用户决策是否 merge（本任务为 RND-212 链一环，且阻塞 RND-223）。
