[Goal check] This work advances 开发（Development） by 将技术味浓的消息可达性诊断重构为 1–5 坐席小微企业可理解、可操作且不制造虚假安全感的“归档健康”界面。

# RND-338 开发提示词（Developer Prompt）

> 开始前必须读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、RND-337 dev prompt 与最终 QA verdict。RND-338 blocked by RND-337；依赖未 PASS 时不得猜 API 或回退旧接口实现新结论。

## ⚡ 立即执行，不要询问意图

你现在收到的是已经批准、待立即执行的任务指令。你就是 RND-338 开发 agent。先执行 Preflight；任一闸不满足则输出 `BLOCKED_NEEDS_HUMAN`，不改产品代码。全部满足后直接实现。

---

## Preflight（开工第一步，先做完再碰代码）

### P-1 现场采集工作树基线 —— 不要相信任何文档里的快照

本提示词**不记录**工作树状态：提示词是静态的，工作树是易变的。你自己采集：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
```

归因规则：

1. 不在下方「本工单拥有的文件」清单里的一切改动 → 标为「非本票」，写进 QA Summary
   的 notes，**不修改、不回滚、不覆盖、不算作本票交付**。
2. 采集**之后**新增的 commit / push / branch 才归因于你（你不该产生任何一个）。
3. RND-335 / RND-339 可能并发进行（见 `tasks/WAVE-ownership.md` §2）。看到
   `backend/app/` 后端文件、`deploy/systemd/`、`docs/` 有 diff 是预期的，不是你的越界。

### P-2 依赖闸：RND-337 必须 QA PASS

```bash
cat tasks/RND-337-qa-verdict.json
```

- 文件不存在，或 `verdict != "PASS"` → `BLOCKED_NEEDS_HUMAN` + 缺失契约，**零产品代码改动**。
- PASS → 读**实际落地的** `backend/app/schemas/reachability_checks.py` 与
  `backend/app/routers/reachability_checks.py`，以真实 schema 为准，
  不以本提示词对六态/字段的描述为准。字段名对不上时按下方「若 RND-337 最终 response
  与本 prompt 有差异」的规则处理。

### ⛔ P-3 串行闸：RND-336 必须先 Done

`tasks/WAVE-ownership.md` §3 记录了一个跨波次冲突：RND-336 与 RND-338 都需要写
`backend/app/assets/i18n.js` 与 `backend/tests/test_sidenav.py`，且 RND-336 会
结构性修改 `backend/app/web/sidenav.py`。裁决是**串行化，RND-336 在前**。

```bash
cat tasks/RND-336-qa-verdict.json
```

- 文件不存在，或 `verdict != "PASS"` → 输出 `BLOCKED_NEEDS_HUMAN`：
  「RND-338 被 WAVE-ownership §3 串行化裁决阻塞，需 RND-336 先 Done，
  或需 Haisu 在 `tasks/WAVE-ownership.md` 改判」。**零产品代码改动**，
  尤其不要碰 `i18n.js` 和 `test_sidenav.py`。
- PASS → 继续。注意此时 `NAV` 里**不应该**再有 `audit-log` 项——那是 RND-336 的
  交付物，**不是**你的归因 diff，也不是需要你修复的回归。你只改 `nav.diagnostics`
  的文案 key，不动 `NAV` 结构。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-338「将消息可达性诊断重构为面向小微客户的归档健康 UI」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-338/将消息可达性诊断重构为面向小微客户的归档健康-ui
- 优先级：High｜风险等级：**R1**
- 所属波次：消息可见性闭环 · 人类用户体验
- 当前关系：blocked by RND-337；RND-337 PASS 后可与 RND-339 并行。

## [Goal check]
本工作推进「开发实现」阶段，证据 = 9 条 AC 覆盖信息架构、六态结论、手动检查、渐进披露、异常恢复、三语言、安全 DOM 和响应式可访问性。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）

目标客户是 1–5 个坐席的小微企业。老板/管理员最想知道“最近检查是否完整、有没有要处理的问题、下一步做什么”，而不是 `matching_total`、内部 reason code 或一屏技术日志。

- 现有页面路径由 `backend/app/routers/web.py:121-141` 注册并进行 session gate；`/admin/diagnostics/reachability` 的路径/bookmark/权限必须保留。
- `backend/app/web/templates/diagnostics.html:19-28` 与 `backend/app/web/static/diagnostics.js:93-145` 当前直接调用旧 `/api/admin/reachability-audit`，在浏览器计算 reach rate，并展示偏技术的分页审计；专属样式在 `diagnostics.css`。
- `backend/app/web/sidenav.py:38` 已有 diagnostics 导航；用户文案来自 `backend/app/assets/i18n.js:276,732,1188` 的 `nav.diagnostics`，现有三语言 diagnostics keys 位于 `:433-473,889-929,1345-1385`。无需改 nav 结构或 route。
- RND-337 应提供 `POST /api/admin/reachability-checks` 与 `GET /api/admin/reachability-checks/latest`，六态为 `healthy`、`attention`、`checking`、`no_data`、`incomplete`、`error`，并提供 complete/count/reason/scope/timestamp/version/safe error。
- 旧 `GET /api/admin/reachability-audit` 保留给技术排障；新页面的主结论不得再通过客户端单页扫描推断。
- 当前没有 dashboard UI；本票只重构现有诊断页，不新建首页卡片。

**共享工作树基线：**见 Preflight P-1。基线由你现场采集，本节不做任何快照断言。
`tasks/` 下的提示词文件是 PM 产物，任何时候都不归因于开发实现。

**本项目已知的高频踩坑点：**
- ❗ **没有模板引擎。** `render_template()` 只做 `__TOKEN__` 替换；禁止 Jinja `{%` / `{{`。页面动态内容用原生 JS。
- ❗ **i18n 有 3 个 locale**：`zh-CN` / `zh-TW` / `en`，新增/改名的每个用户文案都要三份。
- ❗ **架构边界是硬闸**：不在 main 加 route/HTML/SQL；本票无需新业务 route。
- ❗ **架构冻结 D1**：SSR + 原生 JS；不引入 React/Vue/bundler/SPA router/第三方 chart。

## 目标（Goal）

把现有“消息可达性诊断”转成小微管理员日常可读的“归档健康”：首屏给清晰结论、影响和下一动作，技术聚合默认折叠；支持用户主动发起检查并可靠呈现 RND-337 的完整性状态。

## 范围边界

**In scope（交付物）：**

1. **命名与信息架构。** 保留 `/admin/diagnostics/reachability`，将 nav、页面 title、breadcrumb/description 改为三语言“归档健康 / 歸檔健康 / Archive health”。不要保留“可达率”作为主标题或健康度百分数。
2. **人类首屏。** 首屏只呈现：六态图标/标题、1–2 句解释、最近完成检查时间、检查范围/检查消息数、主要 CTA。`healthy` 明确“在本次完整检查范围内未发现可见性问题”，不得承诺绝对完整；`attention` 明确有多少条需要关注；其它四态给下一步。
3. **六态消费。** 完全使用 RND-337 state + complete，不在 JS 重算状态。`incomplete` 区分“尚无完整检查/本次中断”；`no_data` 解释指定范围没有可检查的已解密消息；`error` 只翻译 allowlisted safe error code，未知 code 使用通用错误，不显示 raw error。
4. **手动检查。** “立即检查”调用 POST；防重复点击、显示 checking、轮询 latest 到 terminal，设置有界超时和退避；409/active run 复用、401/403、网络失败、服务端 error 都有可恢复状态。轮询结束/页面卸载时清理 timer，不无限请求。
5. **渐进披露。** 默认折叠 `<details>` 展示计数、reason 汇总、scope、algorithm version 等技术信息；reason code 映射成人类语言并按严重度/数量排序。禁止展示 message samples、raw sender/recipient/room/message id、payload、path、traceback。
6. **状态与可访问性。** 初始 loading、healthy、attention、checking、no_data、incomplete、error、fetch failure、auth failure、manual trigger failure 全部有确定 DOM；状态容器 `aria-live`，按钮、details、retry 可键盘操作；颜色之外还有文字/图标。
7. **安全渲染。** 动态 API 值只通过 `textContent`/createElement/安全属性进入 DOM；不得把 API data 拼到 `innerHTML`。数值/时间/scope 做类型和边界校验，异常数据降级而不是崩页。
8. **响应式和视觉。** 复用现有 design tokens；桌面与 320px 窄屏不横向溢出，CTA/状态层级清楚；不增加 chart/table-first 体验。
9. **测试。** 扩展现有 page/render/sidenav tests，必要时新增聚焦 JS contract test，覆盖六态、POST/poll、timer cleanup、安全 DOM、三 locale、旧 URL/auth 和禁止旧 API 主消费。

**Out of scope（显式非目标）：**
- 不新增/改名 route，不新建 dashboard/home 卡片，不改 Settings 信息架构。
- 不改 RND-337 backend/API/schema/migration，不调用旧 audit API 计算主结论。
- 不展示逐条消息、samples 或下载/导出。
- 不在 DOM / `console` / `localStorage` / `sessionStorage` 中出现
  `docs/agent-data-minimization.md` §2 的任何字段族（前端面的检查方式见该文件 §5 末段）。
- 不做自动修复、自动补拉、告警、通知、客服工单、SLA 或合规保证。
- 不实现 RND-339 的自动触发/findings/agent API。
- 不引入 chart library、React/Vue、bundler 或 shared style 大重构。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/diagnostics.html`
- `backend/app/web/static/diagnostics.js`
- `backend/app/web/static/diagnostics.css`
- `backend/app/assets/i18n.js`（⚠️ 与 RND-336 共享，见 Preflight P-3；**仅** nav.diagnostics 与 diagnostics/archive-health 相关 keys，三 locale。不得触碰 audit/security-activity 区，那是 RND-336 的）
- `backend/app/routers/web.py`（仅更新该页面 docstring/描述，不改 route/auth）
- `backend/tests/test_reachability_diagnostics_page.py`
- `backend/tests/test_reachability_diagnostics_render.py`
- `backend/tests/test_sidenav.py`（⚠️ 与 RND-336 共享，见 Preflight P-3；**仅**新增 diagnostics 文案/key 相关断言，不得修改或删除 RND-336 关于 audit-log 已移除的断言）
- `backend/tests/test_rnd338_archive_health_ui.py`（可新建；不用则不建空文件）

**本工单只读、绝不可写的文件：**
- `backend/app/reachability_audit.py`、`backend/app/routers/reachability_audit.py` — 旧技术接口
- `backend/app/services/reachability_check_service.py`、`backend/app/schemas/reachability_checks.py`、`backend/app/routers/reachability_checks.py`、`backend/app/db/models.py`、`backend/alembic/versions/0032_reachability_audit_runs.py`、`backend/tests/test_reachability_checks.py` — RND-337 最终契约（migration 文件名以实际落地为准）
- `backend/app/main.py`、`backend/tests/test_http_contract.py` — 无新 route；**所有者 RND-337 → RND-339**
- `backend/tests/test_rnd280_rbac_scaffold.py` — 本票不新增 router，不触发闭世界白名单
- `backend/app/web/sidenav.py` — **所有者 RND-336**。结构不变，label 由 i18n 提供。RND-336 已从 `NAV` 移除 `audit-log`，那是它的交付物，不要「修回来」
- `backend/app/routers/admin_audit_page.py`、`backend/app/web/templates/audit_log.html`、`backend/app/web/templates/settings.html` — **所有者 RND-336**
- `backend/app/web/static/styles.css` — 共享样式；本页专属 CSS 已存在
- `backend/scripts/run_archive_worker_once.py`、`deploy/systemd/wecom-archive-reachability-check.service`、`deploy/systemd/wecom-archive-reachability-check.timer`、`backend/app/services/reachability_automation_service.py`、`backend/app/routers/reachability_findings.py` — RND-339

若 RND-337 最终 response 与本 prompt 有差异，以 PASS verdict 和实际 schema 为准；若差异影响六态/完整性/CTA，停止 `BLOCKED_NEEDS_HUMAN`，不得在前端猜或补后端。

## 验收标准（Acceptance Criteria）

- **AC-1a 依赖闸**：RND-337 verdict 为 PASS，实际 POST/latest 六态契约可读；不满足时零产品代码改动并 BLOCK。
- **AC-1b 串行闸**：RND-336 verdict 为 PASS（WAVE-ownership §3）；不满足时零产品代码改动并 BLOCK，尤其未触碰 `i18n.js` 与 `test_sidenav.py`。
- **AC-2 命名/URL**：一级 nav 与页面三语言名称为 Archive health 对应文案；旧 `/admin/diagnostics/reachability` auth/bookmark/200 保持；不新增 route，不再把“reachability rate”作为首屏概念。
- **AC-3 六态首屏**：每个 state + complete 组合都有明确标题、解释、时间/范围/数量和适用 CTA；partial/no run/error 绝不显示绿色健康；healthy 文案限定在最近一次完整检查范围。
- **AC-4 手动检查生命周期**：POST 防重复、active run 复用、checking poll、terminal 停止、timeout/backoff/unload cleanup、retry、401/403/network/error 均有自动化测试；不阻塞 UI、不无限轮询。
- **AC-5 渐进披露**：技术 details 默认折叠，reason/count/scope/version 映射完整；主层级无 raw code/id，DOM/HTML/console 不出现禁止字段或 sentinel 值。
- **AC-6 安全 DOM/异常数据**：恶意字符串和 invalid number/date/reason fixtures 不执行 HTML、不污染 attribute、不导致未捕获异常；API 值不可达 `innerHTML`。
- **AC-7 三语言**：新增/改名 keys 在 zh-CN/zh-TW/en 各存在，六态、CTA、errors、details/reasons、时间/范围标签均随 locale 更新，无单语言硬编码用户文案。
- **AC-8 可访问/响应式/只读**：aria-live、button/details/focus/disabled semantics 可测；颜色非唯一信号；320px 与桌面无横溢；无修复、导出、消息样本或通知控件。
- **AC-9 回归/范围**：page/render/sidenav/architecture/HTTP contract 与 `make verify` 全绿；旧技术 API 仍存在但页面主请求不调用；本票归因 diff 仅拥有文件。

## 验证方式（Verification — 确定性闸）
- 类型：**automated + human visual gate**
- 命令：
  ```bash
  make verify
  .venv/bin/python -m pytest backend/tests/test_reachability_diagnostics_page.py backend/tests/test_reachability_diagnostics_render.py backend/tests/test_sidenav.py -q
  test ! -f backend/tests/test_rnd338_archive_health_ui.py || .venv/bin/python -m pytest backend/tests/test_rnd338_archive_health_ui.py -q
  .venv/bin/python -m pytest backend/tests/test_reachability_checks.py backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
  grep -rn '{%\|{{' backend/app/web/templates/diagnostics.html
  git diff --check
  git status --short --branch
  ```
- 模板 grep 无输出（exit 1）是预期；出现 Jinja 即 FAIL。
- 通过 = AC-1 ~ AC-9、自动化命令和三语言/窄屏人工视觉 gate 均通过。

## 依赖（Dependencies）
- **硬 blocker 1：RND-337 必须最终 QA PASS。** 核对真实 schema、六态、POST 幂等/active 行为、safe error 目录。
- **硬 blocker 2：RND-336 必须 Done。** 来自 `tasks/WAVE-ownership.md` §3 的串行化裁决——两票共享 `i18n.js` 与 `test_sidenav.py`。这不是功能依赖，是文件所有权依赖，但同样是硬闸。
- RND-339 非本票前置；两票可并行，RND-338 不得触碰 backend automation/findings 文件。

## 完成定义（Definition of Done）
- [ ] RND-337 PASS 证据和实际 schema 已记录
- [ ] AC-1 ~ AC-9 有自动化证据，六态/异常/轮询测试完整
- [ ] 三 locale 齐全，桌面与 320px 人工视觉检查完成
- [ ] 主结论仅来自 latest state + complete，不调用旧 audit API 推断
- [ ] 动态 API 值走安全 DOM，无禁止字段/controls
- [ ] `make verify` 与回归全绿
- [ ] 归因 diff 只在拥有文件，输出 QA Summary + status
- [ ] 未 commit、未 push、未建分支

## 风险与回滚（Risk & rollback）
- 风险：healthy 文案过度承诺；始终带“最近一次完整检查/范围内”。
- 风险：poll 泄漏或重复 POST；单 active controller、disable CTA、有界退避和 cleanup。
- 风险：safe error/reason 未识别；默认通用翻译且 raw code 仅允许在技术 details 中以安全文本显示；raw error 永不显示。
- 风险：旧页面技术能力丢失；保留折叠聚合与旧技术 API 本身，不保留逐条样本 UI。
- 回滚：恢复本页 template/JS/CSS/i18n/docstring/tests；URL/backend/schema/data 不变。

## 人工点位（Human touchpoints）
- **Trigger**：RND-337 QA PASS 后 Haisu 将 RND-338 置 In Progress。
- **Gate**：Haisu 浏览器 review zh-CN/zh-TW/en、desktop/320px、六态视觉层级、健康限定文案和手动检查过程。
- **Escalation**：RND-337 状态/错误目录不稳、需要改 backend/shared styles/route、视觉文案需产品决策或两轮仍失败时，`BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引（步骤）
1. 跑 Preflight P-1 / P-2 / P-3 并记录结论；读实际 RND-337 schema 与现有 diagnostics template/JS/CSS/tests/i18n。
2. 先写六态、POST/poll/cleanup、安全 DOM、三 locale、旧 API 不再消费的 tests。
3. 重排 template 为首屏结论 + 折叠技术详情；JS 只消费新 API，CSS 复用 tokens 响应式实现。
4. 更新三个 locale 和 web route docstring；不改 nav 结构/main/backend。
5. 跑全闸，做三语言 desktop/320px/键盘视觉检查，输出 QA Summary + 归因 status；不要 commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不改 CI/CD、部署、`.gitignore` 或生产数据。
- 不改 RND-337/339 backend，不新增 route/dashboard，不改 shared styles。
- 不用旧分页 API 推断健康，不自行重算六态，不将 incomplete/no-run/error 显示为 healthy。
- 不用 innerHTML 注入 API 值；敏感字段以 `docs/agent-data-minimization.md` 为准；不做自动修复/通知。
- 不改 `i18n.js` 的 audit/security-activity 区，不动 `NAV` 结构，不修改 RND-336 写的 `test_sidenav.py` 断言。
- 不用 Jinja/React/Vue/bundler/chart library；SSR + 原生 JS。
