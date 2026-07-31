[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-307 的 6 条 AC（重点验证真的是"每租户"聚合而非全平台合计复制、且真复用 UsageService）并产出带证据的 PASS/FAIL 判定。

# RND-307 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-307「B1-3 跨租户聚合」｜风险等级 R1
- **本票最容易踩的坑：把 `UsageService` 的 `tenant_id=None`（全平台合计）误用成"每租户"结果。AC-1 从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 每租户聚合（关键）
- 证据：测试构造 **2 个租户**，各自有不同数量的消息/存储数据，断言 `GET /tenants/usage` 返回的两条结果**数值不同**且分别正确对应各自租户（不是同一份数字复制两遍、也不是相加成一个总数摆在两条里）。
- 代码审阅：确认实现是**遍历租户逐个调用** `count_messages(db, tenant_id=t.id)` 等函数，而不是调用一次 `tenant_id=None` 后把合计值塞进每条记录。
- 判定：真正逐租户区分 = PASS。**若发现结果是全平台合计值的复制、或多租户测试数据没有区分度（无法验证是否串数据）→ 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: major）**。

### AC-2 — 复用而非重写
- 证据：`grep -n "count_messages\|sum_storage\|count_monitored_employees\|sync_health" backend/app/routers/platform.py` 应有命中。
- 判定：真复用既有 `UsageService` 函数 = PASS。**平行重写聚合 SQL → FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-3 — 空态
- 判定：无租户 → 空列表不报错 = PASS；否则 FAIL。

### AC-4 — 鉴权
- 判定：无凭据/错凭据 → 401 = PASS。

### AC-5 — 不泄露内容
- 证据：响应体逐字段核对，只含聚合数字（消息数/存储字节/员工数/同步健康），不含消息正文、媒体 URL、任何 PII 明细。
- 判定：符合 = PASS。出现内容级字段 → FAIL（`SCOPE_VIOLATION`）。

### AC-6 — 契约同步 + 回归
- 判定：`test_http_contract.py` 全绿 = PASS；未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查（必查）
1. **未改 `UsageService`**：`git diff --stat -- backend/app/services/usageservice.py` 必须无输出。
2. **N+1 已知且已注释**：若实现是遍历逐租户调用（预期如此），确认代码里有简短注释说明"首期 N+1 可接受，租户数增长需优化"（非强制 FAIL 项，缺注释记 minor finding）。
3. **架构边界**：router 未 import `app.main`；service 层未 import `app.routers.*`。
4. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/platform.py`（仅新增 `GET /tenants/usage`）、schema 文件、`tests/test_rnd307_cross_tenant_usage.py`（新）、`tests/test_http_contract.py`。
   > `platform.py` 是本波次共享文件（RND-310/312/313/314 也会改它）——先 `git status`/`git diff` 分离归因（见 `docs/ticket-autopilot-workflow.md` §3.4）。

## 附加检查（Security）
- 测试中使用固定假租户/消息数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd307_cross_tenant_usage.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "count_messages\|sum_storage\|count_monitored_employees\|sync_health" backend/app/routers/platform.py
git diff --stat -- backend/app/services/usageservice.py    # 应无输出
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-307-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-1 若无法证明结果是真正逐租户区分（而非合计复制） → 直接 FAIL（major）**，不接受"看起来数值合理"这类未经验证的断言，必须有能区分两个租户的测试数据。
