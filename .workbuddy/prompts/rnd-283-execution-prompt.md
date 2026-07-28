# RND-283 开发 agent 执行提示词
> 面向开发 agent（单人端到端实现 RND-283 / A2-1 用量分析聚合查询）。
> 本文件即你的完整 brief。全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。
>
> 归属：本票是 Epic **RND-266（A2 用量分析）** 当前唯一的具体子票，是该 Epic 的可执行切片。RND-266 已是 `In Progress`，但其唯一子票 RND-283 在 Linear 仍为 `[BLOCKED: A1-1]`。
>
> **依赖链（务必先读）**：RND-283 的 blocker 是 **A1-1 = RND-281**（`[BLOCKED: F0] A1-1 UsageService 聚合模块`，父 Epic RND-262）。而 RND-281 自身又 `[BLOCKED: F0]`（账号体系重构）。因此**真实前置链为 F0 → RND-281 → RND-283**。RND-281 的模块落地在 `backend/app/services/usageservice.py`（导入名 `app.services.usageservice`），提供概览原语 `getarchivedays` / `count_messages` / `sum_storage` / `count_monitored_employees` / `sync_health`，供 A1-2 / A2-1 / B1-3 复用。A2-1（本票）**复用**这些概览原语填充 4 张指标卡，并**新增** 4 个分析专属聚合（趋势 / 类型构成 / 存储构成 / 按小时分布）。

## 一、任务（一句话）
为「A2 用量分析」实现可执行切片：在 `archive_messages` / `media_files` 上做**租户内**聚合，新增 `/api/admin/usage` JSON 端点与 `/admin/analytics` SSR 页面（4 张图 + 4 张指标卡全部由真实聚合数据渲染），**不暴露任何消息内容、不做导出**。

## 二、精确落点 / 根因（已定位，附 文件:行号:函数）

### 落点 A — 数据来源（已在工作树确认）
- `backend/app/db/models.py:320` `ArchiveMessage`（`__tablename__="archive_messages"`）
  - `tenant_id` (L418, `String(36)`, FK `tenants.id`, indexed) — 租户隔离键
  - `msgtime` (L404, `BigInteger`, epoch ms, indexed) — 趋势 / 按日 / 按小时分桶
  - `msgtype` (L401, `String(32)`, indexed) — 类型构成；经 `app/message_type_registry` 归并为大类
  - `roomid` (L403) / `sender` (L402) / `is_revoked` (L415)
- `backend/app/db/models.py:518` `MediaFile`（`__tablename__="media_files"`）
  - `file_size` (L538, `BigInteger`, nullable) — 字节数，存储构成用
  - `file_type` (L533, `String(32)`) — 媒体类型（image/file/voice/video/...）
  - `archive_message_id` (L527, FK `archive_messages.id`) — 关联回消息以继承 tenant 作用域
  - `tenant_id` (L530)

### 落点 B — 既有聚合 / 页面范式（照抄，不要另起炉灶）
- `backend/app/routers/reachability_audit.py:65` `get_reachability_audit` —— 租户内只读聚合端点标准写法：
  - `db: Session = Depends(get_db)` (L75)
  - `auth: Tuple[AdminUser, str] = Depends(get_current_user)` (L76)
  - `_, tenant_id = auth` (L84)；**tenant_id 仅来自会话，绝不接受请求参数**
- `backend/app/routers/web.py:78` `admin_diagnostics_reachability` —— 控制台页面渲染范式：`require_html_session` 取 `tenant_id`，未登录 `RedirectResponse("/admin/login", status_code=302)`；`render_template("diagnostics", i18n_script=I18N_SCRIPT_TAG)`
- `backend/app/services/usageservice.py`（RND-281 落地，blocker） — 概览聚合原语：`getarchivedays(tenant_id)` / `count_messages(tenant_id)` / `sum_storage(tenant_id)` / `count_monitored_employees(tenant_id)` / `sync_health(tenant_id)`。**实现前先读该模块确认确切函数签名**（RND-281 描述中的小写名为规格示意，实际命名以落地代码为准）。

### 落点 C — 架构边界（违反即 CI 失败）
- `backend/tests/test_architecture_boundary.py` —— 反向依赖守护。`backend/app/services/` 包下模块自动归类为 service 层，**无需**加入 `_FLAT_SERVICE_MODULES`；但新模块**禁止** import `app.routers.*` 或 `app.main`（只能依赖 db / schemas / 其他 service / domain 模块）。
- `backend/app/main.py:94-102` —— 新路由 `app/routers/analytics.py` 须在 `create_app()` 内 `app.include_router(analytics_router)`，**禁止**在 `main.py` 内写 `@app.get(...)` 装饰器（composition-root 守护，见 `_HEALTH_PROBE_PATHS` 白名单）。

### 落点 D — 前端范式（D1 冻结：SSR + 原生 JS，无 React）
- `backend/app/web/templates/diagnostics.html` —— 服务端渲染模板：`__I18N_SCRIPT__`、`__STATIC_VERSION__` 占位符 + `<script src="/web/static/diagnostics.js?...">` 拉数据渲染。**不要**照搬 `design/ui-v1/pages/analytics.html` 的内联脚本硬编码数据写法（那是设计稿，非实现）。
- `backend/app/web/templates/review_console.html:360` 侧栏「数据」分组 —— 在「消息记录」(L361) 之后新增 `<a class="side-nav-item" href="/admin/analytics" data-i18n="nav.usageAnalytics">用量分析</a>`（当前页可加 `active`）。需同步在 `app/assets/i18n.js` 三个 locale 块（zh-CN / zh-TW / en）各加 `nav.usageAnalytics`（中/繁/英）。
- 图表用原生 SVG（参考 `design/ui-v1/pages/analytics.html` 的 SVG 生成逻辑），由 `analytics.js` `fetch('/api/admin/usage')` 后注入 `#trend` / `#donut` / `#hours` / `#storage` 等节点。

## 三、阶段一：复现 + 测量（RED）
1. 启动 backend + 测试 DB（按现有 pytest fixture / `make verify`）。
2. **前置校验 A1-1（RND-281）已合并**：
   - `python -c "import app.services.usageservice"` 成功；
   - `alembic check` 绿。
   - 若失败 → **停下**，在交付说明里报告「A1-1 未就绪（其自身 blocker 为 F0），RND-283 保持 BLOCKED，未实现」，不继续。
3. 基线：访问 `/admin/analytics` 应 404（页面尚未存在）；`GET /api/admin/usage` 应 404。
4. 记录当前 `archive_messages` 行数（tenant 内）作为图表数据量级参考。

## 四、阶段二：实现（GREEN，最小变更）

### 路 A — 复用 A1-1 概览原语 + 新增分析聚合（落点 B）
- **概览指标卡（复用 RND-281）**：`归档天数 = getarchivedays(tenant_id)`、`消息总量 = count_messages(tenant_id)`、`日均消息 = count_messages / 归档天数`、`存储占用 = sum_storage(tenant_id)`（媒体 + 估算文本/索引）。这些**不要**在 RND-283 重新实现 — 直接 `from app.services.usageservice import ...`。
- **分析聚合（本票 net-new）**，新增模块 `backend/app/services/analytics_service.py`（导入名 `app.services.analytics_service`，包下 service 层）：
  1. `trend(db, tenant_id, days)`：`func.count()` group by `date_trunc('day', to_timestamp(msgtime/1000))`，返回当前周期 + 上一周期（前端算环比）。支持 `days=7/30/90`（默认 30）。
  2. `type_composition(db, tenant_id, days)`：`func.count()` group by `msgtype`，再用 `app.message_type_registry.describe_message_type(mt)` / `resolve` 归并为 `text/image/file/voice/video/structured/other`（**复用 registry，不要自己维护映射表**）。
  3. `storage_composition(db, tenant_id)`：`func.sum(MediaFile.file_size)` join `archive_messages` on `archive_message_id`，group by 媒体大类（image/file/voice/video）；`文本与索引` 切片 = 文本类消息数 × 文档化常量 `ESTIMATED_TEXT_BYTES`（明确为估算，标注）。单位 B→GB。
  4. `hourly_distribution(db, tenant_id, days)`：`func.count()` group by `extract(hour from to_timestamp(msgtime/1000))` → 24 桶。
- 所有查询 `where(ArchiveMessage.tenant_id == tenant_id)`，用 `select(...)`。

### 路 B — JSON 端点
- 新建 `backend/app/routers/analytics.py`：
  - `@router.get("/api/admin/usage", response_model=UsageAnalyticsOut)`
  - 依赖 `get_current_user`（租户内），`db: Session = Depends(get_db)`
  - 返回 `{overview:{archived_days,total_messages,avg_daily,storage_bytes}, trend, type_composition, storage_composition, hourly_distribution, meta:{period_days}}`
  - **零内容泄露**：payload 只含聚合计数 / 字节 / 占比，**绝不**含 `msgid` / `sender` / `roomid` / `content_text` / `decrypted_payload` / `tolist` / `sdkfileid` / 任何原始标识符。

### 路 C — SSR 页面 + 前端
- `backend/app/routers/web.py` 新增 `admin_analytics`：`@router.get("/admin/analytics", response_class=HTMLResponse)`，范式同 `admin_diagnostics_reachability`（L78）。
- 新建 `backend/app/web/templates/analytics.html`：以 `diagnostics.html` 为骨架，`<div id="analytics-root">` 占位，`<script src="/web/static/analytics.js?v=__STATIC_VERSION__">`。
- 新建 `backend/app/web/static/analytics.js`（原生 JS，无 React）：`fetch('/api/admin/usage')` → 注入 4 张 SVG（趋势折线 / 类型环形 / 存储堆叠条 / 按小时柱状）+ 4 张指标卡，复用 design 文件的 SVG 生成逻辑但数据来自接口。
- **删除 design 中的「导出 Excel」按钮**（Non-goals：不做导出，见 C2）—— 真实页面不放任何导出入口。
- `review_console.html:360` 数据分组加 analytics 导航项 + `app/assets/i18n.js` 三个 locale 块各加 `nav.usageAnalytics`。

### 路 D — 接线
- `backend/app/main.py` 顶部 `from app.routers.analytics import router as analytics_router`，`create_app()` 内 `app.include_router(analytics_router)`（紧跟 L101 `web_router` 之后）。

## 五、阶段三：验证（GREEN + 回归 + make verify）
1. 功能：访问 `/admin/analytics` → 4 图 + 4 卡均渲染真实数据；`GET /api/admin/usage` 返回非 0 聚合且**无内容字段**（用 `jq` 确认无 `msgid`/`sender`/`content` 等键）。
2. 回归：`make verify`（lint-diff typecheck build test）全绿；重点：`test_architecture_boundary.py`、`test_http_contract.py`（路由数基线——新增 2 条路由属预期，若测试 pin 了精确路由数须同步更新）、`test_password_auth.py`、`test_i18n_foundation.py`。
3. 失败先修实现，不迁就测试（除非测试断言旧路径，须标注）。

## 六、硬约束（违反即判失败）
- 不改 URL / status / body / OpenAPI / 租户隔离 / i18n 约定。
- `tenant_id` 仅来自认证会话，绝不来自请求参数（与 `reachability_audit.py:84` 一致）。
- 不暴露任何消息内容（`msgid`/`sender`/`roomid`/`content_text`/`payload`/`tolist`/`sdkfileid`）。
- 不做导出功能（删除 design 的导出按钮）。
- 不新建 React / 前端框架（D1 冻结：SSR + 原生 JS）。
- 不重复实现 A1-1（RND-281）的职责：概览原语直接复用 `app.services.usageservice`；若 A1-1 未合并，停下报告，不自行补 `UsageService` 模块。
- 新聚合模块放 `backend/app/services/analytics_service.py`，禁止 import `app.routers.*` / `app.main`。
- 不执行 git commit / push。
- 与相关工作树改动无冲突前提下最小化改动；冲突则停下报告。

## 七、收尾（交付物）
向用户交付：A1-1 就绪确认（或 BLOCKED 报告，含「其自身 blocker 为 F0」）/ RND-281 实际函数签名清单、`/api/admin/usage` 真实数据样本（脱敏）、改动文件清单、`make verify` 日志、未提交声明。
