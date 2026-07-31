[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-309 的 7 条 AC（重点验证无重复审计、真复用 require_platform_tenant_scope、未误建审批表）并产出带证据的 PASS/FAIL 判定。

# RND-309 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-309「B1-5 内容访问申请 gate」｜风险等级 **R2**（合规访问留痕核心一环）
- **AC-1/AC-5 从严判定**：重复审计记录会污染合规日志，比缺审计更隐蔽也同样严重。

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 申请记入审计
- 证据：测试断言调用后 `AuditLog` 新增一条，`action` 匹配 `PLATFORM_TENANT_ACCESS_ACTION`，含超管身份 + 目标 `tenant_id`。
- 判定：符合 = PASS。

### AC-2 — 默认不可读内容
- 证据：响应体逐字段核对，不含消息正文/媒体 URL/联系人明细；`granted` 字段恒为 `false`。
- 判定：符合 = PASS。出现任何内容级字段 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-3 — 复用而非重写（关键）
- 证据：`grep -n "require_platform_tenant_scope" backend/app/routers/platform_access.py` 应有命中。
- 代码审阅：确认端点函数体**没有**手写角色校验或审计写入逻辑，全部委托给依赖注入完成。
- 判定：真复用 = PASS。**平行重写鉴权/审计逻辑 → FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-4 — 鉴权
- 证据：无凭据/错凭据/非平台超管会话 → 401；缺 `tenant_id` → 422。测试覆盖齐全。
- 判定：符合 = PASS。

### AC-5 — 无重复审计（关键）
- 证据：`grep -n "write_audit" backend/app/routers/platform_access.py` —— 若有命中，**必须**只出现在 import 语句或注释里，**端点函数体内不应再调用一次** `write_audit`（`require_platform_tenant_scope` 内部已经调用过）。
- 测试验证：调用一次端点后，查询 `AuditLog` 表，**恰好新增 1 条**，不是 2 条。
- 判定：单条审计 = PASS。**发现端点函数体内又手写一次 `write_audit` 导致重复记录 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: major）**——这会让合规日志里每次访问显示两条几乎相同的记录，误导后续审查。

### AC-6 — 未新建审批表
- 证据：`git diff --stat -- backend/app/db/models.py` 应无输出；`ls backend/alembic/versions/` 无新文件。
- 判定：符合 = PASS。**若发现新建了类似 RND-316 的审批 token 表 → FAIL（`SCOPE_VIOLATION`, major）**——本票明确是轻量审计记录，不是完整审批工作流。

### AC-7 — 契约同步 + 回归
- 判定：`test_http_contract.py` 全绿 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查（必查）
1. **未碰共享文件 `platform.py`**：`git status --porcelain` 中不应出现 `routers/platform.py` 的改动——本票应新建独立的 `platform_access.py`。出现 → 记 finding，核实是否与本波次其他票（RND-307/310/312/313/314）冲突。
2. **`main.py` 改动最小**：`git diff -- backend/app/main.py` 应只有 1 行 import + 1 行 `include_router`，无其他改动。
3. **架构边界**：新 router 未 import `app.main` 之外的业务路由。
4. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/platform_access.py`（新）、schema 文件（新）、`main.py`（仅两行）、`tests/test_rnd309_content_access_request.py`（新）、`tests/test_http_contract.py`。

## 附加检查（Security）
- 测试中使用固定假租户数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd309_content_access_request.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "require_platform_tenant_scope" backend/app/routers/platform_access.py    # AC-3
grep -n "write_audit" backend/app/routers/platform_access.py                       # AC-5：函数体内不应命中
git diff --stat -- backend/app/db/models.py                                        # AC-6：必须无输出
ls backend/alembic/versions/ | tail -3                                             # AC-6：无新文件
git diff -- backend/app/main.py                                                     # 应只有 2 行改动
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-309-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-5 若发现重复审计记录 → 直接 FAIL（major）**，不接受"多一条也没什么大碍"。
- **AC-6 若发现误建审批表 → 直接 FAIL（major）**，那是范围蔓延。
