[Goal check] This work advances 开发（Development） by 将一次性技术分页审计升级为租户级、可持久化、可复用的消息可见性检查快照，并以完整性证据阻止部分扫描被误报为健康。

# RND-337 开发提示词（Developer Prompt）

> 开始前必须读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/reachability_audit.py` 和现有 reachability tests。RND-337 涉及数据库迁移与后台执行，风险等级 R2；未获得 Haisu 对迁移/执行方案的人工确认前不得改产品代码。

## ⚡ 立即执行，不要询问意图

你现在收到的是已经批准、待执行的任务指令。你就是 RND-337 开发 agent。先执行 Preflight；闸未满足时输出 `BLOCKED_NEEDS_HUMAN` 和所缺确认，不改产品代码。闸满足后直接实现，不要先输出计划等待确认。

---

## Preflight（开工第一步，先做完再碰代码）

### P-1 现场采集工作树与 Alembic 基线 —— 不要相信任何文档里的快照

本提示词**不记录**工作树状态与 Alembic head：提示词是静态的，两者都是易变的。
你自己采集：

```bash
git status --short --branch
git diff --name-only
git log origin/main..HEAD
(cd backend && .venv/bin/python -m alembic heads)
ls backend/alembic/versions/ | sort | tail -5
```

归因规则：

1. 不在下方「本工单拥有的文件」清单里的一切改动 → 标为「非本票」，写进 QA Summary
   的 notes，**不修改、不回滚、不覆盖、不算作本票交付**。
2. 采集**之后**新增的 commit / push / branch 才归因于你（你不该产生任何一个）。
3. 你的新 migration 的 `down_revision` 必须指向**你采集到的实际 head**，不是本提示词
   正文里提到的任何 revision 号。若 `alembic heads` 输出多于一个 head，
   立即 `BLOCKED_NEEDS_HUMAN`（已有分叉，不是你造成的，也不该由你合并）。
4. RND-335 可能正在并发修改 `backend/app/audit.py`、`backend/app/routers/*.py`
   （见 `tasks/WAVE-ownership.md` §2，两票可并发且拥有文件不相交）。看到它们有
   diff 是预期的，不是你的越界。

### P-2 一次性数据库（migration 往返需要，硬前置）

AC-1 要求 `upgrade → downgrade → upgrade` 往返。这需要一个**真实 PostgreSQL**：
模型用 `JSONB`，SQLite 顶不上；且 `alembic` 命令读 `DATABASE_URL`。

```bash
echo "DATABASE_URL=[${DATABASE_URL}]"
```

- **为空** → 本仓没有 docker-compose，你无法自备 PG。输出 `BLOCKED_NEEDS_HUMAN`：
  「AC-1 的 migration 往返需要一次性 PostgreSQL 的 `DATABASE_URL`，请 Haisu 提供」。
  模型/schema 的静态断言测试可以继续写（那部分不需要真库），但**不得**声称往返已验证。
- **非空** → 先确认它是一次性 local/test 库：记录主机名与库名（不要记录密码）。
  只要无法证明它是一次性的（例如指向共享开发库、生产库、或你无法判断），
  **禁止**执行任何 `alembic upgrade/downgrade`，按上一条上报。
- 三条铁律：不对共享/生产库执行迁移；不为了让往返跑通而改 CI 配置；
  不用 SQLite 冒充生产形状。

### P-3 R2 人工闸

Haisu 必须已经确认 run schema、后台执行方式与迁移方案。未确认 → `BLOCKED_NEEDS_HUMAN`，
零产品代码改动。

---

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`，project `365企微会话存档`）
- 工单：RND-337「持久化消息可见性检查快照与归档健康 API」
- Linear URL：https://linear.app/xyzhs1897/issue/RND-337/持久化消息可见性检查快照与归档健康-api
- 优先级：High｜风险等级：**R2**
- 所属波次：消息可见性闭环 · 持久化诊断基座
- 当前关系：blocks RND-338、RND-339。

## [Goal check]
本工作推进「开发实现」阶段，证据 = 10 条 AC 覆盖冻结扫描范围、完整分页、持久化状态、租户隔离、并发幂等、安全错误和两个新 API，且旧技术接口保持兼容。

## 背景与项目现状（先对齐，避免重复造轮子 / 跑偏）

目标客户只有 1–5 个坐席。他们需要的是“消息是否完整可见”的可信结论，而不是每次打开页面临时扫一页技术日志。

- `backend/app/reachability_audit.py:78-80,182-271` 是现有唯一分类事实源：只审计 `decrypt_status == "success"` 的消息，区分 direct/group 可达与 missing recipient/room/sender/membership/other。
- `backend/app/reachability_audit.py:182-216,382-390` 的 `build_message_reachability_report()` 当前默认 500、最大 2000，返回 `scanned_count`、`matching_total`、`has_more`、reason counts 和可选 samples。它是分页报告，不是持久化全量快照。
- `backend/app/routers/reachability_audit.py:65-85` 暴露现有 `GET /api/admin/reachability-audit`；必须保持路径、参数、响应兼容，继续服务技术排障。
- `backend/app/web/templates/diagnostics.html` 与 `diagnostics.js` 目前直接消费旧接口并在浏览器计算比例；UI 重构属于 RND-338，本票不改前端。
- `backend/app/main.py:111-136` 仅允许作为 composition root 注册 router；新业务路由必须放 `backend/app/routers/`。
- `backend/tests/test_http_contract.py:326-327,336-419,443-634` 对 route count、路径、方法、response model 有硬编码快照。新增两个 route 必须只做对应的最小更新。
- Alembic head 由 Preflight P-1 现场采集，**本节不记录任何 revision 号**——写进文档的 head 必然过期。你的 migration 从采集到的实际 head 线性延伸，不得制造分叉。迁移只在 P-2 确认的一次性 DB 上验证，禁止触碰生产数据。

**共享工作树基线：**见 Preflight P-1。基线由你现场采集，本节不做任何快照断言。

**本项目已知的高频踩坑点：**
- ❗ **没有模板引擎。** `render_template()` 只做 `__TOKEN__` 单遍替换；本票不需要修改 HTML。
- ❗ **i18n 有 3 个 locale**：本票无用户界面文案，不得顺手改 i18n。
- ❗ **架构边界是硬闸**：router 不 import `app.main`；service 不 import `app.routers.*`；`app/main.py` 只做 import + `include_router()`，不得放 SQL、执行逻辑或业务 route。
- ❗ **架构冻结 D1**：SSR + 原生 JS；本票不引入前端框架、任务队列平台或新基础设施。

## 目标（Goal）

在不复制 reachability 分类逻辑的前提下，建立租户级持久化检查运行记录与稳定 API，使 UI/agent 能读取“最近一次检查”的可靠快照，并且只有冻结范围内所有候选消息都完成审计时才可能得到 `healthy`。

## 范围边界

**In scope（交付物）：**

1. **持久化 run。** 新增 `reachability_audit_runs` 模型与 migration。至少保存：不可猜测 public run id、tenant id、状态、触发来源、algorithm version、scope from/to、冻结的最大 message id、matching/checked/reachable/unreachable counts、reason-count JSON、started/completed/created timestamps、受控 `safe_error_code`。约束合法枚举、非负计数和 tenant 查询索引。
2. **冻结且完整的扫描。** 创建 run 时冻结 `scope_to` 与候选 `max_message_id`；默认检查最近 7 天。后台 runner 按现有 helper 的上限逐页迭代到 `has_more=false`，累计结果并验证 `checked_count == matching_count`。允许对 `reachability_audit.py` 做向后兼容的最小筛选扩展，以支持冻结上界；旧调用结果不变。
3. **可信状态机。** 外部状态仅为 `healthy`、`attention`、`checking`、`no_data`、`incomplete`、`error`。`healthy` 只允许 completed + zero unreachable + complete；zero candidates 的完整 run 为 `no_data`；部分页、中断、冻结范围不一致或计数不等必须为 `incomplete`；受控执行失败为 `error`。禁止把异常/partial 当健康。
4. **异步触发。** `POST /api/admin/reachability-checks` 创建/复用本租户 active run 后立即返回 202 风格响应；完整审计不得在请求生命周期内同步执行。复用仓库 one-shot script/subprocess 模式或等价的最小本地机制，不引入 Celery/Redis/外部 queue。后台进程只接收 public run id，再从 DB 解析 tenant/scope，避免命令行暴露 tenant 标识。
5. **最新快照。** `GET /api/admin/reachability-checks/latest` 返回当前租户最新快照，包含状态、完整性、计数、reason counts、scope、last checked time、algorithm version 和安全错误码；不得返回 message content、payload、raw identifiers、paths、secrets、traceback。没有历史 run 时仍使用既定六态表达：零候选可返回 `no_data`，存在候选但尚无完整检查返回 `incomplete`，同时 `last_checked_at=null`，不得伪造已检查时间。
6. **并发/恢复。** 同租户并发 POST 只产生一个 active run，并返回同一 run；不同租户互不阻塞。runner 可重试同一 run 而不重复累计。超时/遗留 `checking` 必须以明确规则转为 `incomplete`，不能永久显示正在检查。
7. **认证与租户隔离。** 两个 API 复用当前 admin session tenant context；请求体不接受 tenant id。任意读写都显式 tenant scoped；猜测其他租户 public id 不能读取、启动或影响其 run。
8. **契约和测试。** 新增 schema/router/service/runner 聚焦测试，覆盖 0、1、>2000、跨页、partial、exception、并发、retry、stale、跨租户、auth、安全字段。更新 route snapshot 仅增加这两个新 API。

**Out of scope（显式非目标）：**
- 不改诊断页面、导航、CSS、JS 或 i18n；这些由 RND-338 负责。
- 不新增 findings 表、增量触发、daily timer、agent findings API、通知或自动修复；这些由 RND-339 负责。
- 不替换/重写 reachability classifier，不改变旧 `/api/admin/reachability-audit` 契约和默认分页语义。
- 不扫描 `pending`/`failed` 解密内容，不将“解密失败”混入 reachability reason。
- 不在 DB 列、API 响应、日志或进程参数中出现 `docs/agent-data-minimization.md` §2 的
  任何字段族。本票**唯一**的例外是不可猜测的 public run id（由 AC-6 显式授权）。
- 不引入 Redis/Celery/Kafka、新部署服务、云任务或生产迁移。

**本工单拥有的文件（只许写这些）：**
- `backend/app/db/models.py`（仅新增 reachability run 模型/约束）
- `backend/alembic/versions/0032_reachability_audit_runs.py`（可按实际线性 head 调整文件名/revision；只能新增一个线性 migration）
- `backend/app/reachability_audit.py`（仅为冻结范围增加向后兼容的可选上界/分页支持；不得改分类）
- `backend/app/services/reachability_check_service.py`（新建）
- `backend/app/schemas/reachability_checks.py`（新建）
- `backend/app/routers/reachability_checks.py`（新建）
- `backend/scripts/run_reachability_check_once.py`（新建 one-shot runner）
- `backend/app/main.py`（仅 import + include 新 router）
- `backend/tests/test_reachability_audit.py`（仅新增冻结边界/旧契约兼容用例）
- `backend/tests/test_reachability_checks.py`（新建）
- `backend/tests/test_reachability_check_cli.py`（可新建；不用则不建空文件）
- `backend/tests/test_http_contract.py`（仅两个新 route 的 schema、count、auth/shape 基线）

**本工单只读、绝不可写的文件：**
- `backend/app/routers/reachability_audit.py` — 旧技术 API 契约必须保持；若确需改动，先 BLOCK
- `backend/app/web/templates/diagnostics.html`、`backend/app/web/static/diagnostics.js`、`backend/app/web/static/diagnostics.css`、`backend/app/routers/web.py` — **所有者 RND-338**
- `backend/app/assets/i18n.js`、`backend/app/web/sidenav.py`、`backend/tests/test_sidenav.py` — **所有者 RND-336**（见 `tasks/WAVE-ownership.md` §3 的串行化裁决）
- `deploy/systemd/`、`backend/scripts/run_archive_worker_once.py` — **所有者 RND-339**
- `backend/app/audit.py`、`backend/app/routers/audit.py`、`backend/app/routers/auth.py`、`backend/app/routers/users.py`、`backend/app/routers/settings.py`、`backend/app/routers/retention.py`、`backend/app/routers/platform.py`、`backend/app/routers/media.py`、`backend/app/services/decrypt_worker.py` — **所有者 RND-335**（可能与本票并发进行）
- `backend/tests/test_rnd280_rbac_scaffold.py` — 新 router 使用现有 `get_current_user`，按 `docs/ticket-autopilot-workflow.md` §3.3 **不触发**闭世界白名单；不得为通过测试改它。若产品要求改为 `require_role`，先 BLOCK 确认权限模型
- CI/CD、部署脚本、`.env`、生产数据库、密钥文件

> 跨票所有权与并发矩阵的权威来源是 `tasks/WAVE-ownership.md`；本清单与它冲突时以它为准。

若实现所需文件不在拥有清单，停止并以 `BLOCKED_NEEDS_HUMAN` 说明最小缺口；不得顺手扩票。

## 验收标准（Acceptance Criteria）

> **每条子 AC 是一个原子断言，独立 pass/fail。** QA 按子 AC 编号逐条判定，findings
> 也按子 AC 编号定位。

### AC-1 迁移与模型
- **AC-1a**：新 revision 的 `down_revision` 指向 Preflight P-1 采集到的**实际唯一** head；`alembic heads` 之后仍只有一个 head。
- **AC-1b**：`upgrade` 建立 tenant-scoped runs 表，含 tenant FK、public run id 唯一约束、状态/来源枚举约束、非负计数 check、tenant 查询索引。
- **AC-1c**：`downgrade` 只删除本票新增的表与其约束/索引，不触碰任何既有对象。
- **AC-1d**：Given P-2 的一次性 PG，When `upgrade → downgrade → upgrade`，Then 三步都成功且最终 schema 与模型一致。**P-2 未满足时本条不得标记通过。**
- **AC-1e**：模型定义与 migration 定义逐字段对齐（列名、类型、可空性、约束），有静态断言测试；该测试不依赖真库。
- **AC-1f**：表中不存在消息内容、payload、raw sender/recipient/room/message id 字段。

### AC-2 冻结范围
- **AC-2a**：创建 run 时在**同一事务**中原子记录 `scope_from`、`scope_to`、`scope_max_message_id`、`algorithm_version`。
- **AC-2b**：Given run 已创建并完成第一页，When 插入 ID 更大的新消息，Then 该 run 的候选集不变（新消息不被本 run 检查）。
- **AC-2c**：Given run 已创建，When 回填一条 `scope_from` 之前的历史消息，Then 该 run 的候选集不变。
- **AC-2d**：默认 scope 为过去 7 天，边界时区明确（测试断言具体的 UTC 边界值，不是「大约 7 天」）。

### AC-3 完整分页
- **AC-3a**：0 条候选的 fixture：`checked_count == matching_count == 0`。
- **AC-3b**：1 条候选：恰好分类 1 次。
- **AC-3c**：2000 条（正好等于 `MAX_SCAN_LIMIT`）：每条恰好分类一次。
- **AC-3d**：2001 条（跨页）：每条恰好分类一次，无漏扫、无重扫、无 2000 截断。
- **AC-3e**：任意 fixture 下 `checked_count == matching_count`。
- **AC-3f**：各 reason 计数之和 == `checked_count`。
- **AC-3g**：不传新参数时，`build_message_reachability_report()` 的既有返回结构与代表性结果**逐字段不变**。

### AC-4 状态真实性
- **AC-4a**：完整 run + 零 unreachable → `healthy`。
- **AC-4b**：完整 run + 存在 unreachable → `attention`。
- **AC-4c**：完整 run + 零候选 → `no_data`。
- **AC-4d**：run 进行中 → `checking`。
- **AC-4e**：分页中断 / 页错误 / `checked != matching` / stale checking → `incomplete` 或受控 `error`。
- **AC-4f**：参数化测试遍历全部 partial / exception / unknown 路径，断言结果**永不**为 `healthy`。
- **AC-4g**：外部状态取值恰好是这六个，无第七种、无 null。

### AC-5 POST 异步与幂等
- **AC-5a**：Given 鉴权 POST，Then 请求返回前**未**执行全量 scanner（用 scanner 调用计数或耗时断言，不是「感觉很快」）。
- **AC-5b**：响应为 202 风格的 accepted/checking 快照。
- **AC-5c**：同租户并发 POST → 返回**同一个** active public run id。并发性必须在数据库/事务层证明，模块级全局变量或进程内线程锁不算。
- **AC-5d**：不同租户并发 POST → 各自独立的 active run，互不阻塞。
- **AC-5e**：上一个 run 完成后，可以创建下一个 run。

### AC-6 GET latest 契约
- **AC-6a**：响应 state 为六态之一，且含 `complete`、scope、counts、reason counts、timestamps、`algorithm_version`、safe error code。
- **AC-6b**：Given 该租户零候选且有完整 run，Then `no_data`。
- **AC-6c**：Given 存在候选但尚无完整 run，Then `incomplete` 且 `last_checked_at` 为 `null`（不得伪造检查时间）。
- **AC-6d**：递归检查响应 keys 与 values，不含 `docs/agent-data-minimization.md` §2 的任何字段族；本票唯一允许的标识是不可猜测的 public run id。
- **AC-6e**：错误情形只返回 allowlist 化的 `safe_error_code`，不返回 raw exception 或 traceback。

### AC-7 runner 恢复
- **AC-7a**：one-shot runner 只接受 run public id 作为定位参数；tenant/scope 由它自己从 DB 解析（进程命令行不出现 tenant 标识）。
- **AC-7b**：对同一个 run 重复执行，计数不重复累计（幂等）。
- **AC-7c**：unknown run id / 已完成 run / 他租户 run → fail closed，不修改任何数据。
- **AC-7d**：进程异常后，该 run 按确定的 stale 规则离开 `checking`，不会永久停留。

### AC-8 认证与租户隔离
- **AC-8a**：未登录访问两个新 API 都失败。
- **AC-8b**：request body 与 query 均不接受调用者指定的 tenant id。
- **AC-8c**：Given tenant A/B 同时 seed，Then A 的 GET / POST / runner 全路径都不读取也不更新 B 的数据。
- **AC-8d**：service 层每一个 ORM 读写都显式 tenant scoped（不是只在 router 出口过滤）。
- **AC-8e**：用他租户的 public run id 请求，响应不泄露其存在性（存在与不存在的响应一致）。

### AC-9 兼容、架构与 route contract
- **AC-9a**：旧 `/api/admin/reachability-audit` 的路径、query 参数、response model 与代表性结果全部不变。
- **AC-9b**：新增 route 恰好 2 个；`test_http_contract.py` 的 `route_count` 由**采集到的当前真实值** +2 得到，不是写死数字。
- **AC-9c**：`main.py` 的 diff 只有 router import 与 `include_router()`，无 SQL、无业务逻辑、无内联 schema。
- **AC-9d**：service 不 import `app.routers.*`，router 不 import `app.main`（架构硬闸通过）。
- **AC-9e**：分类逻辑只来自 `app.reachability_audit`；新 service 中不存在重新定义的 status/reason 分支。

### AC-10 回归与范围
- **AC-10a**：`make verify` exit 0。
- **AC-10b**：聚焦测试、`test_http_contract.py`、`test_architecture_boundary.py`、`test_verify_alembic_head.py` 全部 exit 0。
- **AC-10c**：本票归因 diff 仅限拥有文件；前端、systemd、archive worker、旧 router 无本票改动。

## 验证方式（Verification — 确定性闸）
- 类型：**automated + R2 migration review**
- 命令：
  ```bash
  make verify
  .venv/bin/python -m pytest backend/tests/test_reachability_audit.py backend/tests/test_reachability_checks.py -q
  test ! -f backend/tests/test_reachability_check_cli.py || .venv/bin/python -m pytest backend/tests/test_reachability_check_cli.py -q
  .venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py backend/tests/test_verify_alembic_head.py -q
  (
    cd backend
    .venv/bin/python -m alembic heads
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m alembic downgrade -1
    .venv/bin/python -m alembic upgrade head
  )
  git diff --check
  git status --short --branch
  ```
- **上面的 `alembic` 往返块依赖 Preflight P-2。** `DATABASE_URL` 为空或无法证明是一次性库时，
  **不要运行它**——按 P-2 输出 `BLOCKED_NEEDS_HUMAN`，AC-1d 保持未完成。不需要真库的
  静态 schema 断言（AC-1e）照常写照常跑。**禁止**为了让往返跑通而改 CI 配置或改用 SQLite。
- 通过 = 全部子 AC 满足、唯一 Alembic head、所有适用命令 exit 0。

## 依赖（Dependencies）
- 无前置工单；必须基于现有 reachability classifier 和当前 Alembic head。
- **R2 人工闸**：Haisu 确认 run schema、后台执行方式和迁移方案后才能开始产品改动。
- 本票 QA PASS 后才允许 RND-338、RND-339 开始；不得在本票内提前实现后继功能。

## 完成定义（Definition of Done）
- [ ] Preflight P-1（含实际 Alembic head）、P-2、P-3 结论已记录
- [ ] AC-1a ~ AC-10c **每一条子 AC** 都有自动化证据（不是每个大项一条）
- [ ] frozen scope、2001+ 分页、partial/exception/concurrency/tenant tests 完整
- [ ] 两个新 API 与旧技术 API 契约均验证
- [ ] migration upgrade/downgrade/upgrade 在 P-2 确认的一次性 DB 上通过且 Alembic 唯一 head
- [ ] API/日志/进程参数通过 `docs/agent-data-minimization.md` §5 哨兵递归断言
- [ ] `make verify` 全绿，架构硬闸通过
- [ ] 本票归因 diff 只在拥有文件，结束 `git status` 已记录
- [ ] QA Summary 包含 route count、页数/检查数、状态表和迁移证据
- [ ] 未 commit、未 push、未建分支

## 风险与回滚（Risk & rollback）
- 风险：分页期间候选集漂移导致漏扫；用创建时 scope + max message id 冻结。
- 风险：进程崩溃留下永久 checking；用 started_at/stale 规则降级为 incomplete。
- 风险：同租户并发产生多个 active run；在数据库/事务层建立可测试的唯一性或等价原子保护，不能只靠进程内锁。
- 风险：raw exception/标识泄露；只持久化 allowlisted safe error code，详细异常仅安全日志且不得含 payload。
- 回滚：停止新调用、回退 router/service/model 代码，再执行本 migration downgrade；旧技术 API 始终可用。禁止对生产自行执行回滚。

## 人工点位（Human touchpoints）
- **Trigger / R2 Gate**：Haisu 审阅并确认 migration、状态机、后台 runner 方案后将 RND-337 置 In Progress。
- **Gate**：Haisu 审阅 QA Summary 和 migration diff 后批准 commit；agent 不得自行 commit。
- **Escalation**：Preflight P-2 无法满足、`alembic heads` 已有分叉、现有 helper 无法在允许文件内冻结候选、需新队列/部署变更、需 `require_role` 或两轮修复仍失败时，`BLOCKED_NEEDS_HUMAN`。

## 开发 agent 执行指引（步骤）
1. 跑 Preflight P-1 / P-2 / P-3 并记录结论；读 classifier/router/tests、既有 migration 和 one-shot worker pattern。
2. 先写模型/migration 与状态转换/冻结边界/多页/并发/tenant/security contract tests；提交人工 review，等待 R2 gate。
3. 最小扩展 classifier 的冻结筛选，不复制 status 判定；实现 service 与幂等 runner。
4. 新 router 只解析 auth/input/output；main 仅 include；`route_count` 读当前真实值再 +2。
5. 在 P-2 确认的一次性 DB 跑 migration 往返与全闸；输出 QA Summary + 归因 status；不要 commit。

## 硬性约束（来自 DEV_AGENT_RULES.md）
- 不 commit/push/建分支/改历史；不改 CI/CD、部署、`.gitignore` 或生产数据。
- 不复制 reachability 分类，不改旧 API，不触碰 RND-338/339 文件。
- 不在 request lifecycle 同步全扫，不引入外部 queue/infrastructure。
- 数据最小化以 `docs/agent-data-minimization.md` 为准（DB 列、API、日志、CLI 参数四个面），例外只有 public run id。
- `healthy` 必须由完整冻结扫描证明；任何不确定性 fail closed 为 incomplete/error。
- 证据优先：以 DB 约束、并发测试、2001+ 分页、migration 往返和 exit 0 为证。
