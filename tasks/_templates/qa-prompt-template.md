# RND-<N> 验收提示词（Acceptance / QA Prompt）— 模板

> **⚠️ 休眠模板（2026-08-28 起）** — 独立 QA agent 流程已停用，现行流程是 dev 自
> QA + required CI 判定（见 `AGENTS.md`）。仅当 Haisu 为高风险改动显式指派可选的
> **qa 角色**时才使用本模板。

> 复制本模板，替换所有 `<...>` 占位符。**保留下面的「立即执行」块，逐字复制到生成的提示词里** —— 它是防止接收 agent（尤其 Qoder 等在收到长文档时习惯先反问用户意图的工具）把这份提示词当成"待讨论文档"而不是"待执行任务"的关键。若本工单同时有 `[Goal check]` 首行，「立即执行」块放在 `[Goal check]` 行**之后**——`[Goal check]` 必须留在文件字面第一行。

---

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-<N> 的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始「验收方法」里的 AC-1，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发下方「产出」规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：读「任务身份」确认工单号，然后直接进入「验收方法」逐条核对。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档
- 工单：RND-<N>「<标题>」
- 风险等级：<R0|R1|R2|R3>｜类型：<代码改动 automated 验收 | ...>

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = 逐条核对 RND-<N> 的 <n> 条 AC，确认交付确定性成立且无范围蔓延 / 回归，产出 PASS/FAIL 判定 + 证据清单。

## 你的角色与权限
- 你是**独立验收 agent**，目标：判断 RND-<N> 交付物是否满足验收标准。
- 你可以：读取仓库所有文件、运行**只读**检查命令（`make verify`、`pytest`、`grep`、`git diff`、`git status`）。
- 你**不可以**：修改代码 / 文档 / 测试、commit、push、创建额外分支、切换离开
  已分配的交付分支、改工单、改验收标准。
- 若实现不足或存在缺口，必须输出 **FAIL** 并列出具体缺口（**不替开发 agent 补做**）。

## 输入
- RND-<N> 开发提示词中定义的 AC-1 ~ AC-<n>
- 待验收的当前工单 diff（未提交时用 `git diff`；已获批提交时用
  `git show <本票-commit>`），重点看：<关键文件清单>
- 交付分支中更早的同 Epic 工单 commit（如有，只作为集成基线，不计入本票 scope）

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
5. **文件所有权**：当前本票 diff 中的文件必须全部属于本工单的「拥有文件」清单。
   改到他人拥有的文件即 FAIL（`type: SCOPE_VIOLATION`）。分支中更早的获批工单
   commit 不归因到本票，但其 issue↔commit 映射必须清楚。

## 附加检查（Scope / Security 越界）
- diff 是否引入数据库迁移、生产访问、CI/CD 改动、`.gitignore` 改动 → 若越界且不在 In scope，FAIL（`type: SCOPE_VIOLATION`）。
- 是否有凭证 / 密钥 / 真实域名 / 真实用户数据被写进代码、测试、文档 → FAIL（`type: SECURITY_VIOLATION`）。
- 当前分支必须是已分配的交付分支且不是 `main`。检查是否存在未经 Haisu 批准的
  commit/push、额外分支或历史改写；若有，FAIL（`type: SECURITY_VIOLATION`）。
  `git log origin/main..HEAD` 可以包含同一 Epic 更早的获批工单 commit，但必须满足
  一票一个最终 commit、一个 commit 不混票；当前本票至多对应一个最终 commit。

## 验证命令（只读，可运行）
```bash
make verify                                                   # 复合闸
.venv/bin/python -m pytest backend/tests/<新增测试>.py -q       # 本工单用例
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git status --porcelain                                        # 文件所有权核对
git branch --show-current                                     # 必须是已分配的交付分支，不能是 main
git log --oneline origin/main..HEAD                           # 核对每票一个 commit 及授权/范围
git diff                                                      # 未提交时只应包含当前本票
# 或 git show <本票-commit>                                   # 已提交时核对本票唯一 commit
```
所有命令 Exit Code 必须为 0；分支、提交授权或范围不符合即 FAIL。

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
