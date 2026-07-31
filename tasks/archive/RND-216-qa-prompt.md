# RND-216 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的验收角色），对**已实现的** RND-216 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-216-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-216** 的验收标准 + 开发提示词 §2/§5 的目标架构与约束。

---

## 0. 验收依据

- **Linear RND-216 验收标准（必须全过）**：
  1. 现有 URL、HTTP status、DOM、API 请求与 i18n 初始化顺序**不变**；
  2. 静态资源**不存在 404**；
  3. refresh / rich-media / frontend 测试**全部通过**；
  4. 完成**真实浏览器 smoke**；
  5. 可通过「恢复 inline strings」**独立回滚**。
- **硬约束（来自开发提示词 §4，验收也要守）**：不模块化 JS、不重写 DOM、不引入框架/构建工具、不新增 jinja2 依赖、不碰 DB migration / CI / 企业名变更。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测分支（建议 `feature/rnd-216`），且开发 agent 已通过 `make verify`（lint-diff + typecheck + build + 全量 pytest）。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
2. 本地开发服务器可访问（默认 `http://localhost:8000`）；若不在运行，用项目既有方式**前台**启动后再验（不后台化、不加 `&`）。
3. 浏览器可用（真实 smoke 用 Playwright 或手动，见 C10）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。

### A. 行为等价（URL / status / DOM / API / i18n 顺序不可变）
- **C1 URL 与 status 不变**：对以下端点 `curl -i`（带有效 session cookie）确认 status 与改造前一致：
  - `GET /admin/messages` → 200；`GET /admin/messages/{有效msgid}` → 200；`GET /admin/messages/{无效msgid}` → **404**（body 含「404 – Message not found」）；
  - `GET /admin/conversations` → 200；`GET /admin/search` → 200；`GET /admin/diagnostics/reachability` → 200；
  - 未登录时上述受保护端点 → **302** 跳 `/admin/login`。
  - 期望：status 集合与改造前逐一相等（对照开发前 `main.py` 路由现状）。
- **C2 DOM 结构不变**：served HTML 关键元素 ID / 结构与原模板文件逐字一致。抽查：`/admin/conversations` 须含三栏容器（如 `timeline-top-sentinel`、`timeline-history-status`、`load-older` 相关节点——注意 RND-204 已移除 `load-older-btn`）、`/admin/search` 含结果容器与四类 filter 触发器、`/admin/diagnostics/reachability` 含诊断挂载点。
- **C3 API 请求不变**：`app/web/static/review-console.js` / `search.js` / `diagnostics.js` 内 `fetch` / `XMLHttpRequest` 的**端点路径与查询参数**与改造前一致（仅 JSON literal 那一行被改为读全局，其余 `fetch(...)` 调用原样）。用 `grep -oE "fetch\([^)]*\)"` 抽取后与原 `main.py` 内嵌 JS 比对，确认无新增/删除/改写端点。
- **C4 i18n 初始化顺序不变**（关键）：served HTML 中 `I18N_SCRIPT_TAG` 注入的 `<script>`（定义 `I18N` 全局，含 `I18N.availableLocales` / `I18N.t`）必须**出现在**页面自有 `<script src="/web/static/<page>.js">` **之前**。判定：`curl /admin/conversations` 的响应里，含 `var I18N` / `I18N.t=` 的 `<script>` 标签的起始位置字节 < 外部 JS `<script src>` 标签起始位置。顺序颠倒会致页面 JS 调 `I18N.t` 时 `I18N` 未定义 → 直接 FAIL。

### B. 静态资源与缓存（无 404 + cache-busting 生效）
- **C5 静态资源无 404**：从 served HTML 抽取所有 `/web/static/*.css` 与 `/web/static/*.js` 引用，`curl -o /dev/null -w "%{http_code}"` 每一个 → 必须全 **200**。覆盖：`base.css`、`diagnostics.css`、`review-console.js`、`search.js`、`diagnostics.js`。
- **C6 长缓存 + cache-busting 版本串**：
  - 每个静态响应 `curl -i` 须带 **`Cache-Control: public, max-age=31536000, immutable`**（或等价长缓存），**绝不可含 `no-store`**。
  - HTML 引用须带 `?v=<version>`（如 `/web/static/review-console.js?v=abcd1234`）。
  - ⚠️ 若静态响应出现 `Cache-Control: no-store` → 说明 `MediaAccessNoStoreMiddleware`（RND-187）未对 `/web/static` 豁免，判 **FAIL**（这是开发提示词 §2.4 的硬性坑）。
- **C7 版本串随内容变化**：改动任一 static 文件内容后重启进程，`?v=` 哈希应变化（确保缓存真能失效）。验收可只读 `app/web/__init__.py` 的 `STATIC_VERSION` 生成逻辑确认其基于文件内容哈希，或构造一次小改动实测。

### C. 测试套件（refresh / rich-media / frontend 全绿）
- **C8 命名套件通过**（工单点名）：
  ```
  cd backend && python -m pytest \
    tests/test_admin_auto_refresh.py \
    tests/test_rnd_206_rich_media.py \
    tests/test_admin_auto_load_older.py \
    tests/test_rnd_206_qa_fixes.py \
    tests/test_rnd_210_msgtype_and_card.py \
    tests/test_revoke_frontend_render.py \
    tests/test_rnd_198_frontend.py \
    tests/test_i18n_foundation.py \
    tests/test_reachability_diagnostics_render.py \
    tests/test_reachability_diagnostics_page.py \
    tests/test_search_page_js_syntax.py \
    tests/test_rnd_230_search_page_participants_js.py \
    tests/test_rnd_229_focus_locate.py \
    -q
  ```
  - 重点核对：上述测试中**原先 `import` 私有 HTML 常量**的 21 个文件，现已改为读 `app/web/templates/*.html` 与 `app/web/static/*.js`；Node 执行类测试（auto_load_older / rnd_206_qa_fixes / rnd_210 / revoke_frontend / rnd_198）须直接读 `app/web/static/<page>.js` 跑 Node，不再从 HTML 抽 `<script>`。任一导入失败（如仍 `from app.main import _REVIEW_CONSOLE_HTML`）→ FAIL。
- **C9 全量 `make test` 通过**：`cd backend && python -m pytest tests -q` 全绿（或仅既有 skip）。这是「frontend 测试全部通过」的兜底。

### D. 工程一致性（Makefile / 依赖 / 路由契约）
- **C10 Makefile 假设已同步**：
  - `Makefile` `build` 目标注释不再声称「i18n.js 是唯一的独立 JS asset / everything else is inlined into main.py」；
  - `build` 目标对 `backend/app/web/static/*.js` 增加了 `node --check`（与 i18n.js 并列）。
  - 实测：`make build` 应输出新 static JS 的 syntax OK，且整体 `build: OK`。
- **C11 无新增依赖**：`backend/requirements.txt` **不含 jinja2**（开发提示词决策：用极简 `render_template` 占位替换，不引入模板引擎）。`git diff backend/requirements.txt` 应无新增行（或仅预期内的既有依赖）。
- **C12 路由契约不受影响**（回归风险点）：
  - ⚠️ `app.mount("/web/static", ...)` 会向 `app.routes` 增加一项 `Mount`。`test_http_contract.py` 断言 `route_count == 33`（来自 RND-218 基线）。若挂载使该计数变 34 → 该测试 FAIL。
  - 判定：先跑 `test_http_contract.py`。若 FAIL 且根因是 static 挂载抬高计数 → 判 **FAIL（工程一致性）**，记录为「需 dev agent 处理：使契约测试排除 Mount，或显式接受 static 挂载不计入 33」。验收 agent 不自行改契约测试。
  - 其余 http_contract 快照（`test_route_snapshot_with_real_model_names` 逐字段、`/api/*` path 集合）须不变。

### E. 真实浏览器 smoke
- **C13 端到端可用**（Playwright 或手动，登录后）：
  1. `/admin/conversations` 加载三栏、可滚动、自动刷新/加载更旧正常、rich-media（图片/语音/视频/文件）渲染正常；
  2. 从 `/admin/search` 搜结果 → 点卡片跳回对话并 `focusMessage` 高亮（RND-229 不受影响）；
  3. `/admin/diagnostics/reachability` 正常渲染并拉取 `/api/admin/reachability-audit`；
  4. 登录态失效时各受保护页正确 302 到 `/admin/login`。
  - 任何一页 JS 报错（console error）→ FAIL。

### F. 可回滚性
- **C14 独立回滚**：确认本任务改动局部、聚焦——`git revert` 该 PR（或恢复 `main.py` 内联字符串 + 删 `app/web/`）即可回到外置前，`backend/requirements.txt` 无残留新依赖，无其他任务代码被耦合进同一 diff。抽查 `git diff --stat` 范围应仅限 `app/web/`、`app/main.py`（删常量+挂静态）、`Makefile`、受影响的 `tests/*.py`。

---

## 3. 测试方法

- **静态 / 缓存**：用 `curl -i` 打页面与静态资源，逐条核对 C1/C4/C5/C6。
- **行为等价**：C2/C3 用 `curl` 取 served HTML + `grep` 抽 `fetch` 比对；C3 也可直接 diff `app/web/static/*.js` 与原 `main.py` 内嵌 JS（除 JSON literal 行外应逐字一致）。
- **测试**：先 `make verify` 收口，再单独跑 C8 命名套件；`test_http_contract.py` 单独跑（C12）。
- **smoke**：项目既有 Playwright 脚本，或本 agent 编写针对性步骤覆盖 C13。
- 任一工单点名测试若开发 agent 未交付对应断言，本 agent **自行补最小验收测试**覆盖该点后再判定（仅测试，不改实现）。

---

## 4. 硬性约束（验收 agent 自身也要守）
- 不修改任何实现代码；只**读**与**断言**。若发现需要改代码才能验证，说明是「待测代码缺口」而非自己补。
- 不绕过租户隔离做测试（用合法多租户 fixture 验证隔离）。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；开发服务器假设已在运行，命令前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-216 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · make verify：通过/失败 · 开发提示词：rnd-216-execution-prompt.md

| 编号 | 验收点 | 结果 | 证据（实测/命令/截图路径） |
|------|--------|------|---------------------------|
| C1   | URL/status 不变 | PASS | curl: /admin/messages=200 ... |
| C2   | DOM 不变 | PASS | ... |
| C3   | API 请求不变 | PASS | grep fetch 比对一致 |
| C4   | i18n 顺序 | PASS | I18N <script> 位于 page.js 之前 |
| C5   | 静态无 404 | PASS | 5/5 = 200 |
| C6   | 长缓存+?v= | PASS | Cache-Control 无 no-store |
| C7   | 版本串随内容变 | PASS | ... |
| C8   | 命名测试套件 | PASS | 13 文件全绿 |
| C9   | 全量 make test | PASS | ... |
| C10  | Makefile 同步 | PASS | build 注释+node --check |
| C11  | 无新依赖 | PASS | requirements.txt 无 jinja2 |
| C12  | 路由契约 | PASS | route_count==33 |
| C13  | 浏览器 smoke | PASS | 4 项均正常 |
| C14  | 可回滚 | PASS | git diff 范围局部 |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- <如 i18n 顺序、缓存策略等的观测结论>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺前置）。
```

- 若某条因环境无法复现（如浏览器 smoke 跑不了），如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给可复现证据。
- 最终把该报告作为 Linear RND-216 评论贴出（状态保持 In Progress，交还用户 Haisu 决策合并）。
