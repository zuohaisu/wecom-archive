[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-318 的 8 条 AC（重点验证锁定后不可逆、未误依赖 F0 配置中心）并产出带证据的 PASS/FAIL 判定。

# RND-318 验收提示词（Acceptance / QA Prompt）

## ⚠️ 2026-07-31：本版取代 2026-07-30 的旧稿

旧版验收提示词假设留存配置存于 F0（RND-244）配置中心 KV，本版已改为独立数据表设计（理由见 `tasks/RND-318-dev-prompt.md` 开头）。**若交付物仍是 KV 存储路线，按旧假设走，不代表本版判 FAIL——先确认开发 agent 实际采用了哪种设计，用对应版本验收**；但若两种都不是（既没有独立表也没接 F0 KV，凭空造了别的存储）→ FAIL。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-318「C3-1 留存配置（表/租户设置）」｜风险等级 **R2**（新表 + 迁移，合规留存策略地基）

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 策略可配
- 证据：测试断言 `PUT` 后 `retention_configs` 表出现/更新对应行；`GET` 返回刚写入的值。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 未配置态可区分
- 证据：从未 `PUT` 过的租户 `GET` → `configured: false`，不是 404/500。
- 判定：符合 = PASS。

### AC-3 — 锁定后不可再改（关键）
- 证据：测试构造"先 `PUT lock=true`，再 `PUT` 不同 `retention_days`"，断言第二次调用返回 423，**且查库确认 `retention_days` 未被第二次调用改动**。
- 判定：锁定语义严格生效 = PASS。**若锁定后仍可被改（哪怕只是响应体显示新值但库里没变、或库里真的被改了）→ 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——这是合规数据的完整性防线。

### AC-4 — 输入校验
- 证据：`retention_days` 为 0、负数、3651 → 均 422，测试逐一覆盖边界。
- 判定：边界值测试齐全且行为正确 = PASS。

### AC-5 — 租户隔离
- 证据：跨租户反例测试——租户 A 写入的配置，租户 B 读取应看到 B 自己的（未配置）状态，不串数据。代码审阅：`tenant_id` 仅来自 `require_role()`，不接受请求参数。
- 判定：符合 = PASS。任一缺失 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-6 — 鉴权分级
- 证据：`GET` 任意角色可读；`PUT` 普通角色（非 admin/owner）→ 403，有测试覆盖。
- 判定：分级正确 = PASS。

### AC-7 — 迁移可逆
- 证据：`alembic upgrade head` 与 `alembic downgrade -1` 均可执行；`alembic check` 无 drift。
- 判定：符合 = PASS。

### AC-8 — 契约同步 + RBAC 同步 + 回归
- 判定：`test_http_contract.py`（route_count 当前基线 +2）与 `test_rnd280_rbac_scaffold.py`（白名单含 `retention.py`）均已同步 = PASS；任一未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`。`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查（必查）
1. **设计路线一致性**：确认开发 agent 实际采用的存储方案（独立表 vs F0 KV vs 其他）与其自己的 QA Summary 描述一致；若两者矛盾（比如建了表但 Summary 说存在 KV 里）→ 记 finding。
2. **未越界依赖 RND-244**：`grep -rn "config_service\|get_config\|set_config" backend/app/routers/retention.py` **不应有命中**（本设计不依赖配置中心）。若有命中，说明开发 agent 选择了 KV 路线而非本版假设的独立表——**不直接判 FAIL**，改用本文件开头说明的方式核实其正确性（是否真的有 F0 config_service 存在、行为是否符合"策略可配"的 AC 本质）。
3. **锁定不可撤销无后门**：确认代码中**没有**任何绕过锁定检查的路径（如管理员角色特殊豁免、内部 API）。
4. **架构边界**：router 未 import `app.main`；service 层未 import `app.routers.*`；`_FLAT_SERVICE_MODULES` 若改动需核实必要性。
5. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `alembic/versions/0027_retention_config.py`（新，或实际顺延版本号）、`app/db/models.py`（仅新增 `RetentionConfig`）、`routers/retention.py`（新）、`schemas/retention.py`（新）、`main.py`（仅两行）、`tests/test_rnd318_retention_config.py`（新）、`tests/test_rnd280_rbac_scaffold.py`（白名单同步）、`tests/test_http_contract.py`（契约同步）。

## 附加检查（Security）
- 测试中使用固定假租户数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd318_retention_config.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m alembic check                                          # AC-7
ls backend/alembic/versions/ | tail -3
git diff -- backend/app/db/models.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-318-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：实际采用的存储方案（独立表/F0 KV/其他）、表结构或 KV 键名（供 RND-301/RND-319 对接）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-3 若锁定后配置仍可被改 → 直接 FAIL（blocker）**，不接受"这种情况很少见"。
- **AC-5 若跨租户数据串了 → 直接 FAIL（blocker）**。
