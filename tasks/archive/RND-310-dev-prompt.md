[Goal check] This work advances 开发（Development） by 交付平台总控台的租户启停端点，写入既有审计基础设施，为平台超管提供收紧/恢复某租户访问的手段。

# RND-310 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-310 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-310「B1-4 租户启停」｜父 Epic RND-275（B1 平台总控台）
- 优先级：Medium｜风险等级：**R2**（可让一个租户的所有用户瞬间失去访问，爆炸半径是整租户）｜milestone：R5 · 云商业化前台

## 背景与项目现状（已实地核实）

B1-2（<issue>RND-305</issue>，跨租户鉴权作用域）**已 Done 并已上线**：`app/auth.py:207` 的 `require_platform_admin`。同文件 `app/auth.py:238` 还有 `require_platform_tenant_scope`（**本票不直接复用它**——那个依赖是给"以 query 参数 `tenant_id` 显式指定目标租户 + 自动写审计"的场景用的；本票是 path 参数 `{id}`，签名不同，见下）。

`Tenant` 模型（`app/db/models.py:54` 附近）已有 `is_active` 列（`Boolean`，默认 `True`）——**本票只是把它接上一个 PATCH 端点，不需要新迁移**。

审计基础设施已就绪：`app/audit.py:54` 的 `write_audit(db, *, tenant_id, action, object_type, admin_user_id=None, object_id=None, detail=None)`，fail-safe，**直接调用，不要重造**。

**❗ 本项目高频踩坑：**
- **`platform.py` 是本波次共享文件**：RND-307/312/313/314 也会往这个文件加端点。**开始前先 `git diff`/`git status` 看清哪些兄弟票的端点已落地**，在文件末尾追加自己的函数，不要改动别的票已经写好的函数。`test_http_contract.py` 的 `route_count` 用**当前实际值** +1 回填。
- **不要与 `TenantWecomConfig.is_active` 混淆**：`Tenant.is_active`（本票要改的）和 `TenantWecomConfig.is_active`（连接配置是否启用，RND-311 已用）是**两个不同字段**，语义不同——租户被停用应视为"整租户不可访问"，比连接配置停用更上层。不要误改成 `TenantWecomConfig.is_active`。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个平台超管专用端点，可启停某租户（`Tenant.is_active`），每次变更强制写审计，租户被停用后其所有租户内 API 应拒绝访问（若既有 `require_role` 尚未检查 `Tenant.is_active`，见下方 Escalation）。

## 范围边界

**In scope：**
1. `backend/app/routers/platform.py` **新增** `PATCH /tenants/{tenant_id}`：
   - 鉴权：`require_platform_admin`。
   - 请求体：`{"is_active": bool}`。
   - 修改目标租户的 `Tenant.is_active`；**必须**调用 `write_audit(...)` 记录「哪个超管、对哪个租户、改成了什么状态」（`action` 建议 `platform.tenant_activated` / `platform.tenant_deactivated`，视 `is_active` 取值二选一，或统一 `platform.tenant_status_changed` + `detail` 里带 `{"is_active": bool}`——**任选其一但要在代码里写清楚**）。
   - 目标租户不存在 → 404。
2. 测试：`backend/tests/test_rnd310_tenant_activation.py`。

**Out of scope（显式非目标）：**
- **不改** `require_role()` 或任何租户内 API 去检查 `Tenant.is_active`——那是一个独立的、影响面更大的改动（需要确认所有租户内端点在停用后确实拒绝访问），不在本票范围。本票只交付"改状态 + 写审计"的原语；若产品需要"停用后租户内 API 立即拒绝"，那是下游票的事。**若这一点在验收时产生歧义，按 Escalation 处理，不要自行扩大范围去改 `require_role`。**
- 不做租户创建（<issue>RND-311</issue>）/ 列表（<issue>RND-314</issue>）/ 连通性自检（<issue>RND-312</issue>）/ 跨租户聚合（<issue>RND-307</issue>）。

**本工单拥有的文件（只许写这些，`platform.py` 为共享追加）：**
- `backend/app/routers/platform.py` —— **仅新增** `PATCH /tenants/{tenant_id}` 及其依赖
- `backend/app/schemas/tenant_provision.py`（或复用兄弟票已建的 schema 文件）—— 仅新增启停相关 schema
- `backend/tests/test_rnd310_tenant_activation.py`（新）
- `backend/tests/test_http_contract.py` —— 契约同步

**只读、绝不可写：** `app/audit.py`（只调用 `write_audit`）、`app/db/models.py`、`app/auth.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件（例如为了让停用生效必须改 `require_role`）→ **停止**，`BLOCKED_NEEDS_HUMAN`，不要顺手改。

## 验收标准（Acceptance Criteria）

- **AC-1 启停可用**：`PATCH /tenants/{id}` 传 `{"is_active": false}` → 目标租户 `Tenant.is_active` 变为 `false`；传 `true` 可恢复。
- **AC-2 强制审计（关键）**：每次调用产生一条 `AuditLog`，经 `write_audit` 写入（非自造插入），含超管身份 + 目标 `tenant_id` + 变更后的状态。须有测试。
- **AC-3 目标不存在 → 404**：传入不存在的 `tenant_id` → 404，不是 500 / 静默成功。
- **AC-4 鉴权**：无凭据 / 错凭据 / 非平台超管会话 → 401。
- **AC-5 不越界改 `require_role`**：`git diff --stat -- backend/app/auth.py` **必须无输出**——本票不改动租户内鉴权行为。
- **AC-6 契约同步**：`test_http_contract.py` 已同步（route_count 当前基线 +1）。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；`create_tenant`（RND-311）既有测试全绿。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd310_tenant_activation.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "write_audit" backend/app/routers/platform.py    # AC-2
git diff --stat -- backend/app/auth.py                    # AC-5：必须无输出
git diff -- backend/app/routers/platform.py                # 人工核对：只新增，未碰兄弟票端点
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-305（B1-2）**已 Done 并已核实落地**（`backend/app/auth.py:207`）。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明「租户停用后是否已联动影响租户内 API 访问」的当前真实状态（供后续跟进票参考，见 Out of scope 说明）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：本票只改状态位，不联动 `require_role`——若产品预期"停用=租户内所有 API 立即拒绝"，这里存在**认知落差**。已在 Out of scope 显式声明，QA Summary 需如实说明现状，交给 Haisu 判断是否需要下游票。
- 风险 2：审计写入被遗漏——由 AC-2 防守，`write_audit` 本身 fail-safe，不要再包一层吞异常。
- 回滚：纯新增，`git checkout -- backend/app/routers/platform.py` 即可；无迁移（`is_active` 列已存在）。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2）**：本票可让整租户失去访问能力，**建议 Haisu 人工过一遍再 approve commit**（不是"测试绿就放行"）。
- **Escalation**：若发现「停用后租户内 API 是否应立即拒绝」这一点在实现中无法回避（例如现有测试隐含要求联动）→ `BLOCKED_NEEDS_HUMAN`，说明具体冲突点，**不要**为了让测试通过而改动 `require_role`。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/routers/platform.py`（先 `git diff`/`git status` 看兄弟票是否已落地）、`app/db/models.py:54`（`Tenant.is_active`）、`app/auth.py:207`（`require_platform_admin`）、`app/audit.py:54`（`write_audit`）。
2. 新增 `PATCH /tenants/{tenant_id}` + 请求/响应 schema。
3. 调用 `write_audit` 记录变更。
4. 写测试覆盖 AC-1~AC-4。
5. 同步 `test_http_contract.py`。
6. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假租户数据。
- 不扩大 Scope：不改 `require_role`、不做联动生效逻辑。
- 复用优先：`write_audit`/`require_platform_admin` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
