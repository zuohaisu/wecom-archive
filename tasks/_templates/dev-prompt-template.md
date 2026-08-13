# RND-<N> 开发提示词（Developer Prompt）— 模板

> 复制本模板，替换所有 `<...>` 占位符。**任一必填字段为空或仅写 TBD 的工单不得置 In Progress**，按 `BLOCKED_NEEDS_HUMAN` 上报。
> 由 PM agent 生成，交给**开发 agent** 执行。开发 agent 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出一行 `[Goal check]`。

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-<N>「<标题>」
- Linear URL：<url>
- 优先级：<priority>｜风险等级：**<R0|R1|R2|R3>**
- 所属波次：<R1 · 前端快赢四页 | ...>
- 交付 worktree/branch：<名称；可为本票专用，或同 Epic 串行交付容器>
- 同一交付 PR 的其他工单：<无 | RND-...；只列相关工单>

## [Goal check]
本工作推进「<闭环阶段>」阶段，证据 = <可测量证据>。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）
<必填。写清仓库**当前已有什么**（含具体文件:行号）与**当前缺口是什么**。这一节的作用是防止 agent 从零重写已存在的东西。>

**本项目已知的高频踩坑点（每次都要复述给 agent）：**
- ❗ **没有模板引擎。** `backend/app/web/__init__.py` 明确写着「No template engine is introduced on purpose」。`render_template(name, **ctx)` 只做 `__TOKEN__` 单遍替换。**不存在 Jinja，不要写 `{% include %}` / `{% for %}`。** 需要复用片段就用 `__TOKEN__` + Python 侧生成 HTML 字符串。
- ❗ **i18n 有 3 个 locale**：`zh-CN` / `zh-TW` / `en`，都在 `backend/app/assets/i18n.js` 的 `LocaleRegistry` 里。新增任何界面文案，**3 个 locale 都要加**，缺一个就是 FAIL。
- ❗ **架构边界是硬闸**：`backend/tests/test_architecture_boundary.py` 在 `make test` 里跑。`routers` 不得 import `app.main`；service 层不得 import `app.routers.*`；`app/main.py` 里不得新增业务路由 / 直接 SQL / 内联 HTML。失败即硬停止，不得绕过。
- ❗ **架构冻结 D1**：SSR + 原生 JS。禁止引入 React / Vue / 构建步骤 / SPA 路由假设。

## 目标（Goal）
<1–2 句：意图与价值。>

## 范围边界
**In scope（交付物）：**
1. <具体产出>

**Out of scope（显式非目标，至少一条具体项，禁止写 TBD/N/A）：**
- <明确不许碰的东西>

**本工单拥有的文件（只许写这些）：**
- `<path>`

> **写这份清单时（PM 职责，不是开发 agent 的）**：所有权清单的判据是「**跑通验证闸所必需的全部文件**」，不只是功能文件本身。凡是本工单的改动会导致其失败、且必须随之更新的守卫性文件（契约快照、计数基线、白名单式测试等），都属于本工单，必须写进清单并注明**允许的最小改动范围**。
> 判定方法：把本工单的改动想象成已完成，然后问「`make verify` 会因此红在哪里」——那些地方就在清单里。本项目当前已知的此类守卫见 `docs/ticket-autopilot-workflow.md` §3.3（**以该文档为准，不在此复制清单，避免两处漂移**）。

**本工单只读、绝不可写的文件（他人所有）：**
- `<path>` — 所有者 RND-<M>

> 若实现中发现必须改他人拥有的文件 → **停止**，标记 `BLOCKED_NEEDS_HUMAN` 并说明原因。不要「顺手改一下」。

## 验收标准（Acceptance Criteria）
> 每条必须是可判定的 pass/fail 陈述，写成 Given/When/Then 或等价的客观可观察结果。

- **AC-1 <名称>**：<Given … When … Then …>
- **AC-2 …**
- **AC-N 回归**：`make verify` 全绿，`test_architecture_boundary.py` 通过，既有页面无破版。

## 验证方式（Verification — 确定性闸）
- 类型：**automated**
- 命令：
  ```bash
  make verify                                                    # 复合闸：lint-diff → typecheck → build → test
  .venv/bin/python -m pytest backend/tests/<新增测试>.py -q        # 本工单新增用例
  .venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q   # 架构硬闸
  ```
- 通过 = 全部 AC 满足 **且** 上述命令 Exit Code 均为 0。
- 失败 = 任一 AC 不满足 → 进入有界修复（最多 2 轮，只修 findings）。

## 依赖（Dependencies）
- <阻塞工单 / 前置条件；若未满足则不得开始，应上报而非猜测>

## 完成定义（Definition of Done）
- [ ] 全部 AC 满足
- [ ] `make verify` 全绿
- [ ] 新增测试覆盖每一条 AC
- [ ] 3 个 locale 的 i18n 键齐全（若涉及界面文案）
- [ ] 无新增 lint / 类型错误
- [ ] 只改了本工单拥有的文件（`git status` 自证）
- [ ] 产出 QA Summary（格式见 `DEV_AGENT_RULES.md` 的 QA Rules）
- [ ] **未 commit、未 push**（等 Haisu 批准）
- [ ] 当前位于已分配的交付 worktree/分支，且分支不是 `main`
- [ ] working tree 只含本票未提交改动；更早的同 Epic 工单已各自形成获批 commit

## 风险与回滚（Risk & rollback）
- 风险：<可能坏什么>
- 回滚：<如何撤销>

## 人工点位（Human touchpoints）
- **Trigger**：Haisu / PM 将 RND-<N> 置 In Progress（本提示词即启动信号）。
- **Gate**：Haisu 审阅后批准本票的唯一 commit，以及交付分支 push / 创建或更新 PR
  （agent 不得自行执行）。
- **Escalation**：2 轮修复后仍 FAIL，或遇到需要产品决策的歧义 → `BLOCKED_NEEDS_HUMAN`，附上下文，**不要猜**。

## 开发 agent 执行指引（步骤）
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、<关键现有文件>。
2. <步骤>
3. 跑 `make verify`，确认全绿。
4. 输出 QA Summary + `git status`（证明只动了自己的文件），**不要 commit**。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 在已分配的非 `main` 交付 worktree/分支工作；它可以服务本票，也可以串行服务同
  Epic 多票。开始本票前，上一票必须已完成 QA 并形成获批 commit；不得让多票未提交
  diff 共存。未经批准不 commit/push，不另建分支、不改 git 历史；任何情况下都不得
  直接 commit/push `main`。
- 一票有且只有一个最终 commit，单个 commit 不得混入其他工单；多票 PR 必须保留
  各票 commit，禁止 squash 成一个 commit。
- 除非本工单 In scope 明确列出且 Haisu 已批准，否则不改 CI/CD 配置、部署设置或
  `.gitignore`；批准修改仓库配置不等于批准生产操作。
- 不碰生产数据 / 密钥；凭证只从环境变量读，不写进代码、文档、测试、脚本。
- 不扩大 Scope：只做 In scope；新想法记录到 PR 说明或新工单，绝不隐式加入。
- 复用优先，最小正确改动优先于大范围重构。
- 证据优先：不以「我觉得可以了」自证，以命令 exit 0 / 测试通过为证。
