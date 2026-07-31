# RND-232 执行计划（开源准备：去标识化与发布前梳理）

> 顶层计划，串联 6 个子任务。本文件**不直接改代码**——按本项目交付惯例，各子任务有独立执行提示词，由你（Haisu）或开发 agent 按节奏执行；**开发 agent 不自行 commit/push**。
> 配套文档：域名去标识化见 `archive/RND-233-dev-prompt.md`（已归档，RND-233 已 Done）；wiki 清理见 `archive/RND-236-dev-prompt.md`（已归档，RND-236 已 Done）；LICENSE/gitignore 见 `archive/RND-237-dev-prompt.md`（已归档，RND-237 已 Done）；营销网站草案见 `RND-239-marketing-site-plan.md`（原迁移方案已取消）。
>
> **2026-07-29 更新**：本文件已从 `.workbuddy/prompts/rnd-232-execution-plan.md`（该目录已于 2026-07-31 撤销）迁移合并进 `tasks/`，与本项目 Ticket Autopilot 提示词（见 `docs/ticket-autopilot-workflow.md`）统一存放；文件名同步改为 `RND-232-dev-prompt.md`。内容未改写，仅路径与交叉引用更新。
>
> **2026-07-31 更新**：已完成工单的提示词/QA 报告统一沉降到 `tasks/archive/`，本文件内指向历史提示词的链接已同步改为 `archive/RND-<n>-...`。

## 0. 任务与来源
- Linear：**RND-232「开源准备：去标识化与发布前梳理」**（epic），优先级 P2，子任务 RND-233/234/236/237/238/239。
- 目标：发布前去标识化 + 清理，使仓库可安全开源。
- 非目标（严禁）：不重写架构、不加功能（**例外**：RND-239 营销站为 Haisu 2026-07-26 新增方向，属前端展示，不改后端业务行为）、不动 RND-212 重构 epic、不改变业务行为。

## 1. 决策记录（已与 Haisu 确认）
- **RND-238（改写历史 + force push）**：**已决策不执行**（保留 git 历史原样，历史中的 `crowntime.cn` 引用不清除）。理由：仅基础设施名非密钥，force push 代价过大。Agent 不做历史改写、不碰 work tree。→ 仅做工作树脱敏（RND-233），历史残留保持不变。
- **RND-239（构建营销网站）**：**方向变更（2026-07-26 17:19）**——取消「迁私有仓库」，改为**在项目内构建前端营销网站**（介绍系统 + 含官网）；`company_homepage` 静态页保留在项目内。详见 `RND-239-marketing-site-plan.md`（草案）。
- **RND-233（域名脱敏）**：策略 = **读环境变量**。部署脚本从 `.env` 读域名；`.env.example` 用占位符；文档/测试用 `example.com`。不要全文硬编码替换（会让线上部署失效）。
- **方向变更（2026-07-26 17:19）RND-239**：取消「迁私有仓库」，改为在项目内构建前端营销网站（含官网），`company_homepage` 保留项目内。RND-234 由「泄露应急」重定性为「品牌化决策」。详见 `RND-239-marketing-site-plan.md`。

## 2. 子任务分派与状态
| 子任务 | 动作 | 交付物 | 状态 |
|---|---|---|---|
| RND-233 | 域名去标识化（env 策略） | `archive/RND-233-dev-prompt.md`（已归档，RND-233 已 Done） | 待执行 |
| RND-234 | company_homepage 真实身份 | 重定性：营销站公开，身份转品牌化决策；随 RND-239 收口 | 由 RND-239 收口 |
| RND-236 | 清理 `.qoder` wiki | `archive/RND-236-dev-prompt.md`（已归档，RND-236 已 Done） | 待执行 |
| RND-237 | LICENSE / README / .gitignore / CLA | `archive/RND-237-dev-prompt.md`（已归档，RND-237 已 Done） | **AGPL-3.0 已确认（2026-07-29 反转 MIT）·已完成** |
| RND-238 | 改写历史 + force push | 已决策不执行（保留历史） | 关闭 |
| RND-239 | 构建前端营销网站（含官网，保留项目内） | `RND-239-marketing-site-plan.md` / `RND-239-dev-prompt.md` / `RND-239-qa-prompt.md` | 规划提示词已交付·待实现 |

## 3. 工作树现状盘点（执行前只读扫描）
- `crowntime.cn`：30 个文件（清单见 `archive/RND-233-dev-prompt.md`（已归档，RND-233 已 Done） §1）。
- `.qoder/repowiki`：193 个已跟踪文件，未被 `.gitignore`，属 IDE 自动生成 wiki，冗余。
- 无 `LICENSE` 文件；`.gitignore` 存在需复核；根 `README.md` 存在需复核。
- `static_site/company_homepage`：3 文件，暴露真实公司身份（深圳康冠时代科技有限公司、ICP 粤ICP备2026090201号、公安网安备44030002014504号、真实地址/电话/邮箱）。**方向变更**：该站保留在项目内，将并入新营销网站；身份问题转为「品牌化」决策（见 RND-234 重定性）。
- 工作区另有 3 个未跟踪 ADR 文档（`backend/tests/test_architecture_boundary.py`、`docs/adr/*`，属重构工作），**不在本 epic，勿动**。

## 4. 建议执行顺序
1. **RND-236**（删 wiki，最简单、零风险）
2. **RND-237**（LICENSE + .gitignore + README 复核）
3. **RND-233**（域名 env 化，涉及部署脚本，需谨慎验证）
4. **RND-239 构建营销网站**（含官网；RND-234 转为品牌化决策）
5. **RND-238** 由 Haisu 自行决定（filter-repo + force push）

## 5. 通用硬性约束
- Agent 不 commit / push；提交由 Haisu 操作。
- 不引入新依赖；不改变业务行为；不动 RND-212 重构 epic。
- 每步完成后 `grep -rn "crowntime" . --exclude-dir=.git --exclude-dir=.qoder` 应无结果（历史残留属 RND-238）。
- 参考 `DEV_AGENT_RULES.md`。
