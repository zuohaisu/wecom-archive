# RND-216 执行提示词（机械外置管理后台 HTML / CSS / JS）

> 用途：粘贴给执行引擎（Claude Code / GPT-5.6 high，工单推荐流），由其单人端到端跑完 RND-216。
> 本任务是 RND-212「模块化单体」重构链的第一块砖，会被 RND-218 / RND-223 / RND-217 依赖。
> 它不碰 DB migration / CI / 企业名变更；与 RND-225（授权语义）、RND-226（nested media）无文件级冲突，可并行。

---

## 0. 任务与来源
- Linear 工单：**RND-216「机械外置管理后台 HTML、CSS 与 JavaScript」**，优先级 P1，父任务 RND-212。
- 目标：把目前内联在 `backend/app/main.py` 里的管理后台前端资源搬到独立文件——
  - HTML → `backend/app/web/templates/`
  - CSS / JS → `backend/app/web/static/`
  - 配置 `StaticFiles` 挂载 + 明确的 cache-busting
  - **第一阶段保持单文件 JS、现有全局函数与事件模型**
  - 调整现有前端测试，使其不再 `import` Python 私有 HTML 常量
- 非目标（严禁）：不模块化 JS；不重写 DOM；不引入 React/Vue 或构建工具链；不增加产品功能。
- 验收标准（逐条对照，见第 5 节）：现有 URL、status、DOM、API 请求与 i18n 初始化顺序不变；静态资源无 404；refresh / rich-media / frontend 测试全过；真实浏览器 smoke 通过；**可经「恢复 inline strings」独立回滚**。
- 推荐执行：Claude Code / GPT-5.6 high。

---

## 1. 现状盘点（精确文件 / 行号，基于当前 `main.py`）

### 1.1 内联 HTML 常量（待外置为模板文件）
| 常量 | 行号 | 服务路由 | 内容特征 |
|------|------|----------|----------|
| `_REVIEW_CONSOLE_HTML` | L384 | `/admin/conversations` (L3550) | 三栏会话审阅台；内嵌大段 `<script>`（review-console JS，~L560 起）；L562 注入 `I18N_SCRIPT_TAG` |
| `_SEARCH_PAGE_HTML` | L2820 | `/admin/search` (L3558) | 搜索结果页；L2972 注入 `I18N_SCRIPT_TAG`，L2974 内嵌 `var MSGTYPE_OPTIONS=...JSON...` |
| `_DIAGNOSTICS_HTML` | L3357 | `/admin/diagnostics/reachability` (L3566) | 可达性诊断页；L3370 注入 `I18N_SCRIPT_TAG` |
| `/admin/messages` 与 `/admin/messages/{msgid}` 的 inline body | L277、L315、L339 | L235、L300 | 共用 `_PAGE_CSS`，纯服务端拼字符串，无内嵌 JS |

### 1.2 内联 CSS 常量（待外置为 static 文件）
- `_PAGE_CSS` (L132)：基础页面样式，**被 `_DIAGNOSTICS_CSS` 复用**（L3318-3319 拼入）。
- `_DIAGNOSTICS_CSS` (L3318)：= `_PAGE_CSS` + 诊断页专属样式。

### 1.3 内联 JS 里插值的「动态 JSON」（**保留在 `app.main` 作数据常量，不移动**）
- `_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON` (L372，由 `build_frontend_registry_entries()` 生成)
  - 被 `_REVIEW_CONSOLE_HTML` 在 L1013 以 `var entries=""" + _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON + """;` 插值进 review-console JS 的 `MessageTypeRegistry` IIFE。
- `_SEARCH_MSGTYPE_OPTIONS_JSON` (L382，由 `build_filterable_type_options()` 生成)
  - 被 `_SEARCH_PAGE_HTML` 在 L2974 以 `var MSGTYPE_OPTIONS=""" + _SEARCH_MSGTYPE_OPTIONS_JSON + """;` 插值进 search JS。

> 关键点：`_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON` / `_SEARCH_MSGTYPE_OPTIONS_JSON` 是**数据**，不是 HTML。外置后它们**仍留在 `app.main`**，经模板上下文注入到模板里的小段 inline `<script>`（见第 2 节）。这样 `test_message_type_registry_core.py` 对这两个常量的 import（L741、L944）**无需改动**。

### 1.4 i18n（**保持内联注入，不外置**）
- `I18N_SCRIPT_TAG` 来自 `app/i18n_assets.py`，内容即 `backend/app/assets/i18n.js`。
- 它在三个 HTML 常量里分别于 L562 / L2972 / L3370 注入，且**必须位于页面自有 `<script>` 之前**（页面 JS 调用 `I18N.t(...)`）。
- 外置后，模板里仍需保留 `{{ i18n_script | safe }}` / 占位替换，且顺序不变。

### 1.5 现有依赖（**不新增 jinja2**，理由见第 2 节）
`backend/requirements.txt` 只有 fastapi/uvicorn/pydantic-settings/psycopg2/sqlalchemy/alembic/httpx/qiniu/Pillow/cryptography/python-dotenv。**无 jinja2**。

---

## 2. 目标架构（执行引擎应采用的方案）

### 2.1 目录布局（新建）
```
backend/app/web/
  __init__.py            # 提供 render_template(name, **ctx) 极简助手
  templates/
    review_console.html  # 来自 _REVIEW_CONSOLE_HTML（去掉与 JSON 相关的 literal 行）
    search.html          # 来自 _SEARCH_PAGE_HTML
    diagnostics.html     # 来自 _DIAGNOSTICS_HTML
    messages.html        # /admin/messages 列表页（含 404 复用同一模板片段或独立）
    message_detail.html  # /admin/messages/{msgid} 详情 + 404
  static/
    base.css             # 来自 _PAGE_CSS
    diagnostics.css      # 来自 _DIAGNOSTICS_CSS（含 base.css 内容或模板同时 link 两者）
    review-console.js    # 来自 _REVIEW_CONSOLE_HTML 内嵌 <script>（去掉 entries JSON literal）
    search.js            # 来自 _SEARCH_PAGE_HTML 内嵌 <script>（去掉 MSGTYPE_OPTIONS JSON literal）
    diagnostics.js       # 来自 _DIAGNOSTICS_HTML 内嵌 <script>
```

### 2.2 渲染方式：**极简内置占位替换，不引入模板引擎**
- 不要 `pip install jinja2`，不要 `Jinja2Templates`。理由：工单明确「不引入构建工具链」；`Makefile` `build` 注释当前声明「everything else is inlined into main.py's Python source / i18n.js 是唯一的独立 JS asset」——添加模板引擎违背该架构极简约定。
- 在 `app/web/__init__.py` 提供一个 `render_template(name: str, **ctx) -> str`：
  - `text = (TEMPLATES_DIR / f"{name}.html").read_text(encoding="utf-8")`
  - 用显式 token 替换：`__I18N_SCRIPT__` → `I18N_SCRIPT_TAG`；`__MTR_ENTRIES_JSON__` → `_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON`；`__MSGTYPE_OPTIONS_JSON__` → `_SEARCH_MSGTYPE_OPTIONS_JSON`；并可加 `__STATIC_VERSION__` → 见 2.4。
  - 模板里用 `{{ token }}` 或直接 `__TOKEN__` 都行，选一种并保持一致。
- 路由改为：
  ```python
  from app.web import render_template, STATIC_VERSION
  @app.get("/admin/conversations", response_class=HTMLResponse)
  def admin_conversations(request, db=Depends(get_db)):
      if _resolve_session_tenant_id(request, db) is None:
          return RedirectResponse("/admin/login", status_code=302)
      return HTMLResponse(content=render_template("review_console"))
  ```

### 2.3 静态资源挂载 + cache-busting
- 在 `main.py` 末尾（API 路由之后）挂载：
  ```python
  from pathlib import Path
  from fastapi.staticfiles import StaticFiles
  _STATIC_DIR = Path(__file__).parent / "web" / "static"
  app.mount("/web/static", StaticFiles(directory=str(_STATIC_DIR), check_dir=False), name="web-static")
  ```
- cache-busting：在 `app/web/__init__.py` 进程启动时对 `_STATIC_DIR` 全部文件内容求哈希（如 `hashlib.md5(concat).hexdigest()[:8]`）得到 `STATIC_VERSION`；模板里引用 `<script src="/web/static/review-console.js?v={{STATIC_VERSION}}">`、`<link rel="stylesheet" href="/web/static/base.css?v={{STATIC_VERSION}}">`。部署重启即刷新版本（与「无构建工具链」一致）。

### 2.4 ⚠️ 关键坑：全局 `no-store` 中间件会破坏静态缓存
- `main.py` L60 `app.add_middleware(MediaAccessNoStoreMiddleware)`（RND-187，来自 `app/routers/conversations.py`）对**每一个**响应写 `Cache-Control: no-store`，包含 `/web/static/*`，会令 cache-busting 失效。
- **必须**让静态路径豁免该中间件：在 `MediaAccessNoStoreMiddleware.dispatch` 开头加
  ```python
  if scope.get("path", "").startswith("/web/static"):
      return await self.app(scope, receive, send)
  ```
- 豁免后，静态响应需带显式长缓存：通过包装 `StaticFiles` 或在返回前设置
  `response.headers["Cache-Control"] = "public, max-age=31536000, immutable"`。
  结合 `?v=` 版本串实现「内容变 → URL 变 → 强制刷新」。
- 这是验收「静态资源不存在 404 / 明确的 cache-busting」与「可独立回滚」能否成立的前提，勿遗漏。

### 2.5 动态 JSON 的注入（保持现有全局变量名，最小改动）
- review-console 模板在 `I18N_SCRIPT_TAG` 之后插入一小段 inline `<script>`：
  ```html
  <script>var __MTR_ENTRIES__ = __MTR_ENTRIES_JSON__;</script>
  ```
  `review-console.js` 中原来的 `var entries=""" + _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON + """;` 改为 `var entries = window.__MTR_ENTRIES__;`（仅改这一行，其余 IIFE / 全局函数 / 事件模型原样保留）。
- search 模板同理：`<script>var __MSGTYPE_OPTIONS__ = __MSGTYPE_OPTIONS_JSON__;</script>`，`search.js` 中 `var MSGTYPE_OPTIONS=...JSON...;` 改为 `var MSGTYPE_OPTIONS = window.__MSGTYPE_OPTIONS__;`。
- 这样「现有全局函数与事件模型」完全不变，仅把 JSON literal 从 JS 文件移到模板注入的全局，行为等价。

---

## 3. 执行步骤（严格按顺序）

### 阶段一：搭骨架（不删任何旧常量，先并行存在）
1. 新建 `backend/app/web/__init__.py`：`TEMPLATES_DIR`、`STATIC_VERSION`、极简 `render_template()`。
2. 新建 `backend/app/web/templates/` 与 `backend/app/web/static/`，把 5 个 HTML body、2 个 CSS、3 个 JS 抽成对应文件（见 2.1）。抽取时**逐字搬运**，仅做 2.5 节的 JSON literal 替换。
3. 在 `main.py` 挂载 `/web/static`（2.3），并豁免 `MediaAccessNoStoreMiddleware`（2.4）。
4. 路由改为调用 `render_template(...)`；确认 `make build`（`from app.main import app` 能构造）通过。

### 阶段二：删旧常量、接模板
5. 从 `main.py` 删除 `_PAGE_CSS` / `_REVIEW_CONSOLE_HTML` / `_SEARCH_PAGE_HTML` / `_DIAGNOSTICS_CSS` / `_DIAGNOSTICS_HTML` 五大字符串常量（及其在 L280/318/342 对 `_PAGE_CSS` 的引用）。
6. `_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON` / `_SEARCH_MSGTYPE_OPTIONS_JSON` **保留**在 `main.py`（数据常量），供 `render_template` 注入。
7. 验证 `from app.main import app` 仍能构造、`make build` 通过。

### 阶段三：改测试（使其不再 import 私有 HTML 常量）
8. 下列 **21 个测试文件**当前 `from app.main import _REVIEW_CONSOLE_HTML / _SEARCH_PAGE_HTML / _DIAGNOSTICS_HTML`，改为读取对应模板 / 静态 JS 文件（路径：`Path(__file__).parent.parent / "app" / "web" / "templates" / "<name>.html"` 与 `.../static/<name>.js`）。
   - `_REVIEW_CONSOLE_HTML` 使用者（17 个）：`test_message_type_registry.py`(L37)、`test_admin_auto_load_older.py`(L46)、`test_rnd_206_rich_media.py`(L29)、`test_i18n_foundation.py`(L137,334,373)、`test_rnd229_focus_locate.py`(L39)、`test_rnd_207_thumbnail_frontend.py`(L16)、`test_rnd_204_incremental_refresh.py`(L38)、`test_admin_auto_refresh.py`(L34)、`test_rnd_206_qa_fixes.py`(L34)、`test_unsupported_message_labels.py`(L43)、`test_message_type_registry_core.py`(L58)、`test_rnd_198_frontend.py`(L22)、`test_admin_chat_bubble_style.py`(L30)、`test_revoke_frontend_render.py`(L28)、`test_rnd_210_msgtype_and_card.py`(L52)、`test_admin_timestamp_formatting.py`(L28)、`test_admin_group_participant_overflow.py`(L37)。
   - `_SEARCH_PAGE_HTML` 使用者（3 个，含上面已列的 `test_message_type_registry_core.py`）：`test_search_page_js_syntax.py`(L19)、`test_rnd_230_search_page_participants_js.py`(L38)。
   - `_DIAGNOSTICS_HTML` 使用者（2 个）：`test_reachability_diagnostics_render.py`(L31)、`test_reachability_diagnostics_page.py`(L107,114,125,131,138,147,157,169)。
9. **Node 执行类测试特殊注意**：`test_admin_auto_load_older.py`、`test_rnd_206_qa_fixes.py`、`test_rnd_210_msgtype_and_card.py`、`test_revoke_frontend_render.py`、`test_rnd_198_frontend.py` 等会从 HTML 里 `re.search(r'<script>...</script>')` 抽出 JS 在 Node 下执行。外置后，JS 已在 `app/web/static/<page>.js`，这些测试应**直接读取该 `.js` 文件**再跑 Node，不要再试图从 HTML 抽 `<script>`（模板里不再含页面 JS 主体，只剩注入 JSON 的小段）。
10. `test_message_type_registry_core.py` 对 `_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON` / `_SEARCH_MSGTYPE_OPTIONS_JSON` 的 import（L741、L944）**保持不变**——这两个常量仍在 `app.main`。
11. `test_i18n_foundation.py` 对 `auth.py` 的 `_I18N_BOOTSTRAP_JS` import 与 i18n.js 直读**保持不变**（登录页不在本任务范围）。

### 阶段四：同步 Makefile 假设（否则 `make verify` 语义失真）
12. 更新 `Makefile` `build` 目标注释——当前声称「i18n.js 是唯一的独立 JS asset / everything else is inlined into main.py」。改为说明现在 `app/web/static/*.js` 也是独立 asset。
13. 在 `Makefile` `build` 目标里，对新增的 `backend/app/web/static/*.js` 增加 `node --check`（与现有 i18n.js 检查并列），使坏 JS 在构建期即失败。

---

## 4. 硬性约束（不可违反）
- **URL / status / DOM / API 请求 / i18n 初始化顺序完全不变**：只搬文件，不重排 DOM、不改 `<script>` 加载顺序（`I18N_SCRIPT_TAG` inline 必须在页面 JS 之前）。
- **不模块化 JS、不重写 DOM、不引入框架/构建工具**（无 React/Vue/Vite/esbuild，无 jinja2 新增依赖）。
- **保持单文件 JS**：每个页面一个 `.js`，全局函数与事件模型原样保留。
- **不碰 DB migration / CI / 企业名变更**；不混入 RND-225 / RND-226 授权语义改动。
- **静态资源无 404**：所有 `<link>` / `<script src>` 路径必须真实存在且经 `/web/static` 可达。
- **参考 `DEV_AGENT_RULES.md` 的 AI 工作流；不要自行 `git commit`/`push`**（需用户 Haisu 显式授权）。
- **可独立回滚**：本任务改动应局部、可聚焦；回滚 = 恢复 main.py 的内联字符串 + 删 `app/web/`（或 revert 本 PR）。不要把本任务与非 RND-216 的重构纠缠在一起，确保「恢复 inline strings」即可回到外置前状态。

---

## 5. 验收（逐条对照工单）
- [ ] 现有 URL（`/admin/messages`、`/admin/messages/{msgid}`、`/admin/conversations`、`/admin/search`、`/admin/diagnostics/reachability`）与 HTTP status 不变。
- [ ] DOM 结构、既有全局函数、事件监听器、API 请求路径与参数不变。
- [ ] i18n 初始化顺序不变（`I18N.t` 在页面 JS 调用前可用）。
- [ ] 静态资源无 404：浏览器加载页面时 `base.css` / `diagnostics.css` / `review-console.js` / `search.js` / `diagnostics.js` 全部 200，且带 `?v=` 版本串 + 长缓存 `Cache-Control`。
- [ ] `make test`（refresh / rich-media / frontend 全量 pytest，含 Node 抽取执行）通过。
- [ ] `make verify`（lint-diff → typecheck → build → test）全绿；`build` 目标的 i18n.js 注释与 `node --check` 已覆盖新 static JS。
- [ ] 真实浏览器 smoke：登录 → `/admin/conversations` 加载、搜索跳转（RND-229）、刷新/自动加载更旧、rich-media 渲染、可达性诊断页均正常。
- [ ] 回滚验证：确认 git revert 本 PR（或恢复 inline 字符串）即可回到外置前，无残留依赖。

---

## 6. 收尾动作
- 在 Linear 把 RND-216 状态 Todo→In Progress；完成后→Done。
- 写一条 Linear 评论：外置了哪些文件 / 静态挂载与 cache-busting 方案 / no-store 豁免处理 / 测试调整数量（21 个）/ `make verify` 结论 / 浏览器 smoke 结果。
- **不要自动合入/提交**——保留给用户（Haisu）人工 merge 关卡。RND-216 是 RND-212 链起点，后续 RND-218/223/217 会基于 `app/web/` 结构继续，故合并前请确认分支干净、不与并行任务冲突。
