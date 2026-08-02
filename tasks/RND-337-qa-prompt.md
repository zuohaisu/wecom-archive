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

## 共享工作树归因（先做）

提示词撰写时基线为 `## main...origin/main` 且 clean。验收开始时重新记录 `git status --short --branch`、`git diff --name-only`、`git log origin/main..HEAD`。

- 后续出现的用户/他票改动先按 baseline 和文件所有权隔离，不得修改或回滚。
- 本票归因文件超出 dev prompt 拥有清单即 `SCOPE_VIOLATION`。
- 本票产生 commit/push/branch 即 FAIL；用户原有 ahead commit 不可误归因。

## 验收方法（证据优先）

### AC-1 — 迁移与模型
- 证据：唯一 Alembic head；新 revision 的 `down_revision` 指向验收时前一 head；upgrade/downgrade 仅操作 run 表及本票约束/索引；模型与 migration 对齐；没有 message content/payload/raw identity 字段。
- 必测：tenant FK/index、public id 唯一、状态/来源枚举、非负计数、timestamps、reason JSON；local/test upgrade → downgrade → upgrade。
- 判定：结构与往返全通过 = PASS；分叉、不可逆副作用、生产执行或 schema 漂移 = blocker FAIL。

### AC-2 — 冻结范围
- 证据：run 创建时原子记录 scope from/to、max message id、algorithm version；测试在第一页后插入更大 ID/历史消息，结果仍只覆盖冻结候选；默认 7 天边界确定且无本地时区歧义。
- 判定：候选集不随运行漂移 = PASS；只记录时间但回填消息仍进入本 run，或结束后才推断边界 = FAIL。

### AC-3 — 完整分页
- 证据：0、1、2000、2001、多页 fixtures；每个冻结候选恰好分类一次；`checked == matching` 且 reason 总和一致；旧 helper 不传新参数时快照/代表性响应不变。
- 判定：无漏扫、重扫或页上限截断 = PASS；用首 2000 条推断整体 = blocker FAIL。

### AC-4 — 状态真实性
- 证据：状态转换表与参数化 tests 覆盖 healthy/attention/checking/no_data/incomplete/error；页中断、exception、count mismatch、stale checking 都显式测试；断言这些路径永不 healthy。
- 判定：只有完整且 zero unreachable 才 healthy = PASS；任何 partial/unknown 被 healthy/no_data 掩盖 = blocker FAIL。

### AC-5 — POST 异步与幂等
- 证据：API test 证明 request 返回前不执行全量 scanner；响应为 accepted/checking 快照；并发事务测试证明同租户同一 active public id、不同租户独立、完成后可建新 run。
- 判定：数据库/事务级原子性可重复证明 = PASS；只用模块全局变量/线程锁或测试未真正覆盖并发 = FAIL。

### AC-6 — GET latest 契约
- 证据：六态、complete、scope/count/reasons/timestamps/version/safe error shape tests；有/无历史 run 均覆盖；递归检查 response keys/values 不含 content、payload、structured_content、sender、recipient、room、raw msgid、path、secret、traceback/原始异常。
- 判定：足够支持 UI 且最小安全 = PASS；伪造 last_checked_at 或泄漏内部数据 = FAIL。

### AC-7 — runner 恢复
- 证据：CLI/runner 仅接受 public run id；unknown/completed/stale/跨租户场景；同 run 重入不会双加计数；异常后 transaction 最终状态可读且不永久 checking。
- 判定：幂等、fail closed、无 tenant id/process-arg 泄漏 = PASS。

### AC-8 — 认证与租户隔离
- 证据：两个 route 未登录失败；tenant A/B 同时 seed 后 GET/POST/runner 全路径隔离；request body/query 无 tenant selector；猜 public id 的响应不泄露他租户存在性。
- 判定：所有 ORM 读写显式 tenant scoped、运行定位安全 = PASS；只在 router 过滤但 service update 未带 tenant = blocker FAIL。

### AC-9 — 兼容、架构与 route contract
- 证据：旧 `/api/admin/reachability-audit` path/query/model/样例 tests 通过；新增 route 恰好两个；`main.py` diff 仅 router import/include；service 不反向 import router，router 不 import main。
- 判定：route count 相对基线恰好 +2、架构硬闸全绿 = PASS；为通过快照删除/弱化旧断言 = FAIL。

### AC-10 — 回归与范围
- 证据：聚焦测试、`make verify`、architecture、HTTP contract、migration head tests exit 0；归因 diff 只在拥有清单；前端、systemd、archive worker、旧 router 无改动。
- 判定：全部成立 = PASS；任一归因失败或越界 = FAIL。

## 本项目专属检查（必查）
1. 新 route 位于 `app/routers`；`main.py` 只有 composition wiring，无 SQL/BackgroundTasks 业务实现/内联 schema。
2. 分类只能来自 `app.reachability_audit`；搜索新 service 中是否重新定义 status/reason 分支。复制 classifier 即 FAIL。
3. `test_http_contract.py` 只能做两个新 route 所需的 count/snapshot/auth/shape 最小改动，不得删除既有 expected 项。
4. 若 router 使用 `get_current_user`，`test_rnd280_rbac_scaffold.py` 不应改；若实现自行引入 `require_role` 却未获权限决策，判 scope/requirement defect。
5. 日志、response、DB columns、CLI args 均不得出现消息内容/原始身份/secret/traceback；测试应使用敏感哨兵值递归断言缺失。
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

仅在明确 disposable DB URL 下追加执行：
```bash
(cd backend && .venv/bin/python -m alembic upgrade head)
(cd backend && .venv/bin/python -m alembic downgrade -1)
(cd backend && .venv/bin/python -m alembic upgrade head)
```

若无法证明 DB 是一次性 local/test，禁止执行 migration 写命令；自动化 migration test 可继续。缺少真实迁移往返证据时不得把该点静默标 PASS，在 verdict notes 说明 `HUMAN_MIGRATION_REVIEW_PENDING` 或按风险判 BLOCKED。

## 产出
写入 `tasks/RND-337-qa-verdict.json`，schema 见 `tasks/_templates/qa-verdict.schema.json`。

- AC 全 PASS、无 blocker/major、R2 migration 证据完整 → `verdict: PASS`，`recommended_next_state: PASS`。
- 任一 AC FAIL → `verdict: FAIL`，`recommended_next_state: FIXING`；findings 只描述最小修复，不代写。
- 迁移/权限/状态语义需产品决策或两轮仍失败 → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`。
- verdict evidence 必须列出实际候选数/页数、六态测试、route count 前后值、Alembic head 和命令 exit code。

## 禁止事项
- 除 verdict JSON 外不修改任何文件，不补实现或测试。
- 不对共享/生产数据库做 migration，不运行会触发真实消息扫描的命令。
- 不以单页、mocked `has_more=false` 或 HTTP 200 代替完整性证明。
- 没有 2001+、partial failure、concurrency、tenant isolation、sensitive sentinel 中任一测试，直接记 `INSUFFICIENT_TEST_COVERAGE`。
