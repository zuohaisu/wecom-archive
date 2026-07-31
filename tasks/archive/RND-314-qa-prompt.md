[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-314 的 6 条 AC（重点验证响应零泄露密钥字段）并产出带证据的 PASS/FAIL 判定。

# RND-314 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-314「B2-4 已开通租户列表」｜风险等级 R1
- 响应包含所有租户的配置摘要——**零密钥泄露是本票最高风险项**。

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 列表可用
- 证据：测试断言 `GET /tenants` 返回全部租户摘要，字段齐全。
- 判定：功能齐全 = PASS。

### AC-2 — 零泄露（关键）
- 证据：测试对响应 JSON 序列化后的**原始字符串**做断言（不是只检查 Pydantic model 字段名），确认不包含 `app_secret`/`private_key_encrypted` 字样，也不包含任何解密后的密钥值。
- 代码审阅：`response_model` 是否显式白名单字段，而不是 `return tenant.__dict__` / `jsonable_encoder(tenant)` 这类会带出全部 ORM 属性的写法。
- 判定：显式白名单 + 测试验证响应体不含密钥 = PASS。**任一密钥字段出现在响应中 → 直接 FAIL（`SECURITY_VIOLATION`, blocker）。**

### AC-3 — 鉴权
- 证据：无凭据 / 错凭据 → 401；测试覆盖两种情况。
- 判定：均返回 401 = PASS。

### AC-4 — 空态
- 证据：无租户数据时返回空列表而非报错。
- 判定：PASS/FAIL 按是否符合。

### AC-5 — 契约同步
- 判定：`test_http_contract.py` 全绿 = PASS；新增路由未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。

### AC-6 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；`test_rnd311_tenant_provision.py`（`create_tenant`）全绿。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **未改 `create_tenant`**：`git diff -- backend/app/routers/platform.py` 中 `create_tenant` 函数体逐行未变，只有新增内容。
2. **未越界做启停/自检/聚合**：diff 中不得出现 `PATCH /tenants/{id}`（RND-310）、连通性自检逻辑（RND-312）、跨租户聚合（RND-307）。出现 → FAIL（`SCOPE_VIOLATION`）。
3. **架构边界**：router 未 import `app.main`。
4. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/platform.py`（仅新增 `GET /tenants`）、schema 文件（仅新增列表相关）、`tests/test_rnd314_tenant_list.py`（新）、`tests/test_http_contract.py`（契约同步）。
   > `platform.py` 是本波次共享文件（RND-307/310/312/313 也会改它）——先 `git status`/`git diff` 分离归因，只把本票新增的部分记在本票账上（见 `docs/ticket-autopilot-workflow.md` §3.4）。

## 附加检查（Security）
- 测试中使用固定假租户数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd314_tenant_list.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/routers/platform.py    # AC-1 所有权核对：只新增 GET /tenants
git status --porcelain
git log origin/main..HEAD                       # 必须无输出
```

## 产出
写入 `tasks/RND-314-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若响应中出现任何密钥字段 → 直接 FAIL（blocker）**，不接受"反正前端不会显示"这类论证。
