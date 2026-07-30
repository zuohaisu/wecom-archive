[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-288 的 7 条 AC（重点验证标签筛选精确匹配与跨租户隔离）并产出带证据的 PASS/FAIL 判定。

# RND-288 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-288「A4-2 外部联系人列表/筛选 API」｜风险等级 R1｜automated 验收
- **本票 blocks RND-330，且响应体含客户 PII（外部联系人姓名/企业/标签）。AC-3（标签精确匹配）与 AC-6（跨租户隔离）从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 真实数据
- 证据：测试断言列表数据来自真实 DB 查询（构造 `ExternalContact` 行后能查到），非硬编码假数据。
- 判定：真实查询驱动 = PASS。

### AC-2 — 三种筛选生效
- 证据：`company`、`tags`、`owner_wecom_userid` 各自有独立测试用例，构造多条不同属性的记录，断言筛选后只返回匹配的行。
- 判定：三种筛选各有测试且通过 = PASS。缺任一 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-3 — 标签筛选精确匹配（**关键**）
- 证据：**必须存在**一条测试，构造两条记录——一条 `tags` 含 `"VIP"`，另一条 `tags` 含 `"VIP2026"`（或类似的、`"VIP"` 是其子串的标签）——筛选 `tags=VIP` 时**只返回前者**，不误命中后者。
- 代码审阅：`grep -n "LIKE\|ilike" backend/app/routers/external_contacts.py`，确认标签匹配不是裸 `%value%` 子串匹配，而是带边界的模式（如 JSON 带引号匹配）或反序列化后的精确比对。
- 判定：反例测试存在且通过 + 代码确认非裸子串匹配 = PASS。**缺此反例测试，或测试证明确实误命中 → FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——标签筛选结果不可信，客户会拿着错的筛选结果做决策。

### AC-4 — owner_display_name 经既有函数产出**且参数传对**（2026-07-30 加强，首轮在此漏判）
- 证据 A（调用存在）：`grep -n "resolve_person_display_name\|from app.display_names import" backend/app/routers/external_contacts.py` 应有命中。
- **证据 B（参数正确——本条是重点，首轮 QA 只查了 A 就判 PASS，漏掉了可能的实参错误）**：
  签名是 `resolve_person_display_name(raw_id: Optional[str], name: Optional[str] = None)`，第二参数是**展示名**（取自 `Contact.name`），**不是 `tenant_id`**。函数逻辑是「`name` 非空则直接返回 `name`」。
  **逐字读实参**：若代码写成 `resolve_person_display_name(owner_wecom_userid, tenant_id)`，则每行 `owner_display_name` 都会等于**租户 UUID**——不报错、页面上看着"有值"、但全是错的。
- 证据 C（行为断言）：必须有测试证明——有对应 `Contact`（`name="张三"`）时 `owner_display_name == "张三"`；无对应 `Contact` 时回退为 `owner_wecom_userid`；并且**断言 `owner_display_name != tenant_id`**。
- 判定：A + B + C 全部满足 = PASS。**只满足 A（"确实调用了函数"）→ 不足以判 PASS**，必须逐字核对实参。若第二参数传的是 `tenant_id` 或其他非展示名的值 → FAIL（`IMPLEMENTATION_DEFECT`, severity: major）。若缺证据 C 的行为测试 → FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-5 — 分页
- 证据：测试断言分页参数存在且生效；`offset` 超出总数时返回空列表而非报错。
- 判定：两项均有测试 = PASS。

### AC-6 — 租户隔离（**关键**）
- 证据：测试构造两个不同 `tenant_id` 的 `ExternalContact` 行，以租户 A 的鉴权上下文请求，断言响应**不包含**租户 B 的任何行。
- 代码审阅：确认 `tenant_id` 过滤条件存在于查询里，且**只能来自鉴权上下文**（`grep` 路由签名，确认没有把 `tenant_id` 作为请求参数接收）。
- 判定：隔离测试存在且通过 + `tenant_id` 不接受外部输入 = PASS。**任一缺失 → FAIL（`SECURITY_VIOLATION`, severity: blocker）**——这是客户 PII 跨租户泄露的直接风险。

### AC-7 — 回归，含契约测试同步（2026-07-30 重写）
- **背景（首轮在此判错了remedy）**：本票新增 1 个路由，因此**必须**同步两个契约测试，这是强制随附改动、不是别人的活（详见 `docs/ticket-autopilot-workflow.md` §3.3）：
  - `test_http_contract.py`：`route_count` 硬编码基线 + expected path 集合 + snapshot 列表。
  - `test_rnd280_rbac_scaffold.py`：第 77-86 行闭世界白名单——`external_contacts.py` 用了 `require_role()`，不加进白名单**必然**失败。
- 证据：`make verify` exit 0；上述两个契约测试均已同步且通过；`test_architecture_boundary.py` 通过。
- **判定**：
  - 全绿 = PASS。
  - **新增了路由但没同步契约测试** → FAIL（`REGRESSION`）。**不要**建议"另开一张契约维护票"——那会让 `main` 在两票之间持续红灯，违反「main 始终可部署」。这是本票的实现债，退回开发 agent 补齐即可，`recommended_next_state: FIXING`（**不是** `BLOCKED_NEEDS_HUMAN`）。
  - **`route_count` 被写死成某个具体数字而非"当前基线 +1"** → 记 minor finding（项目纪律：`docs/ticket-autopilot-workflow.md` §3.3 与 MEMORY 均要求读当前值再加 delta）。

### ⚠️ AC-7 归因纪律：共享工作树上 `make verify` 会包含他票改动
本项目并行方案是「直接在 main 上、文件所有权错峰」，**多个 agent 的未提交改动同时存在于同一工作树**；`make verify` 跑整棵树，不是本票增量。

**首轮实例（真实教训）**：QA 报告 `+3 routes`，其中 `/admin/users`、`/admin/audit-logs` 是 **RND-327/328** 的路由（RND-326 交付的 stub router 本身注册 0 个路由，已核实），**不是** RND-288 的。RND-288 实际只新增 **1** 个路由。整份归因因此偏了。

**你必须先分离归因**：
```bash
git status --porcelain    # 列出全部改动，逐个判断归属哪张票
```
- 只把**属于本票拥有文件**的失败记在本票账上。
- 他票在途改动导致的失败 → 写进 `notes` 说明，**不计入本票 FAIL**。
- 若无法分离（工作树太脏） → `verdict: BLOCKED`，说明"需要在干净树或隔离环境复验"，**不要**硬判 PASS 或 FAIL。

## 本项目专属检查（必查）
1. **架构边界**：`routers/external_contacts.py` 未 import `app.main`；未反向 import 其他 router。
2. **未改模型/sync/既有查询帮助函数**：`git diff --stat -- backend/app/db/models.py backend/app/services/external_contact_sync.py backend/app/db/external_contacts.py backend/app/display_names.py` **必须全无输出**。
3. **未改现有 contacts 端点**：`git diff --stat -- backend/app/routers/conversations.py backend/app/routers/search.py` **必须无输出**（`GET /api/contacts`、`GET /api/search/contacts` 保持原样，本票是新增而非替换）。
4. **未越界做详情/导出**：diff 中不得出现详情时间线（A4-3/RND-289）或导出相关代码。
5. **无新迁移**：`backend/alembic/versions/` 无新文件（本票不改模型）。
6. **文件所有权（2026-07-30 更新，含契约测试）**：`git status --porcelain` 中**属于本票**的改动应限于：
   - `schemas/external_contact.py`（新）
   - `routers/external_contacts.py`（新）
   - `main.py`（仅注册）
   - `tests/test_external_contacts_api.py`（新）
   - `tests/test_http_contract.py`（**强制随附**，仅 route_count + 本票新路由条目）
   - `tests/test_rnd280_rbac_scaffold.py`（**强制随附**，仅白名单加本票 router）

   清单外文件 → FAIL（`SCOPE_VIOLATION`）。但注意共享工作树可能含他票改动（见上方归因纪律），先分离再判。
   **反向检查**：两个契约测试的 diff 不得放松/删除他票的既有断言——只能追加本票条目。若发现删改他票断言 → FAIL（`SCOPE_VIOLATION`, blocker），那会让契约测试失去对他票的保护。

## 附加检查（Security）
- 响应体不泄露除本票明确定义字段外的其他 `ExternalContact` 内部字段（如 `id` 主键是否该暴露，按 schema 定义核对，不多不少）。
- 测试中无真实客户姓名/企业/联系方式，一律固定假数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_external_contacts_api.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/db/models.py backend/app/services/external_contact_sync.py backend/app/db/external_contacts.py backend/app/display_names.py backend/app/routers/conversations.py backend/app/routers/search.py   # 必须全无输出
grep -n "LIKE\|ilike" backend/app/routers/external_contacts.py   # AC-3 人工审阅匹配方式
grep -n "resolve_person_display_name" backend/app/routers/external_contacts.py   # AC-4
git status --porcelain
git log origin/main..HEAD                                          # 必须无输出
```

## 产出
写入 `tasks/RND-288-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：端点最终响应的确切字段名列表（供 RND-330 对接参考）。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **AC-3 若无标签子串反例测试 → 直接 FAIL**，不接受"筛选看起来对"。
- **AC-6 若无跨租户隔离测试 → 直接 FAIL**，这是 PII 数据，不接受"代码里有 tenant_id 过滤应该没问题"这种未经验证的断言。
