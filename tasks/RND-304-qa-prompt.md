[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-304 的 7 条 AC（重点验证幂等、租户隔离、未依赖 F0/RND-244）并产出带证据的 PASS/FAIL 判定。

# RND-304 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-304「A9-3 标记首次完成」｜风险等级 R1
- 本票是设计反转后的版本——**不再依赖 F0/RND-244 配置中心**，改为 `Tenant` 表直接加列。若交付物仍走 F0 KV 路线，说明用的是旧稿，判 `BLOCKED` 并说明需要重新按当前设计执行。

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 默认未完成
- 证据：全新租户（从未调用 `complete`）→ `GET /api/onboarding/status` 返回 `first_run: true`。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 完成后置位
- 证据：`POST /api/onboarding/complete` 后 `GET` 返回 `first_run: false`。
- 判定：符合 = PASS。

### AC-3 — 幂等（关键）
- 证据：测试连续调用两次 `POST /api/onboarding/complete`，查库断言 `onboarding_completed_at` 两次调用后**值相同**（不是第二次调用把时间戳往后推）。
- 判定：真正幂等 = PASS。**若第二次调用覆盖了时间戳 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, major）**——虽然功能上 `first_run` 结果不变，但审计/排查时间线会被污染。

### AC-4 — 租户隔离
- 证据：跨租户反例测试——租户 A 完成向导后，租户 B 的 `GET` 仍返回 `first_run: true`。代码审阅：`tenant_id` 仅来自 `require_role()`，不接受请求参数。
- 判定：符合 = PASS。任一缺失 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-5 — 鉴权分级
- 证据：`GET` 任意角色可读；`POST` 非 admin/owner 角色 → 403，有测试覆盖。
- 判定：分级正确 = PASS。

### AC-6 — 迁移可逆
- 证据：`alembic upgrade head` 与 `alembic downgrade -1` 均可执行；`alembic check` 无 drift。
- 判定：符合 = PASS。

### AC-7 — 契约同步 + RBAC 同步 + 回归
- 判定：`test_http_contract.py`（route_count 当前基线 +2）与 `test_rnd280_rbac_scaffold.py`（白名单含 `onboarding.py`）均已同步 = PASS；任一未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查（必查）
1. **未依赖 F0/RND-244（关键）**：`grep -rn "config_service\|get_config\|set_config\|from app.config" backend/app/routers/onboarding.py` **应无命中**。若有命中，说明开发 agent 走的是旧的 F0 KV 设计——按当前设计判 `BLOCKED`，不是直接判 FAIL（这可能是开发 agent 拿到了过期的提示词版本，需要人工确认后重跑，而不是判定实现质量有问题）。
2. **`Tenant` 表新增列**：`git diff -- backend/app/db/models.py` 应只看到 `Tenant.onboarding_completed_at` 一行新增，无其他改动。
3. **未新建多余表**：`backend/alembic/versions/` 中本票的迁移文件应只涉及给 `tenants` 表加列，不应新建独立的 onboarding 状态表（那会是过度设计——单一布尔状态不需要单独一张表）。
4. **未提供重置端点**：diff 中不应出现"取消完成"/"重新打开向导"相关的端点或参数。
5. **架构边界**：router 未 import `app.main`；service 层未 import `app.routers.*`。
6. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `alembic/versions/0029_*.py`（或实际顺延版本号）、`app/db/models.py`（仅新增 `onboarding_completed_at`）、`routers/onboarding.py`（新）、`schemas/onboarding.py`（新）、`main.py`（仅两行）、`tests/test_rnd304_onboarding_status.py`（新）、`tests/test_rnd280_rbac_scaffold.py`（白名单同步）、`tests/test_http_contract.py`（契约同步）。
   > 若 RND-319 同天并行推进，两票都会各自新增迁移文件（0028/0029）——先 `git status` 分离归因，只把本票的部分记在本票账上（见 `docs/ticket-autopilot-workflow.md` §3.4）。

## 附加检查（Security）
- 测试中使用固定假租户数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd304_onboarding_status.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m alembic check                                          # AC-6
grep -rn "config_service\|get_config\|set_config\|from app.config" backend/app/routers/onboarding.py   # 本项目专属检查 1：应无命中
ls backend/alembic/versions/ | tail -3
git diff -- backend/app/db/models.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-304-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：实际采用的存储方案（应为 `Tenant` 列；若不是需说明），迁移文件的实际版本号。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-3 若发现重复调用覆盖时间戳 → 直接 FAIL（major）**，不接受"结果反正一样"。
- **AC-4 若跨租户数据串了 → 直接 FAIL（blocker）**。
