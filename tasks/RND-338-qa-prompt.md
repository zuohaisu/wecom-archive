[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-338 的 9 条 AC，证明“归档健康”对小微管理员清晰、可信、可操作，且不会由单页技术数据制造虚假健康结论。

# RND-338 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-338 的独立验收 agent，任务从你读到这句话开始。

- **不要**询问目的或是否开始；直接执行依赖检查和 AC-1。
- **不要**先输出计划等待确认；只读验收无需许可。
- 若必须人工视觉证明但环境不可用，继续完成自动化部分，并按下方规则记录 `HUMAN_VISUAL_REVIEW_PENDING`，不得凭源码声称视觉 PASS。
- 唯一允许中止是写出带具体缺口的 BLOCKED verdict，不是向用户反问。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-338「将消息可达性诊断重构为面向小微客户的归档健康 UI」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-338/将消息可达性诊断重构为面向小微客户的归档健康-ui
- 风险等级：R1｜类型：前端代码独立验收（只读 + 人工视觉证据）
- 依赖：RND-337 最终 QA PASS；RND-339 不阻塞本票。

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = AC-1 ~ AC-9 每条有测试/源码/运行时证据，六态、轮询、安全 DOM、三语言和窄屏均有明确判定。

## 你的角色与权限
- 只读验证 RND-338，不替开发修代码/测试/文案。
- 可以读取文件、运行 tests、启动本地服务并做只读浏览器/DOM/网络检查。
- 不修改实现、测试、工单，不 commit/push/建分支；唯一允许写入 `tasks/RND-338-qa-verdict.json`。

## 输入
- `tasks/RND-338-dev-prompt.md` 的 AC-1 ~ AC-9。
- `tasks/RND-337-qa-verdict.json` 和实际 reachability-checks schema/OpenAPI/tests。
- 本票归因 diff：diagnostics template/JS/CSS、i18n、web route docstring、page/render/sidenav/聚焦 tests。

## 共享工作树归因（先做）

提示词撰写前产品代码 clean；六个 RND-337/338/339 prompt 是 PM artifacts，不归因于开发实现。验收时重记 status/diff/log，并按 dev prompt 拥有清单隔离其他变化。

- 修改 RND-337 backend、RND-339 automation、main/HTTP contract/shared styles/nav structure 即本票 scope violation。
- 用户/他票既有 diff 不得修改，也不能仅因存在而让本票自动 FAIL；必须说明归因。

## 验收方法（证据优先）

### AC-1 — 依赖闸
- 证据：RND-337 verdict 为 PASS；实际 POST/latest schema、六态与 safe error contract 可读；UI field access 与实际 response 一致。
- 判定：依赖完整且无前端猜测 = PASS；缺 verdict、API drift、mock 自造不存在字段 = BLOCKED/FAIL。

### AC-2 — 命名与 URL
- 证据：三 locale nav/title/breadcrumb/description 为 Archive health 对应文案；`/admin/diagnostics/reachability` 未登录保持 auth gate、登录后 200；route count/path 不变。
- 判定：入口可发现、bookmark 保留、无主层“reachability rate/可达率” = PASS。

### AC-3 — 六态首屏可信度
- 证据：对 healthy/attention/checking/no_data/incomplete/error 与 complete true/false fixtures 检查可见 title/explanation/time/scope/count/CTA；healthy 文案包含范围/最近完整检查限定；partial/no run/error 没有绿色健康样式或成功措辞。
- 判定：只消费 server state+complete，未在 JS 用比例重算 = PASS；任何客户端推断整体健康 = blocker FAIL。

### AC-4 — 手动检查生命周期
- 证据：记录实际/模拟网络序列：单次 POST、防双击、active run、poll latest、terminal stop、timeout/backoff、unload cleanup、retry、401/403/network/server error；fake timers 证明无残留/无限请求。
- 判定：每条有自动化断言且 UI 可恢复 = PASS；重复触发、多 poll controller、永久 spinner = FAIL。

### AC-5 — 渐进披露
- 证据：首屏默认不展开技术统计；`<details>` 内有安全的人类 reason/count/scope/version；未知 reason 安全 fallback；raw identifiers/content/path/traceback sentinel 不在 DOM/HTML/console。
- 判定：结论优先、技术次级且无逐条样本 = PASS；table/log dump 成为首屏或泄漏禁止字段 = FAIL。

### AC-6 — 安全 DOM 与异常数据
- 证据：用 `<img onerror>`、引号/URL、NaN/负数/巨大数、invalid date、unknown state/reason/error fixture；无脚本执行、attribute 注入、未捕获异常；API 值只进入 textContent/createElement/安全属性。
- 判定：默认安全降级 = PASS；API data 可达 innerHTML 或 unknown state 显示 healthy = blocker FAIL。

### AC-7 — 三语言
- 证据：新增/改名 key 在 zh-CN/zh-TW/en 各一次；运行时切换覆盖 nav/title、六态、CTA、loading/errors、details/reasons、labels；无单语言硬编码用户文案。
- 判定：三个 locale 完整、切换后行为符合现有 i18n 机制 = PASS。

### AC-8 — 可访问、响应式、只读
- 证据：aria-live/status、button disabled/focus、details summary、retry 的语义；键盘完整走 manual check/details；desktop 与 320px 截图/浏览器检查无横向溢出，颜色之外有文字/图标；DOM 无 auto-fix/export/sample/notification control。
- 判定：自动化语义 + 人工视觉均成立 = PASS；无视觉环境则记录 pending，不凭静态 CSS 完成该点。

### AC-9 — 回归与范围
- 证据：page/render/sidenav、RND-337 contract、HTTP contract、architecture、`make verify` 全绿；网络断言页面主流程不调用 `/api/admin/reachability-audit`；本票 diff 仅拥有文件。
- 判定：全绿且无后端/route/nav-structure/shared-style 变更 = PASS。

## 本项目专属检查（必查）
1. `diagnostics.html` 无 `{%` / `{{`；本项目无 Jinja。
2. 每个新增 i18n key 在三个 locale 均存在；不要只数总数，要确认值位于正确 locale block。
3. 无 React/Vue/bundler/chart library/SPA route；没有新增 route，HTTP route count 不变。
4. 搜索 `fetch(` / URL construction，主流程只能调用 RND-337 POST/latest；旧 audit URL 不得用于主结论。
5. 搜索 innerHTML/insertAdjacentHTML；只要 API 值可达就 blocker FAIL。静态常量 markup 也需证明数据不可达。
6. `healthy` CSS/文案分支必须同时要求 server healthy 和 complete；未知/缺字段 fail closed。

## 附加检查（Scope / Security）
- 新 dashboard/Settings/route、自动修复/补拉、通知/告警/export、逐条 message sample → `SCOPE_VIOLATION`。
- content/payload/raw sender/recipient/room/message id/path/traceback 在 DOM/log/storage → `SECURITY_VIOLATION`。
- API data 写 localStorage/sessionStorage 或注入 innerHTML → blocker `SECURITY_VIOLATION`。
- agent commit/push/建分支/改历史 → FAIL，先与 baseline 区分。

## 验证命令（只读，可运行）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_reachability_diagnostics_page.py backend/tests/test_reachability_diagnostics_render.py backend/tests/test_sidenav.py -q
test ! -f backend/tests/test_rnd338_archive_health_ui.py || .venv/bin/python -m pytest backend/tests/test_rnd338_archive_health_ui.py -q
.venv/bin/python -m pytest backend/tests/test_reachability_checks.py backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -rn '{%\|{{' backend/app/web/templates/diagnostics.html
git diff --check
git diff --name-only
git status --short --branch
git log origin/main..HEAD
```

模板 grep 无匹配时 exit 1 是预期。人工运行时还必须检查 zh-CN/zh-TW/en × desktop/320px、六态、manual check、keyboard 和 network 请求序列。

## 人工运行时点位（自动化不能替代）
- 直接打开旧 URL，确认 nav/title/首屏层级，不出现技术表格抢占视线。
- 六态逐一检查；重点比较 healthy vs incomplete/no_data/error 的颜色、图标、措辞，不能产生相同“没问题”印象。
- 发起检查：按钮 disabled、checking、poll 到 terminal、details 数字刷新；断网后可 retry。
- 三语言、桌面/320px、键盘 focus/展开；检查长英文/大数字/未知 reason 不溢出。

## 产出
写入 `tasks/RND-338-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- 全部 AC PASS、无 blocker/major 且视觉证据完成 → `verdict: PASS`，`recommended_next_state: PASS`。
- 自动化通过但视觉 gate 未完成：notes 明确 `HUMAN_VISUAL_REVIEW_PENDING`，不得宣称最终交付完成。
- 任一 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`；只列最小 findings。
- RND-337 漂移或需产品文案/状态决策 → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。

## 禁止事项
- 除 verdict JSON 外不修改任何文件，不替开发补实现/测试。
- 不放松“server state 为真相、partial 不健康、安全 DOM、三 locale、旧 URL”五个核心标准。
- 没有六态参数化 test、fake-timer cleanup、恶意 DOM fixture、三 locale test 中任一项，直接记 `INSUFFICIENT_TEST_COVERAGE`。
