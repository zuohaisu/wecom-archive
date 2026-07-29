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

### AC-4 — owner_display_name 经既有函数产出
- 证据：`grep -n "resolve_person_display_name\|from app.display_names import" backend/app/routers/external_contacts.py` 应有命中。
- 判定：确实调用 = PASS。若是本票自己写的展示名拼接逻辑 → FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-5 — 分页
- 证据：测试断言分页参数存在且生效；`offset` 超出总数时返回空列表而非报错。
- 判定：两项均有测试 = PASS。

### AC-6 — 租户隔离（**关键**）
- 证据：测试构造两个不同 `tenant_id` 的 `ExternalContact` 行，以租户 A 的鉴权上下文请求，断言响应**不包含**租户 B 的任何行。
- 代码审阅：确认 `tenant_id` 过滤条件存在于查询里，且**只能来自鉴权上下文**（`grep` 路由签名，确认没有把 `tenant_id` 作为请求参数接收）。
- 判定：隔离测试存在且通过 + `tenant_id` 不接受外部输入 = PASS。**任一缺失 → FAIL（`SECURITY_VIOLATION`, severity: blocker）**——这是客户 PII 跨租户泄露的直接风险。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **架构边界**：`routers/external_contacts.py` 未 import `app.main`；未反向 import 其他 router。
2. **未改模型/sync/既有查询帮助函数**：`git diff --stat -- backend/app/db/models.py backend/app/services/external_contact_sync.py backend/app/db/external_contacts.py backend/app/display_names.py` **必须全无输出**。
3. **未改现有 contacts 端点**：`git diff --stat -- backend/app/routers/conversations.py backend/app/routers/search.py` **必须无输出**（`GET /api/contacts`、`GET /api/search/contacts` 保持原样，本票是新增而非替换）。
4. **未越界做详情/导出**：diff 中不得出现详情时间线（A4-3/RND-289）或导出相关代码。
5. **无新迁移**：`backend/alembic/versions/` 无新文件（本票不改模型）。
6. **文件所有权**：`git status --porcelain` 改动应限于 `schemas/external_contact.py`（新）、`routers/external_contacts.py`（新）、`main.py`（仅注册）、`tests/test_external_contacts_api.py`（新）。清单外文件 → FAIL（`SCOPE_VIOLATION`）。

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
