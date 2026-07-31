[Goal check] This work advances 开发（Development） by 交付平台总控台的跨租户聚合端点，复用已专为本票预留 tenant_id=None 语义的 UsageService，逐租户列出用量摘要。

# RND-307 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-307 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-307「B1-3 跨租户聚合」｜父 Epic RND-275（B1 平台总控台）
- 优先级：Medium｜风险等级：**R1**｜milestone：R5 · 云商业化前台

## 背景与项目现状（已实地核实）

`UsageService`（`backend/app/services/usageservice.py`，<issue>RND-281</issue> 已 Done）的模块 docstring **明确写着**（`usageservice.py:1-5`）：
> "Every function takes an optional `tenant_id`: when `None` the query is run across ALL tenants (cross-tenant aggregation, **reused by B1-3**)."

也就是说这个模块**从一开始就是为本票预留的接口**，公开函数（均接受 `tenant_id: Optional[str] = None`）：
- `count_messages(db, tenant_id=None) -> int`
- `sum_storage(db, tenant_id=None) -> int`
- `count_monitored_employees(db, tenant_id=None) -> int`
- `get_archived_days(db, tenant_id=None) -> int`
- `sync_health(db, tenant_id=None) -> dict`

**注意语义**：这些函数传 `tenant_id=None` 时返回的是**全平台合计**（一个数），不是"每个租户各自一条"的列表。本票 AC 要的是"**每租户**聚合"（逐租户列表），所以正确用法是**遍历全部租户，对每个租户各调用一次这些函数**（传各自的 `tenant.id`），而不是只调一次 `tenant_id=None` 就当作结果。`tenant_id=None` 的合计值可以作为列表末尾的"全平台合计"行（可选加分项，非必需）。

B1-2（<issue>RND-305</issue>，跨租户鉴权作用域）已 Done：`app/auth.py:207` 的 `require_platform_admin`。

**❗ 本项目高频踩坑：**
- **`platform.py` 是本波次共享文件**：RND-310/312/313/314 也会往这个文件加端点。开始前先 `git diff`/`git status` 看清哪些兄弟票的端点已落地，在文件末尾追加自己的函数。`test_http_contract.py` 的 `route_count` 用**当前实际值** +1 回填。
- **不要误用 `tenant_id=None` 当作"每租户聚合"**——见上方语义说明，这是本票最容易踩的坑。
- **N+1 查询风险**：租户数量增长后，对每个租户逐个调用 5 个聚合函数可能产生较多查询。本票**首期允许 N+1**（租户数量级不大，估时仅 1 周），但需在代码注释里标注"若租户数增长需批量优化"，不强制本票做批量化改造。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个平台总控台端点，返回每个租户各自的用量聚合摘要（消息数/存储/员工数/同步健康），复用 `UsageService` 现成函数，不重写聚合逻辑。

## 范围边界

**In scope：**
1. `backend/app/routers/platform.py` **新增** `GET /tenants/usage`：
   - 鉴权：`require_platform_admin`。
   - 遍历全部 `Tenant`（不筛 `is_active`，停用的租户也应能看到历史用量——若产品期望只看活跃租户，见 Escalation）。
   - 对每个租户调用 `count_messages`/`sum_storage`/`count_monitored_employees`/`sync_health`（`get_archived_days` 可选，若 AC 不要求可不含，保持响应精简）。
   - 响应：`{"tenants": [{"tenant_id", "tenant_name", "message_count", "storage_bytes", "employee_count", "sync_health"}, ...]}`。
2. 测试：`backend/tests/test_rnd307_cross_tenant_usage.py`。

**Out of scope（显式非目标）：**
- 不改 `UsageService` 本身（只调用）。
- 不做批量优化 / 缓存（首期 N+1 可接受，见上）。
- 不做创建 / 列表 / 启停 / 自检（其他票）。
- 不暴露消息内容，只暴露聚合数字（沿用 <issue>RND-282</issue> Dashboard API 的既有原则）。

**本工单拥有的文件（只许写这些，`platform.py` 为共享追加）：**
- `backend/app/routers/platform.py` —— **仅新增** `GET /tenants/usage` 及其依赖
- `backend/app/schemas/tenant_provision.py`（或复用兄弟票已建的 schema 文件）—— 仅新增聚合相关 schema
- `backend/tests/test_rnd307_cross_tenant_usage.py`（新）
- `backend/tests/test_http_contract.py` —— 契约同步

**只读、绝不可写：** `app/services/usageservice.py`（只调用）、`app/db/models.py`、`app/auth.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 每租户聚合**：`GET /tenants/usage` 返回列表，**条数等于租户数**，每条含该租户各自的消息数/存储/员工数/同步健康——**不是**全平台合计的单一数字。须有测试构造 2 个租户、断言两条结果的数值分别对应各自租户的数据（不相同/不串数据）。
- **AC-2 复用而非重写（关键）**：`grep -n "count_messages\|sum_storage\|count_monitored_employees\|sync_health" backend/app/routers/platform.py` 应有命中；确认调用的是 `usageservice` 里的既有函数，**不是**重写一套聚合 SQL。
- **AC-3 空态**：无租户时返回空列表，不报错。
- **AC-4 鉴权**：无凭据 / 错凭据 → 401。
- **AC-5 不泄露内容**：响应只含聚合数字，不含任何消息正文/媒体 URL。
- **AC-6 契约同步 + 回归**：`test_http_contract.py` 已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd307_cross_tenant_usage.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "count_messages\|sum_storage\|count_monitored_employees\|sync_health" backend/app/routers/platform.py    # AC-2
git diff -- backend/app/routers/platform.py    # 人工核对：只新增，未碰兄弟票端点
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-281（A1-1 UsageService）**已 Done**（`tenant_id=None` 语义专为本票预留）。RND-305（B1-2）**已 Done**（`require_platform_admin`）。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明是否遍历了全部租户还是仅活跃租户（及理由）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：把 `tenant_id=None` 的合计值误当成"每租户"结果，导致列表实际上是同一份全平台数字复制 N 份——由 AC-1 的多租户区分测试防守，这是本票最该防的坑。
- 风险：租户数增长后 N+1 查询变慢——首期接受，注释标注，不在本票范围内优化。
- 回滚：纯新增，`git checkout -- backend/app/routers/platform.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿即可，无需额外人工审阅（R1，纯只读聚合）。
- **Escalation**：若无法确定"停用租户是否应出现在聚合列表里"这一产品决策 → `BLOCKED_NEEDS_HUMAN`（默认行为写在上面的 In scope 里：全部租户含停用的都显示，除非明确反馈要改）。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/routers/platform.py`（先 `git diff`/`git status` 看兄弟票是否已落地）、`app/services/usageservice.py`（全部公开函数签名）、`app/auth.py:207`（`require_platform_admin`）。
2. 新增 `GET /tenants/usage`，遍历租户逐个调用 `UsageService` 函数。
3. 写测试覆盖 AC-1~AC-3（**AC-1 的多租户区分是重点**）。
4. 同步 `test_http_contract.py`。
5. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假租户/消息数据。
- 不扩大 Scope：批量优化/缓存/创建/列表/启停/自检一律 Out。
- 复用优先：`UsageService` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
