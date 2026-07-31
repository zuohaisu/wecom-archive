# RND-217 执行提示词（模块化 Review Console JavaScript）

> 用途：粘贴给实现智能体（**Claude Code 主实现**；**Codex QA**，依工单"推荐执行 Claude Code / GPT-5.6 high；Codex QA"）。
> 性质：纯结构迁移 / 前端模块化重构，**不改变对外 API、DOM 行为、视觉或产品功能**。
> 父任务：RND-212（渐进式重构为 AI 友好的模块化单体）。同级链：RND-216 → **RND-217** → RND-218 → …。

---

## 0. 任务与来源

- **Linear 工单**：RND-217「模块化 Review Console JavaScript」，优先级 Medium(P2)，parent=RND-212。
- **目标**：把 review console 的 API、状态、列表、时间线、消息渲染、媒体 viewer 与刷新调度拆成**可独立理解和测试**的前端模块。
- **范围（8 个模块）**：`api-client`；`console-state`；`conversation-list`；`timeline`；`message-renderers`；`media-viewer`；`refresh`；`console-entry`。
- **非目标（严禁）**：不引入新框架；不改视觉/CSS；不增加产品功能；不改任何 HTTP 契约或后端。
- **验收标准**：
  - API 与 DOM 行为兼容；
  - unchanged refresh 不替换现有媒体节点；
  - stale response guard、滚动保持、媒体播放状态保持；
  - 各模块可独立 syntax/test；
  - Node tests 与真实浏览器 smoke 通过。

---

## 1. 现状与硬约束（先读再动）

当前 review console 的 JS 以**内联 `<script>` 块**形式嵌在 `backend/app/main.py` 的模块级常量 `_REVIEW_CONSOLE_HTML` 中（JS 位于 `main.py:605` 的 `<script>` 到 `main.py:2815` 的 `</script>` 之间，约 2200 行）。`GET /admin/conversations` 直接 `return HTMLResponse(content=_REVIEW_CONSOLE_HTML)`（`main.py:3550`）。

`main.py` 内有 **3 个 `<script>` 块**，RND-217 **只动第 1 块（review console）**：
- 块 1：`main.py:605–2815` → `_REVIEW_CONSOLE_HTML`（**本任务范围**）
- 块 2：`main.py:2973–3310` → `_SEARCH_PAGE_HTML`（搜索结果页，**不动**）
- 块 3：`main.py:3383–3543` → `_DIAGNOSTICS_HTML`（诊断页，**不动**）

### 1.1 测试契约（最关键，违反即 CI 红）
下列测试**直接 `from app.main import _REVIEW_CONSOLE_HTML`**，并通过**(a) 对该常量做字符串/regex 断言**或 **(b) 用 regex 从常量抽取 JS 函数体、以 `subprocess.run([NODE,"-e",harness])` 在裸 Node 下执行**：
- `test_admin_auto_refresh.py`
- `test_admin_auto_load_older.py`
- `test_admin_timestamp_formatting.py`
- `test_rnd_207_thumbnail_frontend.py`
- `test_rnd_198_frontend.py`
- `test_admin_chat_bubble_style.py`（断言 `<style>` 中的 CSS 自定义属性，且抽取 `function timelineRowHtml(m){`）
- `test_admin_group_participant_overflow.py`

**共同前提**（`test_rnd_206_rich_media.py` 定义 canonical `_extract`/`_run`）：
1. `_REVIEW_CONSOLE_HTML` 必须仍可 import 且**包含完整 JS 源**；
2. 下列函数签名必须仍存在且可被 regex 命中：`function timelineRowHtml(m){`、`function renderCompositeMessage(m){`、`function refreshNow(reason)`、`function refreshTimelineIfSelected(){`、`function preserveScrollPosition(body,beforeHeight)`、`function isNearTop()`、`function fmtTime(ms){`、`function esc(s){` 等；
3. **硬性连续块**：`var MediaAccessCache=(function(){ … })();` … `function renderCompositeMessage(m){ … }`（当前 `main.py:1428→2024`）被 4 个测试作为**单一连续片段**抽取（`_REVIEW_CONSOLE_HTML[start:end]`）。该片段在拆分后**必须仍然连续且内容合法**；
4. `<style>` 块必须仍留在 `_REVIEW_CONSOLE_HTML` 中（`test_admin_chat_bubble_style` 依赖）；
5. `I18N`、`esc`/`fmtTime`/`pad`/`handleUnauth` 必须全局可用（每个测试 bundle 都依赖）。

> 结论：**采用 Strategy A（低风险）**——把 JS 抽到 8 个 `.js` 文件，但仍以**经典全局脚本（无 `import`/`export`、无 IIFE 命名空间）**在 `main.py` 中按依赖顺序拼接回 `_REVIEW_CONSOLE_HTML`。这样所有既有测试**零改动**保持绿灯，同时获得可独立 `node --check` 与独立阅读的源文件。**严禁 ES module（`type="module"` + `import/export`）**——那会强制改写全部测试 harness 与所有调用点，超出本任务范围。

---

## 2. 目标模块布局

新建目录 `backend/app/assets/console/`，8 个文件。函数归属（行号为 `main.py` 当前位置，仅作切片指引；实施时以函数体边界为准，**逐字搬运、不改逻辑**）：

### 2.1 `console-state.js` — 全局可变状态 + 共享纯工具
- 状态：`var mode,selEntityId,selConvId,selEntityName,selConvName`(606)；`lastEntityItems,lastConvItems`(607)；`lastEntitySig,lastConvSig`(615)；`timelineConvId,timelineMsgs,timelineHasOlder,timelineNextBefore,timelineLoadingOlder`(616)；`timelineConvType,timelineMode,timelineEntityId`(622)；`timelineHistoryError,timelineTopObserver`(623)；`timelineRequestGen`(631)；`lastRenderedTimelineSignature`(636)；`REFRESH_INTERVAL_SEC=30`(637)；`refreshCountdownSec,refreshTickTimer,refreshInFlight,refreshErrorText,lastRefreshAt`(638-642)；`focusMsgId`(2517)；`focusPending`(2523)；`searchTimer,searchLastQ`(2533)。
- 共享工具：`esc`(643)；`fmtTime`(646)；`pad`(655)；`handleUnauth`(656)。
- 注：`I18N` 由 `I18N_SCRIPT_TAG` 注入（不属于本文件）。

### 2.2 `api-client.js` — HTTP/fetch 封装
- `loadCurrentUser`(715)；`doLogout`(722)；`loadEntityList`(741)；`loadConversations`(799)；`loadTimeline`(848)。
- 注：`fetchTimelinePage`(882)/`fetchOlderMessages`(938) 归入 `timeline.js`；`timelineEntityQueryParams`(867) 归入 `timeline.js`；`fetchDescriptorWithRecovery`(1474) **留在 `message-renderers.js`**（连续块约束）。

### 2.3 `conversation-list.js`
- `entityListSignature`(747)；`convListSignature`(752)；`renderEntityList`(759)；`onEntityClick`(789)；`renderConvList`(807)；`onConvClick`(841)。

### 2.4 `timeline.js`
- `timelineEntityQueryParams`(867)；`fetchTimelinePage`(882)；`isNearTop`(912)；`preserveScrollPosition`(917)；`historyStatusEl`(921)；`showLoadingOlder`(922)；`showEndOfHistory`(926)；`historyRetryHtml`(930)；`showHistoryRetry`(934)；`fetchOlderMessages`(938)；`loadOlderAutomatically`(963)；`retryLoadOlder`(987)；`startHistoryObserver`(991)；`stopHistoryObserver`(1003)；`timelineSignature`(2175)；`timelineRowHtml`(2215)；`renderTimeline`(2251)；`isNearBottom`(2274)；`scrollTimelineToBottom`(2279)；`showNewMessageIndicator`(2284)；`hideNewMessageIndicator`(2288)；`mergeMessagesByMsgid`(2292)；`buildTimelineRowNode`(2357)；`syncHistoryStatus`(2364)；`applyRefreshScroll`(2374)；`applyTimelineRefresh`(2389)。

### 2.5 `message-renderers.js` — **必须包含连续块 1428→2024 原样**
- `MEDIA_LABELS`(1006)；`MEDIA_STATUS_LABELS`(1007)；`rebuildMediaLabels`(1008)；
- `MessageTypeRegistry`(1012，含 `resolve`1015/`resolvePlaceholder`1018/`resolveSystem`1024)；
- `isSafeUrl`(1035)；`hostnameOf`(1042)；`fmtCoord`(1045)；
- 全部 `render*Card`/`renderStructuredCard`/`renderCardMessage`/`renderAudioArchiveMessage`/`renderAudioDocMessage`/`renderSystemCard`(1051-1401)；`STRUCTURED_CARD_RENDERERS`(1379)；
- **连续块（不可拆）**：`var MediaAccessCache=(function(){ … })`(1428) … `fetchDescriptorWithRecovery`(1474) … `hydrateRichMedia`(1661)/`loadRichMedia`(1664)/`buildErrorBox`(1674)/`showRichMediaError`(1687)/`handleRichMediaPlaybackFailure`(1700)/`swapRichMediaPlaceholder`(1715)/`richMediaPlaceholder`(1815)/`renderViewableMediaSlot`(1834)/`renderVideoPreview`(1851)/`renderVoicePreview`(1858)/`renderFilePreview`(1861)/`renderEmotionPreview`(1864)/`renderNestedImageSlot`(1867)/`thumbSlotOpts`(1874)/`renderMediaPreviewByKind`(1885)/`COMPOSITE_MAX_DEPTH`+`COMPOSITE_MEDIA_KINDS`(1904-1905)/`renderCompositeNodeMedia`(1907)/`renderCompositeStructured`(1923)/`renderChatrecordCard`(1936)/`renderCompositeNode`(1945)/`renderCompositeChildren`(1982)/`renderMixedMessage`(1997)/`renderChatrecordMessage`(2013)/`renderCompositeMessage`(2024)；
- `renderRevokePlaceholder`(2031)；`renderMessageBody`(2051)；`safeRenderMessageBody`(2150)。

### 2.6 `media-viewer.js` — **从 1428→2024 连续块中"搬出"的 viewer 代码**
当前 viewer 状态/函数**内嵌在连续块中间**（`main.py:1505–1610` 一带）：`timelineViewerItems`(1505)；`registerViewerItem`(1520)；`viewerItems`(1520)；`viewerIndex`(1521)；`viewerKeyHandlerBound`(1522)；`viewerGen`(1526)；`viewerFocusTrigger`(1527)；`ensureViewerRoot`(1529)；`refreshViewerLabels`(1563)；`openViewer`(1575)；`restoreViewerFocus`(1586)；`closeViewer`(1598)；`openChatrecordViewer`(1607)；`viewerShow`(1610)，以及仅被它们引用的 viewer 私有 helper。
- **搬迁规则**：把这些 viewer 代码从 `message-renderers.js` 的 `MediaAccessCache…renderCompositeMessage` 跨度中**移除**，放入 `media-viewer.js`。搬迁后 `message-renderers.js` 中该跨度仍须连续（MediaAccessCache → renderCompositeMessage）且合法——viewer 函数仅在 `renderViewableMediaSlot`/`renderChatrecordCard` 里以**字符串 onclick**（`"openViewer(...)"`）形式引用，运行时经全局 hoisting 由 `media-viewer.js` 解析，测试抽取片段不会在求值期调用它们。

### 2.7 `refresh.js`
- `refreshEntityList`(2306)；`refreshConversationList`(2329)；`refreshTimelineIfSelected`(2423)；`_refreshErrMsg`(2460)；`setRefreshError`(2461)；`updateRefreshStatus`(2465)；`scheduleNextRefresh`(2480)；`refreshNow`(2484)；`tickRefreshCountdown`(2497)；`startAutoRefresh`(2506)。
- 经全局作用域读取 `conversation-list` 的 `entityListSignature`/`convListSignature` 与 `timeline` 的 `timelineConvId`/`timelineRequestGen`。

### 2.8 `console-entry.js` — **必须最后拼接**（含 bootstrap 副作用）
- `applyStaticI18n`(660)；`renderLangMenu`(666)；`toggleLangMenu`(677)；`selectLocale`(684)；`applyLocale`(690)；`document.addEventListener('click',…)`(710)；`setMode`(727)；
- `focusMessage`(2720)；`focusCheckRow`(2751)；`showFocusBanner`(2772)；`readFocusFromUrl`(2781)；
- `onSearchInput`(2788)；`onSearchBlur`(2799)；`onSearchFocus`(2802)；`#search-input` 监听器(2806-2814)；
- **bootstrap 顶层语句**（原 `main.py:2524–2530`）：`applyStaticI18n(); loadCurrentUser(); startAutoRefresh(); readFocusFromUrl();`——必须成为拼接后脚本的**最后可执行行**（放 `console-entry.js` 末尾）。

---

## 3. `main.py` 接线方式（拼接回常量）

1. 在 `backend/app/` 下新增（或就近）小助手，例如 `console_assets.py`：
   ```python
   import pathlib
   _CONSOLE_JS_DIR = pathlib.Path(__file__).parent / "assets" / "console"
   _CONSOLE_JS_MODULES = [
       "console-state.js", "api-client.js", "conversation-list.js",
       "timeline.js", "message-renderers.js", "media-viewer.js",
       "refresh.js", "console-entry.js",
   ]
   def read_console_js() -> str:
       return "\n".join(
           (_CONSOLE_JS_DIR / n).read_text(encoding="utf-8") for n in _CONSOLE_JS_MODULES
       )
   ```
2. 修改 `_REVIEW_CONSOLE_HTML`：保留其 `<style>` + `<body>` 标记（及 `""" + I18N_SCRIPT_TAG + """` 注入），将内联 `<script>…</script>`（原 605–2815）替换为：
   ```python
   _REVIEW_CONSOLE_HTML = """\
   <!doctype html>
   ...（CSS + <body> 标记，原 384–604，保持不变）...
   """ + I18N_SCRIPT_TAG + """
   ...（body 标记，原 561–604，保持不变）...
   <script>
   """ + read_console_js() + """
   </script>
   </body>
   </html>
   """
   ```
3. **拼接顺序即依赖顺序**：`console-state → api-client → conversation-list → timeline → message-renderers → media-viewer → refresh → console-entry`（最后）。因全部函数声明会被 hoisting，顺序主要保证：(a) `var` 全局状态在 bootstrap 前存在；(b) `console-entry` 的 bootstrap 顶层语句最后执行；(c) `I18N` 已由 `I18N_SCRIPT_TAG` 在脚本前注入，全局可用。
4. `admin_conversations` 路由（3550）**不改**，仍返回 `_REVIEW_CONSOLE_HTML`。

---

## 4. 必须保留的行为（逐字不动其逻辑）

- **stale response guard**：`refreshEntityList`(2306)/`refreshConversationList`(2329) 的 `mode/selEntityId` 捕获 + `entityListSignature/convListSignature` 比对；`timelineRequestGen` 代际令牌（`fetchTimelinePage`891 / `fetchOlderMessages`957 / `refreshTimelineIfSelected`2440 / `loadOlderAutomatically`973,979）。
- **滚动保持**：`preserveScrollPosition`(917) + `loadOlderAutomatically`(963) 在 fetch 前记录 `beforeHeight`、render 后恢复；`applyRefreshScroll`(2374)/`applyTimelineRefresh`(2389) 维持 `prevScrollTop` 与底部吸附。
- **媒体播放状态保持**：`refreshTimelineIfSelected`(2423) 注释明确——自增刷新若渲染结果字节级不变，**不得重建时间线 DOM**，否则会销毁活动 `<video>`/`<audio>` 播放（currentTime 归零、暂停、浏览器重新请求媒体字节）。`applyTimelineRefresh`(2389) 仅重建变更行、未变行（`data-msgsig` 相同）保持原 DOM 与加载态。
- **unchanged refresh 不替换媒体节点**：`lastRenderedTimelineSignature`(636) + `timelineSignature`(2175) 比对；`refreshTimelineIfSelected` 中 `if(unchanged)return;`(2450-2454) 路径原样保留。
- `REFRESH_INTERVAL_SEC=30`、`setInterval(tickRefreshCountdown,1000)`、`visibilitychange` 监听（`refreshNow('visibility')`）原样保留。

---

## 5. 测试与验证（不达标不收工）

### 5.1 让既有测试零改动通过（硬性）
- 拆完后 `_REVIEW_CONSOLE_HTML` 仍 import 得到且含完整 JS；§1.1 的 5 条前提逐条满足；连续块 1428→2024 在 `message-renderers.js` 内仍连续。
- 跑（开发服务器假设已运行，命令前台跑）：
  ```
  cd backend && python -m pytest tests/test_admin_auto_refresh.py tests/test_admin_auto_load_older.py tests/test_admin_timestamp_formatting.py tests/test_rnd_207_thumbnail_frontend.py tests/test_rnd_198_frontend.py tests/test_admin_chat_bubble_style.py tests/test_admin_group_participant_overflow.py -q
  ```
  必须全绿。

### 5.2 各模块可独立 syntax/test（满足验收"可独立"）
- 扩展 `Makefile` 的 `build` 目标：对每个 `backend/app/assets/console/*.js` 执行 `node --check`（复用既有 `node --check backend/app/assets/i18n.js` 的模式）。
- 可选增强：新增轻量 Node smoke（不破坏既有测试），对单个模块文件 + 其显式依赖文件做 `node --check`/抽取关键函数断言存在。非必须，但建议作为"独立可测"证据。

### 5.3 全量 + 真实浏览器 smoke
- `make verify`（lint-diff + typecheck + build + 全量 pytest）通过，无 lint/type/未用导入告警。
- 真实浏览器 smoke（手动或既有 Playwright，如项目有）：打开 `/admin/conversations`，验证：刷新倒计时与手动刷新、自动加载更早消息滚动保持、媒体（图片/视频/语音）viewer 开关与播放状态在自增刷新后不中断、focus/jump-to-message（RND-229）仍工作。

---

## 6. 执行步骤（严格按顺序）

1. **基线锁行为**：先跑 §5.1 的 7 个测试确认绿；通读 `test_admin_auto_refresh.py`/`test_admin_auto_load_older.py`/`test_admin_chat_bubble_style.py` 的抽取逻辑，作为"不得破坏"护栏。
2. **抽取**：从 `_REVIEW_CONSOLE_HTML` 的内联 `<script>`（605–2815）按 §2 切片，逐字写入 8 个文件。**先搬 `media-viewer.js` 的 viewer 代码出连续块**（§2.6），确保 `message-renderers.js` 的 1428→2024 跨度仍连续合法。
3. **接线**：按 §3 在 `main.py` 引入 `read_console_js()` 并替换内联脚本；保留 `<style>` 与 `I18N_SCRIPT_TAG`。
4. **Makefile**：扩展 `build` 对 8 个 `.js` 做 `node --check`。
5. **验证**：§5.1 七个测试绿；§5.2 每个文件 `node --check` 通过；§5.3 `make verify` 绿 + 浏览器 smoke 通过。

---

## 7. 硬性约束（不可违反）

- **零行为变更**：API/DOM/视觉/产品功能不变；stale guard、滚动保持、媒体播放保持、unchanged-refresh 跳过逻辑逐字保留。
- **经典全局脚本**：严禁 `import`/`export`/`type="module"`/IIFE 命名空间；所有文件拼接为单一 `<script>` 全局作用域。
- **连续块不变**：`MediaAccessCache…renderCompositeMessage`（1428→2024）在 `message-renderers.js` 内保持连续；`media-viewer` 代码搬迁后不得在该跨度内留下缺口破坏连续性。
- **`<style>` 与常量可 import**：CSS 与 `_REVIEW_CONSOLE_HTML` 结构保持，`I18N`/`esc`/`fmtTime`/`pad`/`handleUnauth` 全局可用。
- **bootstrap 最后**：`console-entry.js` 末尾的 4 行 bootstrap 顶层语句必须最后执行。
- **只动块 1**：`_SEARCH_PAGE_HTML` / `_DIAGNOSTICS_HTML` 及其 JS 完全不动。
- 不引入新依赖、不新增后台进程；参考 `DEV_AGENT_RULES.md`；**不要自行 `git commit`/`push`**（需 Haisu 显式授权）；直接在 `main` 开发。

---

## 8. 收尾动作

- Linear：RND-217 置/保持 In Progress；完工写评论：8 个模块文件清单 + 拼接顺序 + 连续块与 bootstrap 处理说明 + 测试结果 + `make verify` 结论 + 浏览器 smoke 结论。
- QA Summary（按 `DEV_AGENT_RULES.md` §QA）：Files changed / Acceptance criteria / Commands run / Manual verification / Risks / 无 secrets / 仅预期文件变更。
- 保留合并关卡给 Haisu 人工处理，不自动合入 main。

## 9. 验收清单（Acceptance Criteria）

- [ ] 8 个模块文件就位（`console-state`/`api-client`/`conversation-list`/`timeline`/`message-renderers`/`media-viewer`/`refresh`/`console-entry`），逻辑与原内联脚本逐字等价。
- [ ] `_REVIEW_CONSOLE_HTML` 仍 import 得到且含完整 JS；`<style>` 与 `I18N_SCRIPT_TAG` 保留。
- [ ] 连续块 `MediaAccessCache…renderCompositeMessage` 在 `message-renderers.js` 内连续合法；`media-viewer` 代码已迁出该跨度。
- [ ] §5.1 七个测试全绿（零改动）；§5.2 每个 `.js` `node --check` 通过。
- [ ] `make verify` 通过；浏览器 smoke 通过（刷新/滚动/媒体播放/focus 均兼容）。
- [ ] stale response guard、滚动保持、媒体播放状态保持、unchanged-refresh 跳过逻辑全部保留。
- [ ] 未引入新框架/新依赖/视觉或功能变更；未触碰块 2/块 3。
