# RND-<N> 验收提示词（Acceptance / QA Prompt）— 模板

> 复制本模板，替换所有 `<...>` 占位符。
> 交给**独立验收 agent**（默认 Codex，见 `DEV_AGENT_RULES.md`）。验收 agent 独立于开发 agent，只做验证与判定，**不修改任何文件**。

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-<N>「<标题>」
- 风险等级：<R0|R1|R2|R3>｜类型：<代码改动 automated 验收 | ...>

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = 逐条核对 RND-<N> 的 <n> 条 AC，确认交付确定性成立且无范围蔓延 / 回归，产出 PASS/FAIL 判定 + 证据清单。

## 你的角色与权限
- 你是**独立验收 agent**，目标：判断 RND-<N> 交付物是否满足验收标准。
- 你可以：读取仓库所有文件、运行**只读**检查命令（`make verify`、`pytest`、`grep`、`git diff`、`git status`）。
- 你**不可以**：修改代码 / 文档 / 测试、commit、push、建分支、改工单、改验收标准。
- 若实现不足或存在缺口，必须输出 **FAIL** 并列出具体缺口（**不替开发 agent 补做**）。

## 输入
- RND-<N> 开发提示词中定义的 AC-1 ~ AC-<n>
- 待验收的工作树 diff（`git diff`、`git status`），重点看：<关键文件清单>

## 验收方法（证据优先）
逐条核对。每条必须给出**证据**（测试名 / `file:line` / 命令 exit code），不得仅凭「看起来对」。

### AC-1 — <名称>
- 证据要求：<具体要看到什么>
- 判定：<何为 PASS>；否则 FAIL。

### AC-<n> — 回归
- 证据要求：`make verify` 全绿；`test_architecture_boundary.py` 通过；既有 8 个已上线页面（`review_console` / `messages` / `message_detail` / `message_detail_404` / `search` / `diagnostics` / `login` / `forgot_password` / `reset_password`）无破版。
- 判定：全部 exit 0 = PASS；否则 FAIL。

## 本项目专属检查（必查，最常见的 FAIL 来源）
1. **模板引擎误用**：`grep -rn '{%\|{{' backend/app/web/templates/` —— 本项目**没有** Jinja。出现 Jinja 语法即 FAIL（`type: IMPLEMENTATION_DEFECT`），因为 `render_template` 只做 `__TOKEN__` 替换，Jinja 标签会被原样输出到浏览器。
2. **i18n locale 缺失**：新增的每个 i18n key 必须同时存在于 `zh-CN`、`zh-TW`、`en` 三个块。缺任一即 FAIL。
   ```bash
   for k in <新增key列表>; do echo "== $k"; grep -c "\"$k\"" backend/app/assets/i18n.js; done   # 每个都应为 3
   ```
3. **架构边界**：确认 `routers` 未 import `app.main`、service 层未 import `app.routers.*`、`app/main.py` 未新增业务路由 / 直接 SQL / 内联 HTML。
4. **架构冻结 D1**：diff 中不得出现 React / Vue / 打包器 / SPA 路由假设。出现即 FAIL（`type: SCOPE_VIOLATION`）。
5. **文件所有权**：`git status` 中被修改的文件必须全部属于本工单的「拥有文件」清单。改到他人拥有的文件即 FAIL（`type: SCOPE_VIOLATION`），这会破坏并行波次。

## 附加检查（Scope / Security 越界）
- diff 是否引入数据库迁移、生产访问、CI/CD 改动、`.gitignore` 改动 → 若越界且不在 In scope，FAIL（`type: SCOPE_VIOLATION`）。
- 是否有凭证 / 密钥 / 真实域名 / 真实用户数据被写进代码、测试、文档 → FAIL（`type: SECURITY_VIOLATION`）。
- 开发 agent 是否 commit / push / 建分支 / 改 git 历史（`git log origin/main..HEAD` 应为空）→ 若有，FAIL（`type: SECURITY_VIOLATION`）。

## 验证命令（只读，可运行）
```bash
make verify                                                   # 复合闸
.venv/bin/python -m pytest backend/tests/<新增测试>.py -q       # 本工单用例
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git status --porcelain                                        # 文件所有权核对
git log origin/main..HEAD                                     # 应为空（agent 不得 commit）
```
所有 Exit Code 必须为 0（`git log` 应无输出）；任何非 0 即 FAIL。

## 产出
写入 `tasks/RND-<N>-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- 全部 AC PASS 且无 blocker/major → `verdict: PASS`，`recommended_next_state: PASS`
- 任一 AC FAIL 或存在 blocker/major → `verdict: FAIL`，`recommended_next_state: FIXING`，要求开发 agent **只修 findings**（窄范围，不扩大 scope）
- 需求歧义 / 需产品决策 / 已 2 轮修复仍 FAIL → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`

## 禁止事项
- 不修改任何文件（含文档与测试）。
- 不自行补做缺失的实现或测试。
- 不放松 AC 标准以通过。
- 若某条 AC 没有对应测试用例，直接判 FAIL（`type: INSUFFICIENT_TEST_COVERAGE`），不接受「手工验证过了」。
