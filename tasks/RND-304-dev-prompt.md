[Goal check] This work advances 开发（Development） by 交付租户级「首次向导已完成」标志的存储与读写端点，收口 A9 首次配置向导 epic，且不依赖仍在进行中的 F0/RND-244 配置中心。

# RND-304 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-304 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 2026-07-31：设计反转，不再依赖 F0/RND-244（PM 决策，附理由）

本票旧稿假设 `first_run` 标志必须存在 F0（<issue>RND-244</issue> 配置中心）的租户级 KV 里，因为它是通用配置存储。**重新核实后判定该假设不成立，改为在 `Tenant` 表直接加一列**，理由：
1. `RND-244` 是一个 12 张子票的独立大 epic，**至今仍 In Progress**，把一个布尔标志绑在它身上意味着这张小票的 ETA 完全不可控——正是 <issue>RND-318</issue>（C3-1 留存配置）当初也踩过、后来改成独立表的同一类问题。
2. 一个单一的"是否完成过首次向导"布尔状态，本质上是**租户身份的一部分**（类似 `Tenant.is_active`），比"KV 里存一个字符串键值对"更适合直接建模成 `Tenant` 表的一列——这比新建一整张表或依赖配置中心都更简单，不是过度设计也不是偷工减料。
3. R2 里程碑（开源发布闭环）想要"陌生人 clone 后 30 分钟内看到第一条真实消息"这个验收标准落地，本票是 onboarding 闭环的最后一块——不应该被一个不相关的大 epic 卡住进度。

**若你认为这个判断有误（例如产品确实需要多个配置项共享同一套 KV 机制）→ 见下方 Escalation，不要自行改回依赖 F0 的设计。**

## ⚠️ 2026-07-31 追加：两个测试文件的手写 tenants 表结构需要随本票同步一列（回应 agent 的 BLOCKED_NEEDS_HUMAN 上报）

dev agent 正确指出：`backend/tests/test_rnd307_cross_tenant_usage.py`（第 19 行起）与 `backend/tests/test_rnd309_content_access_request.py`（第 37 行起）用**手写的 `CREATE TABLE tenants (...)` SQL 字符串**（不是 `Base.metadata.create_all()`/`Tenant.__table__.create()` 反射真实 ORM 元数据）搭建测试用的 SQLite 表，并且各自确实对这张手写表做了 `Tenant(...)` ORM insert——新增 `onboarding_completed_at` 列后，ORM 的 INSERT 语句会带上这一列，手写表里没有这一列，SQLite 会报错。

**已核实排查范围**：全仓另有 10 个测试文件同样手写了 `CREATE TABLE tenants`，但逐一核对后，其余全部要么不做 `Tenant(...)` ORM insert，要么改用 `Base.metadata.create_all()`/`Tenant.__table__.create()` 反射真实模型（天然免疫这类改动，如 `test_rnd318_retention_config.py`、`test_media_file_size_backfill.py` 的相关用例）。**只有这两个文件受影响，且都是 RND-307/RND-309 已完成工单的既有测试。**

**决策**：本票**同步收纳这两个文件的最小改动**（比照 `docs/ticket-autopilot-workflow.md` §3.3 的守卫文件原则——新增列导致既有守卫性测试失败时，同步它们是本票范围，不是另开工单，也不是 `BLOCKED_NEEDS_HUMAN`）：

- `backend/tests/test_rnd307_cross_tenant_usage.py`：在第 19 行起的 `CREATE TABLE tenants (...)` 列表末尾追加一列 `onboarding_completed_at DATETIME`（SQLite 里不加 `NOT NULL` 即默认可空）。
- `backend/tests/test_rnd309_content_access_request.py`：在第 37-40 行的 `CREATE TABLE tenants (...)` 字符串末尾同样追加 `onboarding_completed_at DATETIME`。

**除了这一行 DDL 改动，这两个文件的其他任何内容（断言、fixture 逻辑、其他表结构）一律不动。**

## 任务身份
- 工单：RND-304「A9-3 标记首次完成」｜父 Epic RND-269（A9 首次配置向导）
- 优先级：High｜风险等级：**R1**｜milestone：R2 · 开源发布闭环
- **本票是 A9 epic 的最后一块**（A9-1/A9-2 已通过 <issue>RND-318</issue>/<issue>RND-303</issue> 满足并 Done）

## 背景与项目现状（已实地核实）

`Tenant` 模型（`backend/app/db/models.py:39` 附近）当前字段：`id`/`name`/`slug`/`is_active`/`created_at`/`updated_at`。**没有**任何 onboarding 相关列——本票新增。

**路由文件预留**：<issue>RND-303</issue>（A9-2 批量邀请）的实现说明里提到 `app/routers/onboarding.py` 是"预定给 RND-304 的新文件"，RND-303 特意避免创建它、改在 `routers/auth.py` 里加批量端点。**本票现在是第一个、也是唯一一个创建这个文件的票**，不会有冲突。

**迁移序位**：当前 alembic head 在 <issue>RND-318</issue>（`0027`）之后。若 <issue>RND-319</issue>（C3-2）与本票在同一天并行推进，<issue>RND-319</issue> 占用 `0028`，**本票为** `0029`。**实现时先 `git pull` 确认实际 head**；若已漂移，按实际顺延并在 QA Summary 注明。

**❗ 本项目高频踩坑：**
- **RBAC 白名单是闭世界测试**：`backend/tests/test_rnd280_rbac_scaffold.py` 断言只有指定文件可以出现 `require_role`（<issue>RND-318</issue> 已把 `retention.py` 加进这份白名单，供参考同样的加法）。**本票新建 router 用 `require_role` 会触发该断言，必须同步白名单**（见 `docs/ticket-autopilot-workflow.md` §3.3）。
- 架构冻结 D1：纯后端，无前端页面（"下次跳过向导"的判断逻辑消费方是前端，本票只交付判断所需的数据源）。

## 目标（Goal）
交付一个租户级"是否已完成首次配置向导"的标志：读取端点供前端判断是否要跳转到向导，完成端点供向导走完后置位，且置位后幂等、不可被状态回退。

## 范围边界

**In scope：**
1. **迁移** `0029_tenant_onboarding_completed.py`（或实际顺延版本号）：`tenants` 表新增可空列 `onboarding_completed_at`（`DateTime(timezone=True)`, nullable，无需回填）。
2. `app/db/models.py`：`Tenant` **仅新增** `onboarding_completed_at` 列定义。
3. `backend/app/routers/onboarding.py`（**新建**）：
   - `GET /api/onboarding/status`：`require_role()`（任意已登录角色可读），返回 `{"first_run": bool}` —— `onboarding_completed_at is None` 时 `first_run=true`。
   - `POST /api/onboarding/complete`：`require_role("admin", "owner")`，若 `onboarding_completed_at` 为空则置为当前时间；**若已设置过，保持原值不变（幂等，不重置时间戳）**。返回 `{"first_run": false}`。
   - 挂载到 `app/main.py`（仿既有 router 注册方式，仅新增 1 行 import + 1 行 `include_router`）。
4. `backend/app/schemas/onboarding.py`（**新建**）：`OnboardingStatusOut` / `OnboardingCompleteOut`。
5. 测试：`backend/tests/test_rnd304_onboarding_status.py`。

**Out of scope（显式非目标）：**
- 不做向导页面本身（属 A9-1/A9-2，均已交付）。
- 不做"下次跳过"的前端跳转逻辑（本票只提供 `first_run` 布尔，消费方是前端）。
- 不依赖 F0/<issue>RND-244</issue>（见上方设计反转说明）。
- 不允许"重新打开向导"式的重置端点（一旦完成，只能通过直接改库回退，不提供 API 层面的"取消完成"）。

**本工单拥有的文件（只许写这些）：**
- `backend/alembic/versions/0029_tenant_onboarding_completed.py`（新，若 head 漂移则改用实际顺延版本号）
- `backend/app/db/models.py` —— **仅新增** `Tenant.onboarding_completed_at` 列
- `backend/app/routers/onboarding.py`（新）
- `backend/app/schemas/onboarding.py`（新）
- `backend/app/main.py` —— **仅新增** 1 行 import + 1 行 `include_router`
- `backend/tests/test_rnd304_onboarding_status.py`（新）
- `backend/tests/test_rnd280_rbac_scaffold.py` —— 同步白名单（新增 `onboarding.py`），只加必要条目
- `backend/tests/test_http_contract.py` —— 契约同步（新增 2 个路由：GET + POST）
- `backend/tests/test_rnd307_cross_tenant_usage.py` —— **仅**在手写 `CREATE TABLE tenants` 里追加 `onboarding_completed_at DATETIME` 一列，其余逐字不变
- `backend/tests/test_rnd309_content_access_request.py` —— **仅**在手写 `CREATE TABLE tenants` 里追加 `onboarding_completed_at DATETIME` 一列，其余逐字不变

**只读、绝不可写：** `app/routers/retention.py`（RND-318 拥有）、`app/routers/auth.py`（RND-303 拥有）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 默认未完成**：从未调用过 `complete` 的租户，`GET /api/onboarding/status` → `first_run: true`。
- **AC-2 完成后置位**：`POST /api/onboarding/complete` 后，`GET` 返回 `first_run: false`。
- **AC-3 幂等（关键）**：连续调用两次 `POST /api/onboarding/complete`，第二次**不改变** `onboarding_completed_at` 的时间戳值（须有测试断言两次调用后时间戳相同，不是被覆盖成更新的时间）。
- **AC-4 租户隔离**：`tenant_id` 只来自 `require_role()` 会话解包，不接受请求参数；跨租户反例测试——租户 A 完成向导，租户 B 的 `first_run` 仍为 `true`。
- **AC-5 鉴权分级**：`GET` 任意角色可读；`POST` 仅 `admin`/`owner`（普通角色调用 `POST` → 403）。
- **AC-6 迁移可逆**：`upgrade`/`downgrade` 均可执行；`alembic check` 无 drift。
- **AC-7 契约同步 + RBAC 同步 + 回归**：`test_http_contract.py`（route_count 当前基线 +2）与 `test_rnd280_rbac_scaffold.py`（白名单加 `onboarding.py`）均已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过。
- **AC-8 手写 tenants 表同步（关键，见上方追加说明）**：`test_rnd307_cross_tenant_usage.py`/`test_rnd309_content_access_request.py` 的 `Tenant(...)` insert 用例仍然通过；两个文件的 diff **只应有** `CREATE TABLE tenants` 里新增的那一列，不得触及其余任何断言/逻辑。

## 验证方式（Verification — 确定性闸）
```bash
git pull   # 确认 alembic head，见「迁移序位」
make verify
.venv/bin/python -m pytest backend/tests/test_rnd304_onboarding_status.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m pytest backend/tests/test_rnd307_cross_tenant_usage.py backend/tests/test_rnd309_content_access_request.py -q   # AC-8
.venv/bin/python -m alembic check    # AC-6：无 drift
git diff -- backend/app/db/models.py    # 人工核对：只新增 onboarding_completed_at
git diff -- backend/tests/test_rnd307_cross_tenant_usage.py backend/tests/test_rnd309_content_access_request.py   # AC-8：只应有新增一列的 DDL 改动
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
无剩余前置（"F0" 通用基础鉴权/租户脚手架 RND-276~280 均已 Done；本票**不**依赖 RND-244 配置中心，见开头设计反转说明）。<issue>RND-303</issue>（A9-2）✅ Done，已确认 `routers/onboarding.py` 未被占用。本票是 A9 epic 最后一块。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-8 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（关键）**：`complete` 端点非幂等，重复调用重置时间戳——由 AC-3 防守。
- 风险 2：迁移版本号与实际 head 不符——实现前先 `git pull` 核实（尤其注意 <issue>RND-319</issue> 若同天并行，可能已占用 `0028`）。
- 回滚：`downgrade` 迁移 + `git checkout -- backend/app/db/models.py backend/app/main.py`；无数据影响（新增列默认 NULL）。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：涉及新迁移但仅新增可空列、无回填、风险很低，测试绿即可，无需额外人工审阅。
- **Escalation**：若发现产品实际需要的是"可重新打开向导"（即需要一个取消完成的机制）→ `BLOCKED_NEEDS_HUMAN`，说明具体诉求，不要自行加一个重置端点（这不在本票 AC 里）。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/db/models.py`（`Tenant` 定义位置）、`app/routers/retention.py`（RND-318 的新 router + 新迁移范式，直接照抄结构）、`app/auth.py`（`require_role` 用法）。
2. `git pull` 确认 alembic head，写迁移。
3. 新增 `onboarding.py` router（GET/POST）+ schema。
4. 挂载到 `main.py`。
5. 写测试覆盖 AC-1~AC-5（**AC-3 幂等是重点**）。
6. 同步 `test_http_contract.py` 与 `test_rnd280_rbac_scaffold.py`。
7. 在 `test_rnd307_cross_tenant_usage.py`/`test_rnd309_content_access_request.py` 的手写 `CREATE TABLE tenants` 里各追加 `onboarding_completed_at DATETIME` 一列（**仅此一行改动**），跑一遍确认这两个文件恢复通过。
8. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假租户数据。
- 不扩大 Scope：不做向导页面、不做重置端点、不依赖 F0。
- 复用优先：鉴权范式仿 `retention.py`/`require_role`，不重新发明。
- 证据优先，以 exit 0 / 测试通过为证。
