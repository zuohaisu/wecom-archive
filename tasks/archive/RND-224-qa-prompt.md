# RND-224 测试 / QA 验收提示词（独立验收智能体）

> 用途：粘贴给**独立**测试 / QA 智能体（按 `DEV_AGENT_RULES.md` 的 Codex 验收角色），对**已实现的** RND-224 做独立验收。
> 与开发提示词（`.workbuddy/prompts/rnd-224-execution-prompt.md`）解耦：本提示词不指导如何实现，只规定「怎么判定算做完、怎么证明没做完」。开发 agent 完成并自测通过后，由本 agent 独立复验。
> 权威依据：Linear 工单 **RND-224** 验收标准（CI 能检测关键反向依赖和 composition-root 违规；规则允许简单 CRUD 而不过度抽象；文档与当前实现一致；完整测试通过）+ 代码硬约束。

---

## 0. 验收依据

- **Linear RND-224 验收（必须全过）**：
  1. CI 能检测**关键反向依赖**（service→router、router→main）和 **composition-root 违规**（main 新增 route / SQLAlchemy query / inline HTML/CSS/JS）。
  2. 规则**允许简单 CRUD 而不过度抽象**（无机械行数阈值、不强制每端点拆 service）。
  3. **文档与当前实现一致**（消息 / 媒体 / decrypted payload / worker 四域 source-of-truth）。
  4. **完整测试通过**（`make verify` 全绿）。
- **非目标不可被破坏**：不改 CI 配置、不加第三方依赖、不做全仓库文档重写、不改 `app/` 运行时行为、无 migration。
- **本票是「codify + 文档」型任务**，几乎不动运行时代码——验收重点在「护栏真的能挡违规」「文档真的对齐了实现」「没有越界改 CI/依赖/代码」。

---

## 1. 前置检查（先确认环境，再验收）

1. 代码已合入待测 `main`，且 **RND-218～223 均已在 `main`**（RND-224 是链收口票，依赖最终架构）。`git log --oneline | grep -Ei "RND-21[89]|RND-22[0-3]"` 核对；若链未完成，停下并在 Linear 评论说明，不验收。
2. 开发 agent 已通过 `make verify`。若未通过，本 agent 先复跑一遍 `make verify` 作为基线。
3. 拿到开发评论中的：护栏测试文件名、§0.6 规则文档落点（A/B/C 选择）、四域文档差异清单、route_count 数字。缺失则本 agent 自行核。
4. 本地可跑 `cd backend && python -m pytest`；开发服务器如需则前台启动（不后台化、不加 `&`）。

---

## 2. 验收清单（逐条 PASS / FAIL + 证据）

> 每条给出「判定方法 + 期望 + 实测」。FAIL 必须附最小复现步骤。整体结论见 §5。

### 护栏测试（工单验收 1）

- **C1 护栏文件就位且纯标准库**：`backend/tests/test_architecture_boundary.py` 存在；`grep` 确认**未 import** `import_linter`/`grimp`/`pytest_arch` 等第三方；只用 `ast`/`pathlib` 等标准库。
- **C2 现状零违规（真实断言全绿）**：`cd backend && python -m pytest tests/test_architecture_boundary.py -q` 全绿；其中「真实仓库」断言证明当前 `app/` 无 service→router、无 router→main、composition root 纯净。
- **C3 反向依赖可检出（护栏自证 · 正例）**：审查测试代码，确认有**合成违规样本**（喂入「service 模块 import `app.routers.x`」「router import `app.main`」的源码字符串/临时目录）且断言检查函数**返回该违规**。可另做一次性实验佐证：临时在**临时目录/内存字符串**（**绝不改真实 `app/` 源码**）构造一条 service→router import，喂给检查函数确认被标记。
- **C4 composition-root 违规可检出（护栏自证 · 正例）**：确认有合成样本覆盖「main 内新增 `@app.get` 业务路由」「main 内 `db.query(...)`/`select(...)`」「main 内含 `<script>`/`<html>` 字面量」三类，检查函数均能检出。
- **C5 不误报（护栏自证 · 反例）**：确认有**合法样本**（router→service、service→db、main→routers、健康探针路由、router 内简单 `db.query`）喂入后检查函数**返回空违规**。特别确认「router 内直接写查询/简单 CRUD」不被判违规。
- **C6 健康探针白名单生效**：composition-root 检查对 `/health`、`/health/live`、`/health/ready` 放行（显式白名单常量），对其他 `@app.*` 业务路由则判违规。
- **C7 无行数阈值（工单验收 2）**：`grep` 测试代码确认**没有**任何「行数 / len(lines) / 函数长度 / 路由体长度」类阈值断言；规则只约束依赖方向与 composition-root 纯度。

### CI 集成（工单验收 1「由 CI 检查」）

- **C8 自动进 CI 门禁、且未改 CI 配置**：确认 `test_architecture_boundary.py` 位于 `backend/tests/`，会被 `make test` 与 CI「Offline / SQLite-compatible tests」步骤（`pytest tests/ -q`）自动收录；`git diff` 确认 **`.github/workflows/deploy.yml` 与 `deploy/`、`scripts/deploy_server.sh` 零改动**。

### 规则文档（工单范围「AGENTS.md architecture rules」）

- **C9 规则文档落地且与测试一致**：`DEV_AGENT_RULES.md` 有「Architecture Boundaries」节（或 §0.6 选定落点）；`docs/AGENTS.md` 有摘要 + 指向；（若做了）根 `AGENTS.md` 为入口指路。逐条比对：文档表述的分层/依赖禁令/composition-root 纯度/「允许简单 CRUD、按功能内聚拆分、不看行数」与 `test_architecture_boundary.py` 语义**完全一致**，无更严/更松。

### Source-of-truth 文档对齐（工单验收 3）

- **C10 消息类型文档一致**：`docs/DATA_MODEL.md` 的 `msgtype` 描述与 `app/message_type_registry.py`/`app/structured_message_parser.py` 实际支持集合/解析行为一致（抽查若干类型交叉核对）。
- **C11 媒体文档一致**：`docs/ops/media_storage_ops.md` + `docs/wecom_archive_media_download_runbook.md` 的模块路径与流程（授权先于 provider、双 schema、Qiniu/local 混合、错误分类真源）指向 RND-221 后的 `app/services/media_access.py`/`app/routers/media.py`/`app/media_storage.py`，无残留「media 逻辑在 conversations.py」等旧描述。
- **C12 decrypted payload 文档一致**：`docs/DATA_MODEL.md`（`decrypted_payload`/`decrypt_status`）+ `docs/ARCHITECTURE.md` 管线描述与当前实现一致；与 `docs/adr/0001` 无冲突（或已交叉引用/修正）。
- **C13 worker 文档一致**：`docs/wecom_archive_worker_runbook.md` + `docs/ai/known-pitfalls.md` 的 worker 入口脚本路径、tenant/corp scope 契约、sync→decrypt 顺序与 RND-222 后实现一致。
- **C14 未越界重写文档**：`git diff` 确认仅动上述四域相关文档 + 规则/ADR 文件，未大面积润色/重排无关文档（SSL/部署/PRD/research 等），符合「不做全仓库重写」。

### ADR（工单范围「必要 ADR 模板」）

- **C15 ADR 模板 + 边界 ADR 就位**：`docs/adr/0000-template.md`（含 Status/Context/Decision/Consequences/Alternatives/Links）存在且风格对齐 `0001`；`docs/adr/0002-module-boundaries-and-composition-root.md` 记录了边界决策、强制方式、备选方案（含「弃用 import-linter 以免加依赖」）与链接。

### 回归 / 越界（工单验收 4 + 非目标）

- **C16 route_count 不变**：`test_http_contract.py::test_router_count` == 33（或链合入后的最新期望值）；本票未加/删路由。
- **C17 无新依赖 / 无 migration / 无运行时代码变更**：`git diff` 确认未改 `pyproject.toml`/`requirements*`、未生成 migration、未改 `app/` 运行时逻辑（仅新增测试 + 文档）。
- **C18 `make verify` 全绿**（lint-diff + typecheck + build + 全量 pytest），无新 lint 告警。

---

## 3. 测试方法

- **护栏**：`cd backend && python -m pytest tests/test_architecture_boundary.py -q -v`（看清正反自证用例名与结果）。
- **契约 + 收口**：`cd backend && python -m pytest tests/test_http_contract.py -q`；最后 `make verify`。
- **代码/文档审查项（C1/C3–C9/C10–C15/C17）**：读 `git diff` 与 `test_architecture_boundary.py`、`DEV_AGENT_RULES.md`、`docs/AGENTS.md`、四域文档、`docs/adr/0000`/`0002` 逐项核对。
- **护栏可检出的独立佐证（C3/C4）**：如需超出开发自证，只在**临时目录 / 内存字符串**构造违规样本喂检查函数，**绝不修改真实 `app/` 源码**；验完即弃。
- **文档对齐佐证（C10–C13）**：以**代码为准**，抽查文档中具体断言（类型集合、模块路径、状态机、worker 顺序）与代码逐点对照。

---

## 4. 硬性约束（验收 agent 自身也要守）

- 不修改任何实现代码 / 测试 / 文档；只**读**、**断言**、**跑测试**。构造违规样本仅限临时目录/内存，绝不落到真实源码。
- 不改 CI 配置、不加依赖。
- 不自行 `git commit` / `push`；只输出验收结论与证据。
- 不引入后台进程；命令前台运行。

---

## 5. 输出格式（必须结构化）

```
## RND-224 验收报告
整体结论：PASS / FAIL / BLOCKED
环境：分支 <x> · RND-218~223 是否全在 main：是/否 · make verify：通过/失败
route 总数：<n>（预期 33，来源：开发评论/本 agent 自测）
规则文档落点：A(DEV_AGENT_RULES) / B(docs/AGENTS) / C(root AGENTS) — 实际：<...>

| 编号 | 验收点 | 结果 | 证据（实测/命令/代码位置） |
|------|--------|------|---------------------------|
| C1   | 护栏纯标准库 | PASS | grep 无第三方 arch 依赖 |
| C2   | 现状零违规 | PASS | test_architecture_boundary 全绿 |
| C3   | 反向依赖可检出 | PASS | 自证正例断言命中 |
| C4   | composition-root 违规可检出 | PASS | 三类合成样本命中 |
| C5   | 不误报 | PASS | 合法样本返回空违规 |
| C6   | 健康探针白名单 | PASS | ... |
| C7   | 无行数阈值 | PASS | grep 无行数断言 |
| C8   | 自动进 CI 且未改 CI 配置 | PASS | git diff 无 .github 改动 |
| C9   | 规则文档与测试一致 | PASS | 逐条比对 |
| C10  | 消息类型文档一致 | PASS | 抽查交叉核对 |
| C11  | 媒体文档一致 | PASS | 路径/流程对齐 RND-221 后 |
| C12  | decrypted payload 文档一致 | PASS | ... |
| C13  | worker 文档一致 | PASS | ... |
| C14  | 未越界重写文档 | PASS | git diff 仅四域+规则/ADR |
| C15  | ADR 模板+边界 ADR | PASS | 0000/0002 就位 |
| C16  | route_count 不变 | PASS | test_router_count==33 |
| C17  | 无依赖/migration/运行时代码变更 | PASS | git diff 确认 |
| C18  | make verify 全绿 | PASS | ... |

### 失败项 / 阻塞项
- <逐条：现象 + 最小复现 + 影响范围>

### 边界与已知限制确认
- 非目标（改 CI / 加依赖 / 全仓库重写 / 改运行时 / migration）确认未触碰 → 视为 PASS 非缺陷。
- 其他观察到的限制：<…>

### 结论与建议
- 可合并 / 需返工（列出必须修的项）/ 阻塞（缺 RND-218~223 之一）。
```

- 若某条无论如何无法复现，如实标注 **NOT REPRODUCIBLE** 而非判 FAIL；若实现确有问题，判 FAIL 并给出可复现证据。
- 最终把该报告作为 Linear RND-224 评论贴出（状态保持 In Progress / Todo，交还 Haisu 决策合并）。