[Goal check] This work advances 开发（Development） by 交付 /admin/media 页面模板与路由，对接已就绪的媒体列表 API，并使侧栏「媒体与附件」自动点亮。

# RND-329 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-329「R1-3 media 页面：媒体与附件前端实现」｜Linear team `Builder`
- 优先级：High｜风险等级：**R1**｜波次：R1 · 前端快赢四页
- **可与 RND-327/328/330 并行**（前提：严守下方文件所有权）

## 背景与项目现状
后端 **列表能力已就绪**（已实地核实）：

| 端点 | 位置 | 用途 |
|---|---|---|
| `GET /api/admin/media` | `backend/app/routers/media_library.py:42` | 媒体列表 / 筛选（`MediaLibraryPage`），**RND-334 已补充 `msgid`/`conversation_id`/`session_title` 三个字段** |
| 既有媒体访问路由 | `backend/app/routers/media.py` | 缩略图 / 预览取流（**只读复用，不得修改**） |

## ⚠️ 2026-07-29 更正：AC-4 不是"零后端改动"就能做完的

上一轮开发 agent 正确执行到 `BLOCKED_NEEDS_HUMAN`：`GET /api/admin/media` 当时缺 `conversation_id`/`msgid`/`session_title`，无法拼出既有预览路由 `/api/conversations/{conversation_id}/messages/{msgid}/media` 的 URL——列表接口只给 `room_id` + 数据库 `message_id`（不是 WeCom `msgid`），语义对不上；唯一 `media_id` 键控的路由是下载路由（RND-292，已 Done），强制 `Content-Disposition: attachment`，不能内嵌预览。

**RND-334 已交付这三个字段**（复用 `conversation_membership.py`/`display_names.py` 既有逻辑）。**开工前先读 `GET /api/admin/media` 的实际响应，确认这三个字段的最终名字**（RND-334 的 QA Summary 里会记录，若与本提示词假设的名字不同以实际为准）。

设计稿：`design/Crowntime WeCom Archive Design System/pages/media.html`。

**RND-326 已为你准备好（不要重做，也不要修改）：**
- `design-system.css`、`sidenav.py` 的 `render_sidenav(active_id, registered_paths)`（`active_id="media"`）。
- `backend/app/routers/admin_media_page.py` —— 空 stub router，已在 `main.py` 注册。
- i18n 锚点：`/* RND-329 media page keys — insert below */`（3 个 locale 各一处）。

**❗ 本项目高频踩坑（必读）：**
- **没有模板引擎。** `render_template` 只做 `__TOKEN__` 单遍替换，**不存在 Jinja**。禁止 `{% %}` / `{{ }}`。未提供的 token 抛 `KeyError` → 500。
- **i18n 三语**：`zh-CN` / `zh-TW` / `en`，缺一即 FAIL。
- **架构边界硬闸**：router 不得 import `app.main`；不要动 `main.py`。
- **架构冻结 D1**：SSR + 原生 JS，禁 React / Vue / 构建步骤。
- **媒体存储是双 provider**：`local` 与 `qiniu_kodo`（见 `MEDIA_STORAGE_PROVIDER`）。缩略图/预览必须走既有媒体访问路由，**不得**在前端直接拼七牛 URL 或暴露签名逻辑。

## 目标（Goal）
让管理员在生产环境用 `/admin/media` 页面浏览与筛选归档媒体，并使侧栏「媒体与附件」从灰色占位变为可点击。

## 范围边界

**In scope：**
1. `backend/app/web/templates/media.html` —— 网格 / 列表页：类型、大小、上传时间、所属会话；按类型与时间筛选；缩略图 / 预览。
2. `backend/app/routers/admin_media_page.py` —— 注册 `GET /admin/media`，渲染模板，传 `sidenav=render_sidenav("media", <已注册路径集合>)`。
3. i18n：在**自己的锚点正下方**新增 `media.*` 键（3 个 locale）。
4. `backend/tests/test_media_page.py`。

**Out of scope（显式非目标）：**
- 不改任何后端 API；**特别是不实现下载端点与下载审计钩子** —— 那是 RND-292（A6-2），独立工单，本票不得涉足。
- 不改媒体存储层、不做转码、不改缩略图生成流水线。
- 不碰 `sidenav.py`、不改 `main.py`。
- 不碰其他三张页面票的文件或 i18n 锚点区间。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/media.html`（新）
- `backend/app/routers/admin_media_page.py`
- `backend/app/assets/i18n.js` —— **仅限 `/* RND-329 media page keys */` 锚点正下方**
- `backend/tests/test_media_page.py`（新）

**⚠️ 强制随附改动（2026-07-30 追加授权）：本票新增 1 个路由，因此 `backend/tests/test_http_contract.py` 也属于本票拥有清单** —— 必须同步 ① 第 326 行 `route_count`（**先跑 `make verify` 读当前真实基线 N，改成 N+1，禁止写死数字**）② expected path 集合追加本票新路由 ③ snapshot 列表追加对应条目。**不得**改他票条目。
> 本票的页面路由用 `require_html_session`（不是 `require_role`），因此**不触发** `test_rnd280_rbac_scaffold.py` 的 RBAC 白名单，那个文件不用改。
> 契约测试同步不是独立工单——拆开会让 `main` 在两票之间红灯。详见 `docs/ticket-autopilot-workflow.md` §3.3。

**只读、绝不可写：** `sidenav.py`、`main.py`、`design-system.css`、`routers/media.py`、`routers/media_library.py`、其他 `admin_*_page.py`、其他锚点区间。

> 若发现必须改他人文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）
- **AC-1 路由可用**：`GET /admin/media` 返回 200，HTML 中无残留 `__TOKEN__`。
- **AC-2 真实数据渲染**：网格由 `GET /api/admin/media` 真实响应填充（非 mock），含类型 / 大小 / 上传时间 / 所属会话字段。
- **AC-3 筛选可用**：按媒体类型与时间范围筛选生效，走后端筛选参数（**不得**前端全量拉取后再过滤）。
- **AC-4 缩略图经既有媒体路由**：用 `GET /api/admin/media` 返回的 `conversation_id` + `msgid`（RND-334 交付）拼出 `/api/conversations/{conversation_id}/messages/{msgid}/media`，**不得**在前端拼接七牛地址或包含任何签名密钥。测试须断言模板与 JS 中不出现 `QINIU_` / `qiniu.com` 等直连痕迹。
- **AC-5 导航自动点亮**：`/admin/media` 注册后侧栏「媒体与附件」渲染为 `<a href>`，**且 `sidenav.py` 未被修改**。
- **AC-6 i18n 三语齐全**：新增每个 `media.*` 键在三个 locale 中均存在，且位于 RND-329 锚点下方。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/media.py   # 必须无输出
grep -rn '{%\|{{' backend/app/web/templates/media.html                # 必须无输出
grep -rniE 'qiniu|QINIU_' backend/app/web/templates/media.html        # 必须无输出（AC-4）
```

## 依赖（Dependencies）
**阻塞于 RND-326 与 RND-334**（媒体列表补充会话定位字段）。任一产物缺失 → `BLOCKED_NEEDS_HUMAN`，不要自己在前端猜字段名或自造 conversation_id 计算逻辑。
RND-292（下载端点 + 审计钩子）**已 Done**，不属于本票范围，本票不涉及下载功能本身（只是复用同一批预览/取流路由做展示）。

## 完成定义
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票 4 个拥有文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：媒体是真实归档内容 —— 测试**不得**引入真实聊天媒体、真实用户数据或生产 URL，一律用固定假数据 / mock。
- 风险：大图直出会拖慢页面 —— 网格应使用缩略图路径而非原图。
- 回滚：新增文件为主，`git checkout -- <files>`。

## 人工点位
- **Trigger**：RND-326 与 RND-334 均完成后置 In Progress。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：若 RND-334 交付的字段名/语义与本提示词描述不符，或仍不足以拼出预览 URL → `BLOCKED_NEEDS_HUMAN`，**不要改后端补字段**（那还是 RND-334 或新票的范围）。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`media_library.py`、`media.py`（只读，理解既有访问路由）、`sidenav.py`（只读）、设计稿 `pages/media.html`。
2. 在 `admin_media_page.py` 注册 `GET /admin/media`。
3. 写 `media.html`，`__TOKEN__` 占位，原生 JS 调 API，缩略图走既有媒体路由。
4. i18n 锚点下加 `media.*` 键 ×3 locale。
5. 写 `tests/test_media_page.py` 覆盖 AC-1~AC-6（AC-4 要显式断言无七牛直连痕迹）。
6. `make verify` → QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；凭证只从环境变量读，**绝不写入模板或 JS**。
- 不扩大 Scope；最小正确改动优先。
- 证据优先，以 exit 0 / 测试通过为证。
