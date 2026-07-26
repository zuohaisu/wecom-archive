# RND-217 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-217 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-217-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-217** 的 5 项验收标准 + 开发提示词 §1.1 的测试契约与 §4 的必保行为。

---

## 0. 验收依据

- **Linear RND-217 验收标准（必须全过）**：
  1. API 与 DOM 行为兼容；
  2. unchanged refresh 不替换现有媒体节点；
  3. stale response guard、滚动保持、媒体播放状态保持；
  4. 各模块可独立 syntax/test；
  5. Node tests 与真实浏览器 smoke 通过。
- **非目标红线（不得被破坏）**：不引入新框架；不改视觉/CSS；不增加产品功能；不改任何 HTTP 契约或后端；只动 review console 第 1 个 `<script>` 块（搜索页/诊断页两块不动）。
- **结构目标**：`backend/app/assets/console/` 下 8 个经典全局脚本文件（`console-state`/`api-client`/`conversation-list`/`timeline`/`message-renderers`/`media-viewer`/`refresh`/`console-entry`），由 `main.py` 的 `read_console_js()` 按依赖顺序拼接回 `_REVIEW_CONSOLE_HTML`。

---

## 1. 前置检查（先确认环境，再验收）

1. 开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
2. 8 个模块文件确实落在 `backend/app/assets/console/`；`main.py` 已用 `read_console_js()` 替换内联 `<script>`（不再内嵌 605–2815 的大块 JS）。
3. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式启动**前台**进程后再验（不后台化、不加 `&`）。
4. **RND-216 状态确认**：RND-217 自洽（从 `_REVIEW_CONSOLE_HTML` 抽取），不要求 RND-216 已合入。但若 RND-216 已先外置，`_REVIEW_CONSOLE_HTML` 的组装方式会变——本 agent 须以**实测的组装产物**为准验证测试契约（见 C6），而非假设内联。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 结构正确性（源码形态）
- **C1 8 文件就位且命名正确**：`backend/app/assets/console/` 含 `console-state.js`/`api-client.js`/`conversation-list.js`/`timeline.js`/`message-renderers.js`/`media-viewer.js`/`refresh.js`/`console-entry.js`；无第 9 个随意文件。
- **C2 经典全局脚本（无模块语法）**：8 个文件均不含 `import`/`export`/`type="module"`/IIFE 命名空间封装；函数/全局变量在单一拼接脚本的全局作用域可见。抽查 `grep -nE "^\s*(import|export)\b|type=\"module\""` 应无命中。
- **C3 函数归属无逻辑改动**：逐文件比对原函数体与开发前 `main.py` 内联版本，确认仅「搬运 + 拆分」，无行为改写（可用 `git diff` 或对照开发前的 `main.py`）。重点核对：stale guard、滚动保持、媒体播放保持、unchanged-refresh 跳过逻辑**逐字保留**。
- **C4 media-viewer 搬迁正确**：`openViewer`/`closeViewer`/`viewerShow`/`ensureViewerRoot`/`registerViewerItem`/`restoreViewerFocus`/`openChatrecordViewer` 及 viewer 私有状态（`viewerItems`/`viewerIndex`/`viewerGen`/`viewerFocusTrigger`/`timelineViewerItems`）现定义在 `media-viewer.js`；且 `message-renderers.js` 中 `var MediaAccessCache=(function(){ … })(); … function renderCompositeMessage(m){…}` 跨度**连续、无缺口、合法**（见 C7）。
- **C5 bootstrap 最后执行**：4 行 bootstrap 顶层语句（`applyStaticI18n(); loadCurrentUser(); startAutoRefresh(); readFocusFromUrl();`）位于拼接后脚本的末尾（在 `console-entry.js` 末尾），`#search-input` 监听器亦随 `console-entry.js` 末尾；无其它顶层副作用语句跑到它们之后。

### 测试契约（零改动全绿 —— 本任务最核心风险点）
- **C6 `_REVIEW_CONSOLE_HTML` 仍可 import 且含完整 JS**：开发服务器启动后 `python -c "from app.main import _REVIEW_CONSOLE_HTML; assert 'function refreshTimelineIfSelected' in _REVIEW_CONSOLE_HTML and '<style>' in _REVIEW_CONSOLE_HTML"` 通过；`<style>` 块（CSS 自定义属性）与 `I18N_SCRIPT_TAG` 注入均保留。
- **C7 既有 7 测试全绿（零改动）**：
  ```
  cd backend && python -m pytest tests/test_admin_auto_refresh.py tests/test_admin_auto_load_older.py tests/test_admin_timestamp_formatting.py tests/test_rnd_207_thumbnail_frontend.py tests/test_rnd_198_frontend.py tests/test_admin_chat_bubble_style.py tests/test_admin_group_participant_overflow.py -q
  ```
  必须全绿。任一失败即判 FAIL（常见根因：连续块被拆断 / 函数签名被改 / `<style>` 丢失 / 某模块未参与拼接）。
- **C8 连续块抽取仍可工作**：C7 中的 `test_rnd_198_frontend`/`test_rnd_206_rich_media`/`test_admin_auto_load_older`/`test_admin_group_participant_overflow` 均以 regex 抽取 `MediaAccessCache…renderCompositeMessage` 连续片段并在 Node 执行——它们通过即证明连续块未被破坏（与 C4 互为印证）。
- **C9 各模块可独立 syntax**：对 8 个文件逐个 `node --check backend/app/assets/console/<file>.js` 全部通过；且 `Makefile` 的 `build` 目标已覆盖这 8 个文件（确认 `Makefile` 由只查 `i18n.js` 扩展为查 `console/*.js`）。

### 行为兼容性（API / DOM / 刷新 / 媒体 / 滚动）
- **C10 对外契约零变更**：`GET /admin/conversations` 返回的 HTML 中，会话列表/时间线/媒体相关 DOM `id` 与事件（`onclick="setMode('staff')"`、`onclick="refreshNow('manual')"`、`onclick="scrollTimelineToBottom()"` 等）与改造前逐字一致；`REFRESH_INTERVAL_SEC=30`、`setInterval(tickRefreshCountdown,1000)`、`visibilitychange` 监听（`refreshNow('visibility')`）均在。
- **C11 stale response guard 有效**：在浏览器中快速切换「员工↔联系人」tab 或切换会话，正在飞行的旧响应不得覆盖新选择（mode/selEntityId 捕获 + `entityListSignature`/`convListSignature` 比对 + `timelineRequestGen` 代际令牌生效）。可借 `test_admin_auto_refresh.py` 的 `refreshTimelineIfSelected` node 抽取与 RND-204/RND-158 既有断言佐证。
- **C12 滚动保持**：加载更早消息时，插入历史后视口位置不跳到顶部（`preserveScrollPosition` + `loadOlderAutomatically` 行为保留）。对照 `test_admin_auto_load_older.py` 对 `preserveScrollPosition`/`isNearTop`/`startHistoryObserver` 的存在性与节点断言。
- **C13 媒体播放状态保持 + unchanged-refresh 不替换媒体节点**：让某会话含视频/语音，播放到一半触发自增刷新（或手动刷新）——**活动 `<video>`/`<audio>` 的 currentTime/播放态不得被重置**，时间线 DOM 不得整体重建；`lastRenderedTimelineSignature` + `if(unchanged)return` 路径生效（字节级不变的刷新直接跳过重建）。对照 `test_admin_auto_refresh.py` 对 `mergeMessagesByMsgid`/`isNearBottom` 的断言，以及 `refresh.js` 源码确认 `if(unchanged)return;` 仍在。

### 回归（不破坏既有能力）
- **C14 search 页 / 诊断页两块未动**：`test_rnd_230_search_page_participants_js.py` 全绿（其 import `_SEARCH_PAGE_HTML`，验证搜索页 JS 未受 RND-217 牵连）；`_DIAGNOSTICS_HTML` 相关路由/JS 无变更。
- **C15 全量回归绿**：`make verify` 全绿（含 RND-214 行为基线、RND-213 CI 门禁）；无新 lint/type/未用导入告警。
- **C16 路由数不变**：`test_http_contract.py` 的 `route_count == 33` 仍 PASS（RND-217 不新增/不删除路由）。

### 真实浏览器 smoke（人工 / Playwright）
- **C17 刷新与倒计时**：打开 `/admin/conversations`，刷新状态栏倒计时可见、手动「刷新」按钮生效、后台自增刷新准时触发且不抖动。
- **C18 自动加载更早消息**：滚动到时间线顶部触发 IntersectionObserver 加载历史，加载中/到底/重试 UI 正常，且滚动保持（C12）。
- **C19 媒体 viewer 与播放态**：图片/视频/语音 viewer 开关正常；视频/语音播放中发生自增刷新后播放不中断（C13）。
- **C20 focus / jump-to-message（RND-229）**：从搜索结果页跳回对话，`focusMessage` 滚动+高亮 + 「返回搜索结果」banner 仍工作。

> 若项目无 Playwright harness，C17–C20 改为**人工浏览器验证**并在报告中写明实测步骤与结果；确实无法复现的环境项标 **NOT REPRODUCIBLE** 而非 FAIL。

---

## 3. 测试方法

- **结构/语法**：`cd backend` 后
  - `for f in console-state api-client conversation-list timeline message-renderers media-viewer refresh console-entry; do node --check app/assets/console/$f.js; done`
  - `python -c "from app.main import _REVIEW_CONSOLE_HTML; ..."`（见 C6）
- **测试契约**：跑 C7 的 7 测试命令；跑 `make verify` 收口。
- **行为/回归**：C11–C16 以既有 pytest 断言 + 源码核对为主；C17–C20 以真实浏览器 smoke 为主（必要时本 agent 编写针对性 Playwright 步骤或最小脚本，但只**读/断言**，不改实现）。
- 若开发 agent 遗漏某模块测试或 `Makefile` 未覆盖 8 文件，本 agent 自行补最小 `node --check`/导入断言再判定，并在报告中标注「自补项」。

---

## 4. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过租户隔离做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-217 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · 是否含 RND-216：是/否 · make verify：通过/失败

| 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|------|--------|------|---------------------------|
| C1   | 8 文件就位 | PASS | ... |
| C2   | 经典全局脚本 | PASS | grep 无 import/export 命中 |
| C3   | 函数归属无改写 | PASS | ... |
| C4   | media-viewer 搬迁 | PASS | ... |
| C5   | bootstrap 最后 | PASS | ... |
| C6   | 常量可 import + 含 JS | PASS | ... |
| C7   | 7 测试全绿 | PASS | pytest -q：7 files passed |
| C8   | 连续块抽取 | PASS | ... |
| C9   | 各模块 syntax | PASS | node --check ×8 + Makefile 覆盖 |
| C10  | 契约零变更 | PASS | ... |
| C11  | stale guard | PASS | ... |
| C12  | 滚动保持 | PASS | ... |
| C13  | 媒体播放保持 | PASS | ... |
| C14  | 搜索/诊断页未动 | PASS | ... |
| C15  | 全量回归绿 | PASS | make verify OK |
| C16  | 路由数不变 | PASS | route_count==33 |
| C17  | 刷新倒计时 smoke | PASS | ... |
| C18  | 自动加载更早 | PASS | ... |
| C19  | 媒体 viewer/播放 | PASS | ... |
| C20  | focus/jump | PASS | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- <观察到的限制或需用户决策项>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺前置或环境不可验）。
```

- 若某条无论如何无法复现（如环境问题导致 C17–C20 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-217 评论贴出（状态保持 In Progress，交还用户 Haisu 决策合并）。
