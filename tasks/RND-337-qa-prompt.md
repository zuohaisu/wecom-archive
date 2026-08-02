[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-337 的 10 条 AC，证明持久化消息可见性快照完整、隔离、安全且不会把部分扫描误报为健康。

# RND-337 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-337 的独立验收 agent，任务从你读到这句话开始。

- **不要**问“你希望我做什么”“需要我现在开始吗”——答案已经是“是”。
- **不要**先输出计划等待回复；直接从 AC-1 开始逐条核对。
- 只读检查无需等待许可；但 migration 往返只能使用明确的一次性 local/test DB，不能猜环境。
- 唯一允许不产出 PASS/FAIL 的情况是按下方规则写出 `BLOCKED` verdict；不要用提问代替 verdict。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-337「持久化消息可见性检查快照与归档健康 API」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-337/持久化消息可见性检查快照与归档健康-api
- 风险等级：R2｜类型：后端、迁移与安全契约独立验收
- 后继关系：RND-338、RND-339 只有在本票最终 QA PASS 后才能开始。

## [Goal check]
本工作推进「独立验收（QA）」阶段，证据 = AC-1 ~ AC-10 每条都有源码、测试与运行命令证据，并产出 PASS/FAIL/BLOCKED verdict。

## 你的角色与权限
- 你是独立验收 agent，只判断 RND-337，不补实现。
- 可以读仓库、运行只读测试/静态检查；仅可在明确一次性 local/test DB 做 migration upgrade/downgrade。
- 不修改代码、测试、文档、工单，不 commit/push/建分支，不对生产/共享数据库执行任何写操作。
- 唯一允许写入的是规定的 `tasks/RND-337-qa-verdict.json`。

## 输入
- `tasks/RND-337-dev-prompt.md` 的 AC-1 ~ AC-10。
- 待验收工作树 diff，重点：run model/migration、classifier 边界扩展、service/schema/router/runner、main 最小注册、聚焦 tests 与 HTTP contract。
- 现有 `backend/app/routers/reachability_audit.py` 和旧 API tests，作为兼容基线。

## Preflight（先做，两项都做完再进 AC-1）

### P-1 现场采集，不要相信任何文档里的快照

本提示词**不记录**工作树状态与 Alembic head。自己采集，并与开发 agent 报告的
开工 baseline 对照：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
(cd backend && .venv/bin/python -m alembic heads)
```

- 用户/他票改动按文件所有权隔离（`tasks/WAVE-ownership.md`），不得修改或回滚。
- 本票**归因**文件超出 dev prompt 拥有清单即 `SCOPE_VIOLATION`。
- 本票产生 commit/push/branch 即 FAIL；采集前就存在的 ahead commit 不可误归因。
- RND-335 可能并发修改 `backend/app/audit.py` 与多个 `backend/app/routers/*.py`
  （§2 两票可并发）。它们有 diff 是预期的，**不得**记在 RND-337 账上。
- AC-1a 的判据是「`down_revision` 指向**你采集到的**前一 head」，不是任何写死的
  revision 号。若 `alembic heads` 输出多于一个 head → 记 blocker 并查明是不是本票造成的。

### P-2 一次性数据库（决定 AC-1d 怎么判）

```bash
echo "DATABASE_URL=[${DATABASE_URL}]"
```

migration 往返需要真实 PostgreSQL（模型用 `JSONB`，SQLite 顶不上）。

- **为空，或无法证明是一次性 local/test 库** → **禁止**执行任何 `alembic upgrade/downgrade`。
  AC-1d 记为未验证：verdict notes 写 `HUMAN_MIGRATION_REVIEW_PENDING`，
  并按 R2 风险判 `BLOCKED`。不需要真库的 AC-1e（模型↔migration 静态对齐）
  照常判定。**不得**把「没跑」当作 PASS，也**不得**因环境缺口给开发判 FAIL。
- **非空且确认是一次性库** → 执行往返，正常判 AC-1d。
- 无论如何：不对共享/生产库做迁移，不运行会触发真实消息扫描的命令。

## 验收方法（证据优先）

**判定单位是子 AC，不是大项。** `tasks/RND-337-dev-prompt.md` 的「验收标准」已把
10 个大项拆成 AC-1a ~ AC-10c 的原子断言。**本节不重述断言内容**（重述必然与 dev
prompt 漂移）——去读 dev prompt 的原文，本节只规定**每类断言需要什么形态的证据**
和**怎么判**。

evidence 里每一条子 AC 独立成行：`AC-3d | test_reachability_checks.py::test_2001_candidates_each_classified_once | PASS`。

### 通用证据形态

| 断言形态 | 可接受的证据 | 不可接受 |
|---|---|---|
| 「每条候选恰好一次」 | 分类调用的实际计数 / 被检查 ID 的集合断言 | `has_more=false` 的 mock；HTTP 200 |
| 「候选集不漂移」 | 第一页后真实插入数据，再断言结果集 | 只断言 `scope_to` 被写进了表 |
| 「永不 healthy」 | 参数化遍历全部 partial/exception 路径的断言 | 抽查一两个分支 |
| 「不在 request thread 全扫」 | scanner 调用计数或耗时断言 | 「响应很快」 |
| 「同租户同一 run」 | 数据库/事务层并发测试 | 模块级全局变量、进程内线程锁 |
| 「递归不含 X」 | `docs/agent-data-minimization.md` §5 的递归遍历断言 | 只查顶层 key；注释 |
| 「tenant scoped」 | A/B 双租户 seed 后的实际读写隔离测试 | 只看 router 出口过滤 |
| 「route +2」 | 采集到的当前真实 `route_count` 与改后值之差 | 写死数字 |
| 「migration 往返」 | P-2 一次性 DB 上的三步实际 exit code | 「代码看起来是对的」 |

### 逐大项的判定重点

- **AC-1**：AC-1a 用**你采集到的** head 判，不用文档里的 revision 号。分叉、
  不可逆副作用、对共享/生产库执行 = blocker FAIL。AC-1d 按 Preflight P-2 处理。
- **AC-2**：AC-2b/2c 必须真的插入数据再断言。「只记录了 `scope_to` 时间戳但回填消息
  仍进入本 run」或「跑完才推断边界」= FAIL。
- **AC-3**：AC-3d（2001 条跨页）是本项核心。用首 2000 条推断整体 = **blocker FAIL**。
  AC-3g 若为了让新代码通过而削弱或删除旧 helper 的既有断言 = FAIL。
- **AC-4**：AC-4f 是本票的灵魂。任何 partial / unknown / exception 被 `healthy` 或
  `no_data` 掩盖 = **blocker FAIL**。另查：代码中所有 fallback、except 分支、
  「无 run」分支都不得返回 `healthy`——`healthy` 是结论，不是默认值。
- **AC-5**：AC-5c 只用模块全局变量或线程锁、或测试没真正并发 = FAIL。
- **AC-6**：AC-6c 伪造 `last_checked_at` = FAIL。AC-6d 用哨兵递归断言。
- **AC-7**：AC-7a 若 CLI 参数里出现 tenant id = FAIL（进程参数是受管面）。
- **AC-8**：AC-8d 只在 router 过滤而 service 的 update 未带 tenant = **blocker FAIL**。
- **AC-9**：AC-9b 若 `test_http_contract.py` 删除或弱化了既有 expected 条目 = FAIL；
  只允许追加本票的两条。AC-9e 复制 classifier = FAIL。
- **AC-10**：共享工作树里他票的失败先按 P-1 隔离归因再写 notes。

## 本项目专属检查（必查）
1. 新 route 位于 `app/routers`；`main.py` 只有 composition wiring，无 SQL/BackgroundTasks 业务实现/内联 schema。
2. 分类只能来自 `app.reachability_audit`；搜索新 service 中是否重新定义 status/reason 分支。复制 classifier 即 FAIL。
3. `test_http_contract.py` 只能做两个新 route 所需的 count/snapshot/auth/shape 最小改动，不得删除既有 expected 项。
4. 若 router 使用 `get_current_user`，`test_rnd280_rbac_scaffold.py` 不应改；若实现自行引入 `require_role` 却未获权限决策，判 scope/requirement defect。
5. 日志、response、DB columns、CLI args 四个受管面均适用 `docs/agent-data-minimization.md` §2；
   测试必须用该文件 §5 的哨兵组递归断言缺失。本票唯一授权的例外是 public run id。
6. `healthy` 是结论而不是默认：检查代码中任何 fallback、exception handler、无 run 分支都不能返回 healthy。

## 附加检查（Scope / Security）
- 改 UI/i18n/systemd/archive worker/findings/notification/auto-repair → `SCOPE_VIOLATION`。
- 新增外部队列/Redis/Celery/云服务或生产迁移 → `SCOPE_VIOLATION` / `SECURITY_VIOLATION`。
- request 接受 tenant id、跨租户 public id 可枚举、raw error/identifier/content 外泄 → blocker `SECURITY_VIOLATION`。
- 仅以绿色 tests 证明安全但没有真实 JSON/DB/log/CLI 参数检查 → `INSUFFICIENT_TEST_COVERAGE`。

## 验证命令（只读，可运行）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_reachability_audit.py backend/tests/test_reachability_checks.py -q
test ! -f backend/tests/test_reachability_check_cli.py || .venv/bin/python -m pytest backend/tests/test_reachability_check_cli.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py backend/tests/test_verify_alembic_head.py -q
(cd backend && .venv/bin/python -m alembic heads)
git diff --check
git diff --name-only
git status --short --branch
git log origin/main..HEAD
```

仅在 Preflight P-2 确认为一次性 DB 时追加执行：
```bash
(cd backend && .venv/bin/python -m alembic upgrade head)
(cd backend && .venv/bin/python -m alembic downgrade -1)
(cd backend && .venv/bin/python -m alembic upgrade head)
```

若无法证明 DB 是一次性 local/test，禁止执行 migration 写命令；不依赖真库的 schema 断言测试可继续。
按 Preflight P-2 处理 AC-1d：notes 写 `HUMAN_MIGRATION_REVIEW_PENDING` 并判 `BLOCKED`，
既不静默标 PASS，也不因环境缺口给开发判 FAIL。

## 产出
写入 `tasks/RND-337-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- **全部子 AC** PASS、无 blocker/major、R2 migration 证据完整 → `verdict: PASS`，`recommended_next_state: PASS`。
- 任一子 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`；findings **按子 AC 编号定位**（如 `AC-3d`），只描述最小修复，不代写。
- Preflight P-2 未满足（AC-1d 无法验证）→ `verdict: BLOCKED`，notes 写 `HUMAN_MIGRATION_REVIEW_PENDING`；其余子 AC 判定照常写进 evidence。
- 迁移/权限/状态语义需产品决策或两轮仍失败 → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。
- evidence 必须逐子 AC 成行，并列出实际候选数/页数、六态测试、route count 前后值、采集到的 Alembic head 和命令 exit code。不得用「AC-3 全部通过」这类聚合表述。

## 禁止事项
- 除 verdict JSON 外不修改任何文件，不补实现或测试。
- 不对共享/生产数据库做 migration，不运行会触发真实消息扫描的命令。
- 不以单页、mocked `has_more=false` 或 HTTP 200 代替完整性证明。
- 没有 2001+、partial failure、concurrency、tenant isolation、sensitive sentinel 中任一测试，直接记 `INSUFFICIENT_TEST_COVERAGE`。
