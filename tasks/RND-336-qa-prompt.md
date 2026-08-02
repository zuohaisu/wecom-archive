[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-336 的 9 条 AC，证明审计入口已下沉、默认信号由服务端过滤、人类文案和安全详情完整，旧 URL 与三语言无回归。

# RND-336 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-336 的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始「验收方法」里的 AC-1，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发下方「产出」规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：读「任务身份」确认工单号，然后直接进入「验收方法」逐条核对。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-336「Frontend: Move Audit Log under Settings as Security & activity」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-336/frontend-move-audit-log-under-settings-as-security-and-activity
- 风险等级：R1｜类型：前端代码独立验收（只读 + 人工视觉证据）
- 依赖：blocked by RND-335；依赖未 PASS 或未使用最终契约时，本票不得 PASS。

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = AC-1 ~ AC-9 每条有测试/源码/运行时证据，并产出 PASS/FAIL/BLOCKED 与明确视觉人工点位。

## 你的角色与权限
- 只读验证 RND-336，不补代码。可以读文件、跑 tests、启动本地服务做只读浏览器检查、检查 DOM/请求/locale。
- 不修改实现、测试或文档，不 commit/push/建分支，不改票或放松 AC。
- 若视觉环境不可用，自动化可继续，但需人眼证明的布局不得凭想象 PASS；在 verdict notes 明确 `HUMAN_VISUAL_REVIEW_PENDING`。

## 输入
- `tasks/RND-336-dev-prompt.md` 的 AC-1 ~ AC-9。
- `tasks/RND-335-qa-verdict.json` 与实际 action/category/API 契约。
- 本票 diff：audit template/page router、sidenav、settings template、i18n、tests。

## 共享工作树归因（先做）

提示词撰写时已有用户改动：`backend/app/web/static/styles.css`、`scripts/deploy_server.sh`、`scripts/tests/deploy_server.bats`，`main` ahead 1。

- 既有改动不归因于 RND-336，不得修改或据此误判。
- 若 RND-336 在既有 `styles.css`/部署 diff 上继续编辑，仍是越权叠加，判 `SCOPE_VIOLATION`。
- 只把开发期间新增 commit 归因给 agent；既有 ahead commit 写 notes，不自动 FAIL。

## 验收方法（证据优先）

### AC-1 — 依赖闸
- 证据：RND-335 verdict 为 PASS；实际 `audit.py` 目录、API item.category、category/include_system tests 存在并通过；前端 map 基于最终 action 集合。
- 判定：依赖完整且版本一致 = PASS；缺 verdict/契约、前端自造 category 或映射漏 action = BLOCKED/FAIL。

### AC-2 — 信息架构与 URL 兼容
- 证据：sidenav 不含 audit-log；Settings HTML 有 `/admin/audit-logs` 可见 card/link；运行时旧 URL 未登录仍重定向、登录后 200；页面 Settings 有 `aria-current=page`，breadcrumb 为 Settings / Security & activity。
- 判定：入口下沉但 route/bookmark/auth 保留 = PASS；删 route、改 URL、仍留一级入口或 Settings 不可发现 = FAIL。

### AC-3 — 人类可读 feed
- 证据：测试从 RND-335 目录取 action 集并对照前端 map；每个 action 主层级为三语言句子/标签；raw action/audit ID/object type/id 只在 `<details>`；未知 action 通用 fallback 且不抛异常。
- 判定：已知全覆盖 + 未知安全降级 = PASS；raw code 仍是主列、漏映射或未知崩页 = FAIL。

### AC-4 — 默认信号与服务端过滤
- 证据：首个 fetch query 含 90 天 from 与 `include_system=false`；30/90/all、category、operator、system toggle 更新 URLSearchParams 并重置 offset；operator value 为 admin_user_id；pagination/total 用服务端响应。
- 判定：过滤由服务器执行且计数一致 = PASS；只过滤当前 DOM、默认含 system、all 仍发日期或 operator 发显示名 = FAIL。

### AC-5 — 安全详情
- 证据：读取按 action/key allowlist；用含 `<img onerror>`、message_body、token、secret、signed_url、storage_key、search_text、path 的 fixture，断言不执行、不显示禁止值；允许的 format/count/changed_keys/status 可见且走 textContent。
- 判定：默认拒绝未知字段、允许字段可读 = PASS；通用遍历 detail 或 API 值进入 innerHTML = blocker FAIL。

### AC-6 — 状态与可访问性
- 证据：loading、empty、network error、401/403、unknown action、operator API failure、pagination 有 tests；label/aria-live/status/details/summary/select/checkbox/button 语义存在；无 create/update/delete/audit export/alert 控件。
- 判定：状态完整、键盘/读屏基础正确、只读 = PASS；缺 401/403 或 operator 失败拖垮 feed = FAIL。

### AC-7 — 三语言
- 证据：新增/改名 key 在 zh-CN/zh-TW/en 各一次；运行时切换检查 title、breadcrumb、Settings card、filters、actions、fallback、details、states；无用户单语言硬编码。
- 判定：三 locale 完整且动作句翻译 = PASS；只改标题而 action/detail/filter 未翻 = FAIL。

### AC-8 — 后端与隐私边界
- 证据：本票归因 diff 不含 `audit.py`/API/db/migration/main/route contracts/settings.js/styles.css；页面不发送搜索文本或新增审计写请求；API data 不写 localStorage/sessionStorage。
- 判定：纯消费、无隐私/后端越界 = PASS。

### AC-9 — 回归
- 证据：`make verify`、audit/settings/sidenav/backend-contract/architecture tests exit 0；settings password/config 仍工作；其它剩余 nav item 顺序/路径/可用态无意外变化；旧 audit route auth 保持。
- 判定：全绿 = PASS；本票归因回归 = FAIL。共享工作树他票失败先隔离归因。

## 本项目专属检查（必查）
1. 模板无 Jinja；出现 `{%` 或 `{{` 即 `IMPLEMENTATION_DEFECT`。
2. 每个新增 i18n key 在三个 locale 均存在，位于正确区域。
3. 无 React/Vue/bundler/SPA router/第三方 UI 库。
4. 不新增/删除 route；`/admin/audit-logs` 仍注册一次，route-contract tests 不改。
5. 本票归因文件限 dev prompt 拥有清单；尤其 styles.css/settings.js/RND-335 backend 不得叠加修改。
6. 所有 item/detail API 值进入 textContent 或等价安全属性；任何 innerHTML 路径都必须有恶意 fixture 证明 API 值不可达，否则 FAIL。

## 附加检查（Scope / Security）
- 新增 audit export、alert/SIEM/dashboard/Agent activity、普通浏览日志 → `SCOPE_VIOLATION`。
- 展示/存储 message body、token、secret、signed URL、storage key、search text、path → `SECURITY_VIOLATION`。
- agent 新 commit/push/建分支/改历史 → `SECURITY_VIOLATION`，先与 baseline 区分。

## 验证命令（只读，可运行）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_audit_page.py backend/tests/test_sidenav.py backend/tests/test_rnd251_settings_page.py -q
test ! -f backend/tests/test_rnd336_security_activity_page.py || .venv/bin/python -m pytest backend/tests/test_rnd336_security_activity_page.py -q
.venv/bin/python -m pytest backend/tests/test_rnd295_audit_list.py backend/tests/test_rnd335_security_activity.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -rn '{%\|{{' backend/app/web/templates/audit_log.html backend/app/web/templates/settings.html
git diff --check
git diff --stat -- backend/app/audit.py backend/app/routers/audit.py backend/app/db/models.py backend/alembic/versions backend/app/main.py backend/app/web/static/settings.js backend/app/web/static/styles.css
git status --porcelain
git log origin/main..HEAD
```

说明：模板 grep 无匹配时 exit 1 是预期，输出必须为空。`git diff --stat` 结合 baseline 归因，不能把用户既有 styles.css diff 算到本票。

## 人工运行时点位（自动化不能替代）
- 登录后从 Settings 账号区域进入“安全与活动”，路径仍为 `/admin/audit-logs`，Settings 侧栏 active。
- 切换 zh-CN/zh-TW/en，检查桌面/窄屏：主句优先、technical details 次级、filters 不溢出、键盘可展开。
- 用 human/system 混合 fixture 检查默认只见 human/security，开启 system 后出现后台批处理；翻页 total/页码正确。
- 无浏览器/fixture 时，notes 明确未验证点，不能以静态源码冒充视觉 PASS。

## 产出
写入 `tasks/RND-336-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- 全 AC PASS、无 blocker/major 且有视觉证据 → `verdict: PASS`，`recommended_next_state: PASS`。
- 代码通过但视觉 review 未完成：notes 明确 `HUMAN_VISUAL_REVIEW_PENDING`，不得声称最终交付完成。
- 任一 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`，findings 只给最小修复。
- backend 依赖漂移或需产品决策 → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。

## 禁止事项
- 除规定 verdict 外不改文件，不替开发补实现/测试。
- 不放松“server-side filtering、detail allowlist、三 locale、旧 URL”四个核心标准。
- 无 action-map 对照 test、恶意 detail fixture、401/403 test 中任一项，直接 `INSUFFICIENT_TEST_COVERAGE`，不接受“手工看过”。
