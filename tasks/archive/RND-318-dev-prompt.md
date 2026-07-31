[Goal check] This work advances 开发（Development） by 交付按租户独立配置的消息保留策略表 + 读写端点，为首次配置向导（RND-301）与到期清理任务（RND-319）提供数据地基。

# RND-318 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚠️ 2026-07-31：本版设计取代 2026-07-29 的旧稿（PM 决策，附理由）

本文件此前的版本假设留存配置必须存于 F0（<issue>RND-244</issue> 配置中心）的租户级 KV，理由是"和 RND-304 的 `onboarding_completed` 同属 F0 KV 范畴"。**重新核实后判定该假设不成立，本票改为独立数据表**，理由：
1. `RND-244`（配置中心）**至今仍是 In Progress，未 Done**，且是一个 12 张子票的完整 epic——把 C3-1 挂在它身上意味着一个小票被一个大 epic 的进度锁死，ETA 不可控。
2. 留存策略是**合规关键数据**（保留天数 + 锁定语义），字段结构固定、需要唯一约束（一租户一份）+ 校验（天数范围）+"锁定后不可逆"这种数据库层面的完整性保证——这比 RND-304 那种"一个布尔标志位"复杂得多，用字符串 KV 存并不合适。
3. 工单标题本身写的是「留存配置（**表**/租户设置）」——"表"是标题里就写明的可选落点，不是本次臆造。

**若这个判断有误（例如 Haisu 确认就是要挂 F0 KV），按下方 Escalation 处理，不要自行改回旧设计。**

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-318 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-318「C3-1 留存配置（表/租户设置）」｜父 Epic RND-272（C3 留存与清理）
- 优先级：Medium｜风险等级：**R2**（新表 + 新迁移；本票是合规留存策略的地基，一旦有租户依赖该配置做清理，误改会有数据影响）｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实）

全仓 `grep -rln "retention\|留存" backend/app/` **无任何既有留存配置代码**——本票是全新领域，没有可复用的服务模块，但**鉴权/迁移的基础设施都已就绪**，遵循既有范式即可：
- 租户内鉴权范式：`backend/app/routers/users.py:33-38` 的 `require_role()`，`tenant_id` 从会话解包，不接受请求参数。
- **RBAC 白名单是闭世界测试**：`backend/tests/test_rnd280_rbac_scaffold.py:77-86` 断言只有 `{audit.py, media_library.py, users.py}` 可以出现 `require_role`。**本票新建 router 会触发该断言，必须同步白名单**（见 `docs/ticket-autopilot-workflow.md` §3.3）。

## 迁移序位（重要）

当前 alembic head = **0026**（`0026_export_approval_tokens.py`，来自 <issue>RND-316</issue>）。本票 = `0027`。**实现时先 `git pull` 确认实际 head 仍是 0026**；若已漂移（本波次其他票也可能已推进 head），按实际 head 顺延并在 QA Summary 注明，**不要**硬凑 `0027` 这个文件名如果实际前置版本号已经变化。

**❗ 本项目高频踩坑：**
- 架构冻结 D1：纯后端。
- **本票不碰 `platform.py`**——本波次 RND-307/310/312/313/314 在改那个文件，本票是独立的租户内配置领域，走 `require_role`，与平台超管无关，不要混淆两套鉴权模型。

## 目标（Goal）
交付一张按租户独立的留存策略表（保留天数 + 是否锁定）+ 读写端点，供首次配置向导写入初始策略、供未来的到期清理任务读取。

## 范围边界

**In scope：**
1. **迁移** `0027_retention_config.py`：新表 `retention_configs`（`id`/`tenant_id` FK **唯一约束**（一租户一份配置）/`retention_days` Integer NOT NULL/`is_locked` Boolean NOT NULL default False/`created_at`/`updated_at`）。
2. `app/db/models.py` **新增** `RetentionConfig` ORM 类（紧邻其他租户级配置模型，仿 `TenantWecomConfig` 的风格）。
3. `app/routers/retention.py`（**新建**）：
   - `GET /api/admin/retention-config`：`require_role()`（任意已登录租户角色可读），返回当前租户配置；**若尚未配置**（首次访问，本票不预置默认行）→ 返回 `configured: false` + `retention_days: null`，不是 404（前端向导需要能区分"未配置"和"服务器错误"）。
   - `PUT /api/admin/retention-config`：`require_role("admin", "owner")`，请求体 `{"retention_days": int, "lock": bool}`。
     - `retention_days` 校验：`1 <= retention_days <= 3650`（10 年上限，防误配），非法值 → 422。
     - **一旦 `is_locked=True`，后续任何 `PUT` 一律拒绝**（423 Locked），即便请求体想把 `lock` 改回 `false`——本票**不提供解锁机制**（解锁是更高权限的独立范围，本票不做）。首次 `PUT` 若 `lock=true` 则本次写入后立即生效锁定。
4. 测试：`backend/tests/test_rnd318_retention_config.py`。

**Out of scope（显式非目标）：**
- **不做到期清理任务本身**（<issue>RND-319</issue>，C3-2 到期锁定/清理任务——本票只交付配置表，不消费它触发清理）。
- **不做跨租户统一策略**——每租户独立（ticket 原文 Non-goals 已写明）。
- **不提供解锁端点**——锁定后不可逆，是本票的既有约束，不是遗漏。
- 不做前端向导页面（属 <issue>RND-301</issue>）。
- 不依赖 F0/<issue>RND-244</issue>（配置中心）——见本文件开头的设计变更说明。

**本工单拥有的文件（只许写这些）：**
- `backend/alembic/versions/0027_retention_config.py`（新，若 head 漂移则改用实际顺延版本号）
- `backend/app/db/models.py` —— **仅新增** `RetentionConfig` 类
- `backend/app/routers/retention.py`（新）
- `backend/app/schemas/retention.py`（新）
- `backend/app/main.py` —— **仅新增** 1 行 import + 1 行 `include_router`
- `backend/tests/test_rnd318_retention_config.py`（新）
- `backend/tests/test_rnd280_rbac_scaffold.py` —— 同步白名单（新增 `retention.py`），只加必要条目
- `backend/tests/test_http_contract.py` —— 契约同步（新增 2 个路由：GET + PUT）

**只读、绝不可写：** `app/routers/users.py`（只参考范式）、`app/routers/platform.py`（他票拥有）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 策略可配**：`PUT` 合法请求 → `retention_configs` 表出现/更新对应行；`GET` 返回刚写入的值。
- **AC-2 未配置态可区分**：从未 `PUT` 过的租户 `GET` → `configured: false`，不是 404 / 500。
- **AC-3 锁定后不可再改（关键）**：`PUT` 一次 `lock=true` 后，任何后续 `PUT`（无论内容）→ 423，且**数据库中的值确实未被改动**（须有测试验证锁定后再 `PUT` 不同 `retention_days`，查库确认原值不变）。
- **AC-4 输入校验**：`retention_days` ≤ 0 或 > 3650 → 422；须有边界测试（0、3651、负数）。
- **AC-5 租户隔离**：`tenant_id` 只来自 `require_role()` 会话解包；跨租户反例测试——租户 A 的配置对租户 B 不可见/不可改。
- **AC-6 鉴权分级**：`GET` 任意角色可读；`PUT` 仅 `admin`/`owner`（普通角色 `PUT` → 403）。
- **AC-7 迁移可逆**：`upgrade`/`downgrade` 均可执行；`alembic check` 无 drift。
- **AC-8 契约同步 + RBAC 同步 + 回归**：`test_http_contract.py`（route_count 当前基线 +2）与 `test_rnd280_rbac_scaffold.py`（白名单加 `retention.py`）均已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
git pull   # 确认 alembic head，见「迁移序位」
make verify
.venv/bin/python -m pytest backend/tests/test_rnd318_retention_config.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m alembic check    # AC-7：无 drift
git diff -- backend/app/db/models.py    # 人工核对：只新增 RetentionConfig
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
无剩余前置（"F0" 通用基础鉴权/租户脚手架 RND-276~280 均已 Done；本票**不**依赖 RND-244 配置中心，见开头设计变更说明）。本票 **blocks** <issue>RND-301</issue>（A9-1 首次向导写留存策略）与 <issue>RND-319</issue>（C3-2 到期清理任务）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-8 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明表结构字段（供 RND-301/RND-319 对接）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（关键）**：锁定语义实现不严，锁定后仍可被改——由 AC-3 防守，这是本票唯一带"不可逆"性质的地方，必须扎实。
- 风险 2：迁移版本号与实际 head 不符——实现前先 `git pull` 核实。
- 回滚：`downgrade` 迁移 + `git checkout -- backend/app/db/models.py backend/app/main.py`；已锁定的行不受迁移回滚影响（回滚只删表结构，不涉及"锁定不可逆"的应用层语义）。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2）**：涉及新迁移，**Haisu 需人工审阅迁移文件**后才可 approve commit。
- **Escalation**：若"锁定策略"的产品含义与本票假设（一次性锁定、无解锁端点）不符，或 Haisu 认为本票确实应该依赖 F0/RND-244 配置中心而非独立表 → `BLOCKED_NEEDS_HUMAN`，说明具体歧义，不要自行改回旧设计或猜测另一种语义。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/routers/users.py:33-38`（`require_role` 范式）、`app/db/models.py`（`TenantWecomConfig` 作为新表风格参考）。
2. `git pull` 确认 alembic head，写迁移。
3. 新增 `RetentionConfig` 模型 + `retention.py` router（GET/PUT）。
4. 挂载到 `main.py`。
5. 写测试覆盖 AC-1~AC-6（**AC-3 锁定不可逆是重点**）。
6. 同步 `test_http_contract.py` 与 `test_rnd280_rbac_scaffold.py`。
7. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假租户数据。
- 不扩大 Scope：不做清理任务本身、不做解锁端点、不做前端。
- 复用优先：鉴权范式仿 `users.py`，不重新发明。
- 证据优先，以 exit 0 / 测试通过为证。
