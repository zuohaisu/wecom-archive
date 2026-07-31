[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-310 的 7 条 AC（重点验证强制审计与未越界改动 require_role）并产出带证据的 PASS/FAIL 判定。

# RND-310 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-310「B1-4 租户启停」｜风险等级 **R2**（可让整租户瞬间失去访问）
- **AC-2 与 AC-5 从严判定，不接受任何"应该没问题"式论证。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 启停可用
- 证据：测试断言 `PATCH /tenants/{id}` 可切换 `Tenant.is_active` 两个方向。
- 判定：功能齐全 = PASS。

### AC-2 — 强制审计（关键）
- 证据：`grep -n "write_audit" backend/app/routers/platform.py` 应有命中；测试断言调用后 `AuditLog` 新增一条，含超管身份 + 目标 `tenant_id`。
- 反模式：确认调用**没有**被额外 `try/except: pass` 包住。
- 判定：经 `write_audit` + 字段齐全 + 无吞异常 = PASS。**缺审计 → FAIL（`SECURITY_VIOLATION`, blocker）**——合规存档产品，租户启停必须留痕。

### AC-3 — 目标不存在 → 404
- 证据：测试传入不存在的 `tenant_id` → 404。
- 判定：符合 = PASS；500 或静默成功 → FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-4 — 鉴权
- 证据：无凭据/错凭据/非平台超管会话（如租户内 admin 会话）→ 均 401，三种情况均有用例。
- 判定：均正确拒绝 = PASS。

### AC-5 — 不越界改 `require_role`（关键）
- 证据：`git diff --stat -- backend/app/auth.py` **必须无输出**。
- 判定：无输出 = PASS。**有任何输出 → 直接 FAIL（`SCOPE_VIOLATION`, blocker）**——这会波及全站租户内鉴权行为，且超出本票 In scope（本票只交付状态位+审计，不做"停用后租户内 API 联动拒绝"）。

### AC-6 — 契约同步
- 判定：`test_http_contract.py` 全绿 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；`test_rnd311_tenant_provision.py`（`create_tenant`）全绿。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **未做"停用联动"**：diff 中不应出现对 `require_role()` 或任何租户内 API 鉴权路径的改动。这是本票明确的 Out of scope，出现即 `SCOPE_VIOLATION`。
2. **未混淆两个 `is_active`**：确认改的是 `Tenant.is_active`，不是 `TenantWecomConfig.is_active`（后者是 RND-311 的连接配置状态，语义不同）。
3. **未新增迁移**：`backend/alembic/versions/` 无新文件——`Tenant.is_active` 列已存在，本票不需要迁移。有新迁移文件 → 记 finding，核实是否越界。
4. **QA Summary 是否如实说明现状**：开发 agent 的 QA Summary 应说明"租户停用后是否已联动影响租户内 API 访问"的真实现状（预期答案：未联动，这是已知的 Out of scope）。若 QA Summary 含糊其辞或声称"已联动"但代码里看不到证据 → 记 finding。
5. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/platform.py`（仅新增 `PATCH /tenants/{tenant_id}`）、schema 文件、`tests/test_rnd310_tenant_activation.py`（新）、`tests/test_http_contract.py`。
   > `platform.py` 是本波次共享文件（RND-307/312/313/314 也会改它）——先 `git status`/`git diff` 分离归因（见 `docs/ticket-autopilot-workflow.md` §3.4）。

## 附加检查（Security）
- 测试中使用固定假租户数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd310_tenant_activation.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "write_audit" backend/app/routers/platform.py       # AC-2
git diff --stat -- backend/app/auth.py                       # AC-5：必须无输出
git status --porcelain
ls backend/alembic/versions/ | tail -5                        # 本票预期无新迁移
git log origin/main..HEAD                                     # 必须无输出
```

## 产出
写入 `tasks/RND-310-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：审计 `action` 字段的具体取值、「停用联动」现状说明（供后续跟进票参考）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若无审计调用 → 直接 FAIL（blocker）**，不接受"逻辑上不会遗漏"。
- **AC-5 若 `auth.py` 有任何改动 → 直接 FAIL（blocker）**，那会波及全站租户内 API。
