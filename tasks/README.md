# tasks/ — 工单交付物唯一归属地

本目录集中管理**所有**工单的开发提示词、验收提示词、QA 判定与 QA 报告。
仓库其它位置（根目录、`.workbuddy/`、`deliverables/`）**不得**再新建这类文件。

完整流程见 `docs/ticket-autopilot-workflow.md`；工程纪律见 `DEV_AGENT_RULES.md`。

## 目录布局

```
tasks/
├── README.md                 # 本文件
├── _templates/               # 模板（dev / qa prompt + verdict schema）
├── RND-<n>-dev-prompt.md     # 未完成工单：留在根目录
├── RND-<n>-qa-prompt.md
└── archive/                  # 已 Done / Canceled 工单：整组下沉
```

`tasks/` 根目录始终只剩「还要用的」，一眼可见待办面。

## 命名规范（强制）

| 文件 | 命名 |
|---|---|
| 开发提示词 | `RND-<n>-dev-prompt.md` |
| 验收提示词 | `RND-<n>-qa-prompt.md` |
| QA 判定（机器可读） | `RND-<n>-qa-verdict.json`（schema 见 `_templates/`） |
| QA 报告（叙述性） | `RND-<n>-qa-report.md` |

- 前缀一律大写 `RND-`。
- 同一工单多份开发提示词时用后缀区分：`RND-229-dev-prompt-search-pagination-flake.md`。
- 跨工单合并的判定可用组合名：`RND-327-328-qa-verdict.json`。

## 归档规则

工单在 Linear 置为 **Done** 或 **Canceled** 后，把该工单的**全部**文件一次性搬走：

```bash
git mv tasks/RND-<n>-*.md tasks/RND-<n>-*.json tasks/archive/
```

- 用 `git mv`（保留 history），不要 `mv` + `git add`。
- 归档只搬文件、不改内容，仅修正跨文件引用路径。
- Agent **不 commit / push**，搬完由 Haisu 本人提交。

## 沿革

- **2026-07-29**：`.workbuddy/prompts/` 中 11 张开放工单迁入本目录，统一改名。
- **2026-07-31**：`.workbuddy/prompts/` 整个撤销删除，其 `archive/` 108 个文件 +
  仓库根目录 4 份 QA 报告全部收进 `tasks/archive/`；合计归档 200 个文件。
  自此仓库内只有一套提示词产线、一个目录。
