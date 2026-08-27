# tasks/ — 工单交付物唯一归属地

本目录集中管理**所有**工单的交付物：QA Summary、（可选的）开发提示词、研究报告与
其它证据。仓库其它位置（根目录、`.workbuddy/`、`deliverables/`）**不得**再新建这
类文件。

**权威流程与工程纪律见 `AGENTS.md`（唯一来源）。**
`docs/ticket-autopilot-workflow.md` 已于 2026-08-28 停用，仅作休眠参考。

自 2026-08-14 起，所有工单都在非 `main` 交付 worktree/分支中完成并通过 PR merge
到 `main`。一票必须对应一个最终 commit；一个 worktree/branch/PR 可以串行承载同一
Epic 的多张相关工单及其多个 commit。并行工单使用不同 worktree，任何 commit 都不
得混票。旧提示词里“直接在 main”“不建分支”等交付机制已由
`AGENTS.md` v6 取代；旧提示词的产品范围和验收标准仍然有效，不为此机械
改写历史文件。

## 目录布局

```
tasks/
├── README.md                  # 本文件
├── WAVE-ownership.md          # 跨票文件所有权矩阵（历史波次，休眠）
├── _templates/                # 模板
├── <KEY>-qa-summary.md        # 未关闭工单：留在根目录
├── <KEY>-dev-prompt.md        # 可选
└── archive/                   # 已关闭工单：整组下沉
```

工单键有两种，都指向一个 GitHub Issue：`RND-<n>`（自 Linear 迁入，issue 标题带
`[RND-<n>]`）与 `GH-<n>`（GitHub 原生开的票，`<n>` 就是 issue 号）。

`tasks/` 根目录始终只剩「还要用的」，一眼可见待办面。

## 命名规范（强制）

| 文件 | 命名 | 何时产出 |
|---|---|---|
| QA Summary | `<KEY>-qa-summary.md` | **每票必有**，dev 跑完 `make verify` 后自己写 |
| 开发提示词 | `<KEY>-dev-prompt.md` | 可选：需要交给另一个会话，或想留范围记录时 |
| 研究报告 | `<KEY>-research-report.md` | research 角色产出 |
| 其它证据 | `<KEY>-e2e-evidence.md` 等 | 按需 |
| 波次所有权表 | `WAVE-ownership.md`（全局唯一，不带工单号，不归档） | 休眠 |

`<KEY>-qa-prompt.md` 与 `<KEY>-qa-verdict.json` 属于**已停用的独立 QA 流程**，除非
Haisu 指派可选的 qa 角色，否则不再新建。

- 工单键前缀一律大写（`RND-` / `GH-`）。
- 同一工单多份开发提示词时用后缀区分：`RND-229-dev-prompt-search-pagination-flake.md`。
- 跨工单合并的判定可用组合名：`RND-327-328-qa-verdict.json`。

## 归档规则

GitHub Issue **关闭**（completed 或 not planned）后，把该工单的**全部**文件一次性搬走：

```bash
git mv tasks/<KEY>-*.md tasks/<KEY>-*.json tasks/archive/
```

- 用 `git mv`（保留 history），不要 `mv` + `git add`。
- 归档只搬文件、不改内容，仅修正跨文件引用路径。
- Agent 未经 Haisu 批准不得 commit / push；批准后为本票创建唯一 commit，并只推送
  已分配的交付分支，通过 PR merge，绝不直接推送 `main`。

## 沿革

- **2026-07-29**：`.workbuddy/prompts/` 中 11 张开放工单迁入本目录，统一改名。
- **2026-07-31**：`.workbuddy/prompts/` 整个撤销删除，其 `archive/` 108 个文件 +
  仓库根目录 4 份 QA 报告全部收进 `tasks/archive/`；合计归档 200 个文件。
  自此仓库内只有一套提示词产线、一个目录。
- **2026-08-28**：Ticket Autopilot 停用，独立 QA agent 与 `qa-verdict.json` 一并
  退场；改为 dev 自 QA + required CI 判定。`DEV_AGENT_RULES.md` 与 `docs/AGENTS.md`
  合并进根目录 `AGENTS.md` v6。
