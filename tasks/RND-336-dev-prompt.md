[Goal check] This work advances 开发（Development） by 将审计日志从一级导航下沉为 Settings 内的 Security & activity，并用 RND-335 分类契约默认呈现人类安全信号、隐藏系统噪音。

# RND-336 开发提示词（Developer Prompt）

> 开始前必须读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、RND-335 dev prompt 与最终 QA verdict。本票 blocked by RND-335；依赖未 PASS 时不得猜接口或提前实现。

## ⚡ 立即执行，不要询问意图

你现在收到的是已经批准、待立即执行的任务指令。你就是 RND-336 开发 agent。先执行依赖检查；若 RND-335 尚未 PASS，输出 `BLOCKED_NEEDS_HUMAN` 和缺失契约，不改代码。依赖满足后直接实现，不要先输出计划等待确认。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-336「Frontend: Move Audit Log under Settings as Security & activity」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-336/frontend-move-audit-log-under-settings-as-security-and-activity
- 优先级：Medium｜风险等级：**R1**
- 所属波次：小微客户安全活动闭环 · 前端消费
- 当前关系：blocked by RND-335；Linear 当前 Todo。

## [Goal check]
本工作推进「开发实现」阶段，证据 = 9 条 AC 覆盖信息架构、人类可读 feed、服务端过滤/分页、安全详情、三语言和只读可访问性，且旧 `/admin/audit-logs` 保持可用。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）

目标客户只有 1–5 个坐席。安全历史有价值，但不是日常主流程，不应与会话审阅、搜索、媒体并列占据一级导航。

- `backend/app/web/sidenav.py:10-44` 的 review 分组包含 `audit-log`。
- `backend/app/routers/admin_audit_page.py:15-31` 已注册和鉴权 `/admin/audit-logs`，当前把 audit-log 标 active。
- `backend/app/web/templates/audit_log.html:17-55` 已有分页、loading/empty/error 和 API fetch，但主表显示 raw action/object/audit ID；`:27` 的 datalist 与生产动作不一致；detail 未展示。
- `backend/app/web/templates/settings.html:42-69` 的账号区域已有 card，可新增轻量入口，无需新增 route 或修改 settings JS tab 机制。
- `backend/app/assets/i18n.js` 三 locale 各有 `audit.*`/`nav.auditLog` 和 RND-328 锚点。
- RND-335 应交付 item.category、category/include_system filters、权威 action 目录和 worker 降噪。前端不得自造另一套 category。

**共享工作树基线：**提示词撰写时 `backend/app/web/static/styles.css`、`scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats` 有用户改动，`main` ahead 1。开始时重记 baseline；不得修改/revert `styles.css`。复用设计系统 class，必要的小样式放 `audit_log.html` 现有 `<style>`。

**本项目已知的高频踩坑点：**
- ❗ **没有模板引擎。** `render_template()` 只做 `__TOKEN__` 单遍替换，禁止 Jinja `{% include %}` / `{% for %}` / `{{ }}`；动态 feed 用原生 JS 和安全 DOM API。
- ❗ **i18n 有 3 个 locale**：`zh-CN` / `zh-TW` / `en`。每个新增 key 三块齐全；动作句也要翻译。
- ❗ **架构边界是硬闸**：router 不 import `app.main`；`app/main.py` 不加业务 route/HTML/SQL。
- ❗ **架构冻结 D1**：SSR + 原生 JS；不引入 React/Vue/bundler/SPA router。

## 目标（Goal）

把现有 Audit Log 重定位为 Settings 下的轻量“安全与活动”页：小微企业主默认看到高价值人类/安全事件，需要时才查看 system 活动，并在不暴露敏感信息的前提下读懂谁、何时、做了什么。

## 范围边界

**In scope（交付物）：**

1. **信息架构。** 从 `sidenav.py` 一级导航移除 audit-log；不删 route。`/admin/audit-logs` 页面将 Settings 标 active，breadcrumb 改为 Settings / Security & activity。在 `settings.html` 账号区域新增独立只读 card，含说明和 `/admin/audit-logs` 链接；不新增 settings tab/route。
2. **小微友好命名。** title/description/read-only 提示、Settings card、breadcrumb、三语言从 Audit Log/审计日志/稽核日志更新为 Security & activity/安全与活动/安全與活動。保留只读追加语义，不暗示防篡改或合规认证。
3. **人类可读 feed。** 为 RND-335 每个已知 action 提供 code → i18n message 映射。主层级显示本地化 actor/action/target/context/time；raw action code、audit ID、object type/id 只放可展开 `<details>`。未知 action 用本地化通用 fallback，raw code 仍只在技术详情，页面不崩。
4. **默认信号与 filters。** 默认 **90 天**（小微低频避免常态空白），提供 30/90/all；category 提供 all/security/account/configuration/data_access；operator 用现有 `/api/admin/users?per_page=100` 加载 1–5 坐席下拉，value 发送 `operator=<admin_user_id>`，失败时保留“全部操作人”且不拖垮 feed；Show system 默认关闭并发送 `include_system=false`，开启发送 true。filter/total/has_more/pagination 以服务端为准。
5. **安全详情。** 按 action 明确 allowlist 渲染 export format/count、安全 reason、changed_keys、old/new account/retention status、worker aggregate。禁止通用遍历展示任意 detail；禁止 innerHTML 拼接 API 值，统一 `textContent`/createElement。
6. **状态与可访问性。** loading、empty、network error、401/403、unknown action、operator fallback、pagination 都有三语言状态。filter 有 label；details/select/checkbox/button 可键盘操作；动态状态有 role/status/aria-live。页面只读，无 create/update/delete/export/alert 控件。
7. **兼容性。** 保留旧 URL 鉴权/bookmark；不改 RND-335 API、DB 或 routes；不记录页面浏览、搜索文本、缩略图/preview/signed URL 请求。
8. **测试。** 扩展 audit page、sidenav、settings tests；可新建 RND-336 聚焦文件，覆盖 action 映射、默认 query、detail allowlist、unknown fallback、无写控件和三 locale。

**Out of scope（显式非目标）：**
- 不删除/改名 `/admin/audit-logs`，不新增 redirect 或 route。
- 不改 audit API/action/category；发现缺口应 BLOCK，不在前端补后端逻辑。
- 不做 chart/dashboard/report/audit export/alert/notification/SIEM/tamper-evidence。
- 不做 Agent activity/delegation/run UI。
- 不记录普通浏览、搜索词、缩略图、signed URL、media preview。
- 不引入 React/Vue、第三方表格/日期库、bundler。
- 不重构 Settings，不改密码表单或动态配置表单。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/sidenav.py`（仅移除 audit-log 一级项）
- `backend/app/routers/admin_audit_page.py`（仅保留 route 并将 Settings 标 active）
- `backend/app/web/templates/audit_log.html`
- `backend/app/web/templates/settings.html`（仅账号区域新增 card/link）
- `backend/app/assets/i18n.js`（仅 RND-328 audit 区及新增 settings/security keys，三 locale）
- `backend/tests/test_audit_page.py`
- `backend/tests/test_sidenav.py`
- `backend/tests/test_rnd251_settings_page.py`
- `backend/tests/test_rnd336_security_activity_page.py`（可新建；不用则不建空文件）

**本工单只读、绝不可写的文件：**
- `backend/app/audit.py`、`backend/app/routers/audit.py`、`backend/tests/test_rnd335_security_activity.py`、`backend/tests/test_rnd295_audit_list.py` — RND-335 契约
- `backend/app/web/static/settings.js` — 账号 card 无需改 tab JS
- `backend/app/web/static/styles.css` — 共享且已有用户改动
- `backend/app/main.py`、`backend/tests/test_http_contract.py`、`backend/tests/test_rnd280_rbac_scaffold.py` — 无新 route
- `backend/app/db/models.py`、`backend/alembic/versions/` — 无数据变化
- `scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats` — 用户既有改动

若 RND-335 最终契约与本 prompt 不一致，以 PASS verdict 和实际代码为准；若影响 AC 或需后端改动，停止 `BLOCKED_NEEDS_HUMAN`，不得隐式扩票。

## 验收标准（Acceptance Criteria）

- **AC-1 依赖闸**：实现前 RND-335 verdict 为 PASS，实际 API 有 category 和 category/include_system，action 目录可读。依赖不满足时零产品代码改动并 BLOCK。
- **AC-2 信息架构/URL**：一级 sidenav 无 audit-log；Settings 有可发现 card/link；直接 `/admin/audit-logs` 仍 session-gated、登录后 200，Settings 侧栏 active，breadcrumb 正确。
- **AC-3 人类可读 feed**：RND-335 每个 action 都有三语言映射或显式 fallback；主层级不显示 raw code/audit ID；未知 action 安全通用文案，technical code/id 仅展开显示。
- **AC-4 默认/服务端 filters**：首请求默认 90 天和 `include_system=false`；30/90/all/category/operator/system toggle query 正确；改 filter 重置 offset；pagination/total/has_more 用服务端结果，无当前页伪过滤。
- **AC-5 安全详情**：只显示 allowlist context；detail 注入 message/token/secret/signed URL/storage/search/path/HTML 时不显示禁止值、不执行 HTML。
- **AC-6 状态/可访问/只读**：loading/empty/error/401/403/unknown/operator fallback/pagination 有测试；filter 有 label/aria，控件键盘可用；无审计写删导出控件。
- **AC-7 三语言**：新增/改名 key 在 zh-CN/zh-TW/en 各定义；title、breadcrumb、card、actions/fallback/details/filters/states 随 locale 更新，无单语言用户文案。
- **AC-8 后端/隐私边界**：RND-335、DB、route count、RBAC、settings.js、styles.css 无本票归因 diff；不发送/存储搜索文本或敏感 detail，不新增浏览审计。
- **AC-9 回归**：`make verify`、架构和 audit/settings/sidenav 测试全绿；settings password/config、audit auth、其它 nav item 无回归。

## 验证方式（Verification — 确定性闸）
- 类型：**automated + human visual gate**
- 命令：
  ```bash
  make verify
  .venv/bin/python -m pytest backend/tests/test_audit_page.py backend/tests/test_sidenav.py backend/tests/test_rnd251_settings_page.py -q
  test ! -f backend/tests/test_rnd336_security_activity_page.py || .venv/bin/python -m pytest backend/tests/test_rnd336_security_activity_page.py -q
  .venv/bin/python -m pytest backend/tests/test_rnd295_audit_list.py backend/tests/test_rnd335_security_activity.py -q
  .venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
  grep -rn '{%\|{{' backend/app/web/templates/audit_log.html backend/app/web/templates/settings.html
  git diff --check
  ```
- 模板 grep 应无输出；grep exit 1 是“未发现”的预期结果。
- 通过 = AC-1 ~ AC-9 满足、适用命令 exit 0，且完成三语言/窄屏人工视觉检查。

## 依赖（Dependencies）
- **硬 blocker：RND-335 必须 QA PASS。** 确认最终 action list、item.category、category/include_system 和安全 detail schema。
- 未满足时不改产品文件，输出 `BLOCKED_NEEDS_HUMAN` 和缺失契约。

## 完成定义（Definition of Done）
- [ ] RND-335 PASS 证据已记录
- [ ] AC-1 ~ AC-9 均有自动化测试
- [ ] 旧 URL/bookmark/auth 保持，入口下沉且 Settings 可发现
- [ ] action/state/filter/detail 三 locale 齐全
- [ ] API 值仅安全 DOM 渲染，detail 使用 allowlist
- [ ] `make verify` 全绿，三语言/窄屏视觉检查完成
- [ ] 本票归因 diff 只在拥有文件，用户 dirty diff 未改
- [ ] QA Summary 列出默认 query、action 覆盖数和 unknown fallback
- [ ] 未 commit、未 push、未建分支

## 风险与回滚（Risk & rollback）
- category 只消费 item.category；前端 action map 只负责文案，并由测试对照后端目录。
- detail 未知字段默认不显示，禁止通用渲染。
- 所有 filters 发服务器，避免 total/pagination 错误。
- 只移除 nav item，不删除 route。
- 回滚恢复 nav/card/template/i18n/tests；无 route/schema/migration/data 操作。

## 人工点位（Human touchpoints）
- **Trigger**：RND-335 PASS 后，Haisu 将 RND-336 置 In Progress 并交付本提示词。
- **Gate**：Haisu 浏览器 review 三语言、窄屏、90 天默认、details 层级、Settings 入口。
- **Escalation**：RND-335 未 PASS、目录不稳、operator 无法用既有 users API、需要改后端/route/shared styles 时，`BLOCKED_NEEDS_HUMAN`。
- **Escalation**：2 轮仍 FAIL 或文案需产品决策时，附最小选项，不猜。

## 开发 agent 执行指引（步骤）
1. 核对 `tasks/RND-335-qa-verdict.json` PASS，读实际 backend 契约；记录 HEAD/dirty baseline。
2. 先扩 tests：nav、Settings link、旧 route/Settings active、默认 query、action key 集合、unknown、安全 detail、三 locale。
3. 最小修改 sidenav/page router/settings card；不碰 route/main/settings.js/styles.css。
4. audit template 用原生 DOM 构建 feed/filter/details；API data 走 textContent，detail allowlist；默认 90 天/include_system=false。
5. RND-328 audit 锚点附近补三 locale；测试从后端目录对照 action map。
6. 跑验证并做三语言/窄屏检查；输出 QA Summary + 归因 `git status`；不要 commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不改 CI/CD、部署、`.gitignore` 或生产数据。
- 不改 RND-335 backend、DB、route contracts、settings.js 或 shared styles。
- 不用 Jinja/React/Vue/bundler；SSR + 原生 JS。
- 不用 innerHTML 插入 API 值，不通用展示 detail，不渲染敏感字段。
- 不扩大为 dashboard/export/alert/SIEM/Agent activity。
- 证据优先：以 server query、DOM 安全 tests、三 locale、视觉 gate 和 exit 0 为证。
