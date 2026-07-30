[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-313 的 7 条 AC（重点验证真复用 RND-303 的 _create_pending_invite、邀请失败不回滚租户创建）并产出带证据的 PASS/FAIL 判定。

# RND-313 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-313「B2-3 激活邮件（建 AdminUser 邀请）」｜风险等级 R1
- 本票依赖 RND-303 提炼的 `_create_pending_invite`——**若开发 agent 在该函数不存在的情况下自己重写了一套邀请创建逻辑，是本票最该抓的问题**。

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-0 — 前置核实
- 证据：`grep -n "_create_pending_invite" backend/app/routers/auth.py` **应有命中**（RND-303 应已落地）。
- 判定：有命中 = 继续后续检查。**若无命中但本票判定不是 `BLOCKED`（即开发 agent 绕过了前置检查径自实现）→ 直接 FAIL（`SCOPE_VIOLATION`, blocker）**——这说明开发 agent 违反了 dev prompt 明确写的"不存在则停止上报"的硬约束。

### AC-1 — 创建即发邀请
- 证据：测试断言 `POST /tenants` 带 `owner_email` → `AdminUser`（`role="owner"`, `invite_status="pending"`）被创建；`send_invite_email` 被调用一次，参数含目标邮箱。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 复用而非重写（关键）
- 证据：`grep -n "_create_pending_invite" backend/app/routers/platform.py` 应有命中。
- 代码审阅：`create_tenant` 中发邀请这一步**只是**调用该函数，没有内联重写角色校验/去重/token 生成/发邮件任何一步。
- 判定：真复用 = PASS。**平行重写 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-3 — 邀请失败不回滚租户创建（关键）
- 证据：测试 mock `send_invite_email`（或 `_create_pending_invite`）抛异常 → 断言 `Tenant` 与 `TenantWecomConfig` 仍成功创建（DB 查询验证），响应体 `owner_invite_sent=false`，HTTP 状态码仍是 201（不是 500）。
- 代码审阅：`db.commit()` 提交租户数据的时点必须**先于**调用邀请创建逻辑。
- 判定：租户创建与邀请发送真正解耦 = PASS。**若邀请失败导致租户创建被回滚或整体 500 → FAIL（`IMPLEMENTATION_DEFECT`, major）**——运营视角这会让平台超管以为整个开通失败，实际上租户已经半创建，造成数据/认知不一致。

### AC-4 — `create_tenant` 既有行为零回归
- 证据：`test_rnd311_tenant_provision.py` 全绿；`TenantProvisionOut` 原有字段（`tenant_id`/`tenant_name`/`tenant_slug`/`config_id`/`corp_id`/`agent_id`/`is_active`）未被删除或改名，只新增。
- 判定：PASS/FAIL 按是否符合。

### AC-5 — 契约同步
- 判定：若响应形状变化，`test_http_contract.py` 的 snapshot 已同步 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。

### AC-6 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **测试未发真实邮件**：`send_invite_email` 在测试中必须被 mock。真实调用 → FAIL（`IMPLEMENTATION_DEFECT`, major）。
2. **未改 `auth.py`**：`git diff --stat -- backend/app/routers/auth.py` 应无输出（本票只 import，不修改）。
3. **未越界做多首位管理员**：diff 中不应出现"创建时传多个 owner_email"的逻辑，本票明确只支持单个。
4. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/platform.py`（修改 `create_tenant`）、`schemas/tenant_provision.py`（新增字段）、`tests/test_rnd313_tenant_activation_email.py`（新）、`tests/test_rnd311_tenant_provision.py`（仅补字段，不改断言逻辑）、`tests/test_http_contract.py`。
   > `platform.py` 是本波次共享文件（RND-307/310/312/314 也会改它）——先 `git status`/`git diff` 分离归因，本票**修改** `create_tenant` 本身，与其余票"纯新增端点"不同，需格外确认没有和兄弟票的改动冲突覆盖（见 `docs/ticket-autopilot-workflow.md` §3.4）。

## 附加检查（Security）
- 测试中使用固定假邮箱，非真实客户/员工邮箱。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
grep -n "_create_pending_invite" backend/app/routers/auth.py    # AC-0
make verify
.venv/bin/python -m pytest backend/tests/test_rnd313_tenant_activation_email.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "_create_pending_invite" backend/app/routers/platform.py   # AC-2
git diff --stat -- backend/app/routers/auth.py                      # 应无输出
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-313-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若发现平行重写邀请创建逻辑 → 直接 FAIL（major）**。
- **AC-3 若邀请失败导致租户创建被回滚或 500 → 直接 FAIL（major）**，不接受"这种情况很少见"。
