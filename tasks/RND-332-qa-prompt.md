[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-332 的 7 条 AC（重点验证第二租户不冲突、单租户零回归、私钥不明文落盘）并产出带证据的 PASS/FAIL 判定。

# RND-332 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-332「D2-2 KeyProvider + KeyVersion 租户化 + 解密审计钩子」｜风险等级 **R2（迁移 + 密钥）**
- **本票是"能否接第二个云客户"的地基，且触及生产唯一在跑的解密链路。AC-2 与 AC-4 从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令、在**本地/测试库**跑迁移与测试。
- 不可以：改任何文件、commit、push、**对生产库执行任何操作**、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 双实现均可跑通
- 证据：`local_file` 与 `kms_envelope` 各有测试跑通取密钥 → 解密链路（`kms_envelope` 可用夹具模拟托管存储）。
- 判定：两条路径均有用例且通过 = PASS。只覆盖一条 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-2 — 第二租户不再冲突（**本票核心价值，重点查**）
- 证据：**必须存在**这条测试——租户 A 插入 `publickey_ver=1`，租户 B 也插入 `publickey_ver=1`，断言**两条都成功**。
- 代码审阅：确认唯一约束是**复合的 `(tenant_id, publickey_ver)`**，不是只保留 `tenant_id`、也不是仍然全局唯一。检查迁移与 `models.py` 两处一致。
- 背景：`publickey_ver` 原本全局唯一（`models.py:332`），每家企微 corp 都从 1 开始，第二个租户必然冲突。这条不成立，整张票就没有意义。
- 判定：双租户同版本号用例存在且通过 + 复合唯一确认 = PASS。**缺该用例 → 直接 FAIL（`INSUFFICIENT_TEST_COVERAGE`, blocker）。若约束退化成只有 `tenant_id`（同租户内版本号可重复）→ FAIL（`IMPLEMENTATION_DEFECT`, blocker）**——那会导致解密时选错密钥。

### AC-3 — 每次解密写审计
- 证据：解密后 `audit_logs` 新增记录，含 `tenant_id` 与密钥版本。
- 代码审阅：`grep -n "write_audit\|from app.audit import" backend/app/services/decrypt_worker.py` 应有命中——**必须调用既有 `app/audit.py:54` 的 `write_audit`，不是自造插入**。
- 反模式：确认审计调用**没有**被额外的 `try/except: pass` 包住（`write_audit` 本身已 fail-safe，再包一层会掩盖真实问题）。
- 判定：经 `write_audit` 写入 + 字段齐全 + 无多余吞异常 = PASS。

### AC-4 — 单租户零回归（**关键，这是生产唯一在跑的路径**）
- 证据：`pytest -k "decrypt"` 既有测试**全绿**；`local_file` 路径行为与今天一致。
- **代码审阅（重点）**：`git diff -- backend/app/services/decrypt_worker.py` —— **只应看到新增的审计调用**。若解密循环逻辑、`run_decrypt_once` 签名、密钥比对逻辑被改动 → FAIL（`SCOPE_VIOLATION`, blocker）。本票明确不许动这些。
- 背景：这条链路是当前生产唯一在跑的归档解密路径，改坏了归档会静默停摆。
- 判定：既有解密测试全绿 + `decrypt_worker` diff 仅含审计调用 = PASS。

### AC-5 — 私钥不明文落盘、不进日志
- 证据：
  - `kms_envelope` 路径下，存储层的私钥是密文（测试直查断言 ≠ 明文 PEM）。
  - **审阅所有 `raise` 与日志语句**：`grep -nE "raise|logger|print" backend/app/key_provider.py` —— 确认异常消息与日志**不回显私钥内容**（只说"密钥获取失败"这类）。
  - 无硬编码密钥进代码 / 测试固定值（测试应临时生成密钥对）。
- 判定：全部干净 = PASS。任一泄露 → FAIL（`SECURITY_VIOLATION`, severity: blocker）。

### AC-6 — 迁移可正反向且双库兼容
- 证据：`alembic upgrade head` 与 `downgrade -1` 均成功；`alembic check` exit 0；迁移写法兼容 SQLite（改唯一约束需 `batch_alter_table`，参考既有迁移）。
- 迁移编号：应为 `0024`（今日分配序位；若 head 已漂移，QA Summary 中须说明实际编号与理由）。
- 判定：正反向均可执行 + 无 drift = PASS。缺 `downgrade()` = FAIL。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过（`app.key_provider` 已登记进 `_FLAT_SERVICE_MODULES`）。
- 若本票新增了路由：须同步 `test_http_contract.py`（见 `docs/ticket-autopilot-workflow.md` §3.3）。本票**预期不新增路由**；若 diff 中出现新路由，确认契约已同步，否则 FAIL（`REGRESSION`）。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **未重造加密原语**：`git diff --stat -- backend/app/crypto.py` **必须无输出**（RND-333 交付，只调用）。若本票自建了一套对称加密 → FAIL（`SCOPE_VIOLATION`）。
2. **未重造审计**：`git diff --stat -- backend/app/audit.py` **必须无输出**。
3. **未改 SDK 隔离**：`git diff --stat -- backend/app/decrypt_isolation.py` **必须无输出**。
4. **架构边界**：`app.key_provider` 已加入 `_FLAT_SERVICE_MODULES`；service 层未 import `app.routers.*`。
5. **未越界做下游票**：diff 中不得出现 onboarding 向导 UI（RND-269）或 `POST /api/platform/tenants`（RND-311）。
6. **未对生产库操作（R2 强制）**：脚本 / 测试中无生产 `DATABASE_URL`、无硬编码连接串。
7. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于：`key_provider.py`（新）、`models.py`（仅 `KeyVersion`）、`0024_*.py`（新）、`decrypt_wecom_messages_once.py`（仅改走 KeyProvider）、`decrypt_worker.py`（仅审计调用）、`test_architecture_boundary.py`（仅一行）、`test_key_provider.py`（新）、安全说明文档。
   > 共享工作树可能含他票在途改动（见 `docs/ticket-autopilot-workflow.md` §3.4）——先 `git status` 分离归因，只把本票的部分记在本票账上。

## 附加检查（Security）
- 无真实企微凭据 / 真实私钥进入代码或测试。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL（`SECURITY_VIOLATION`）。
- 未改 CI/CD、部署配置。

## 验证命令（只读 / 仅本地库）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_key_provider.py -q
.venv/bin/python -m pytest backend/tests/ -k "decrypt" -q          # AC-4
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
cd backend && .venv/bin/python -m alembic check
cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic downgrade -1
git diff -- backend/app/services/decrypt_worker.py                 # AC-4：只应见审计调用
git diff --stat -- backend/app/crypto.py backend/app/audit.py backend/app/decrypt_isolation.py   # 必须全无输出
grep -nE "raise|logger|print" backend/app/key_provider.py          # AC-5：异常/日志不得回显私钥
git status --porcelain
git log origin/main..HEAD                                          # 必须无输出
```

## 产出
写入 `tasks/RND-332-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：审计粒度（批次级/消息级）、`kms_envelope` 测试如何模拟托管存储、迁移实际编号、在哪个库验证的。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **不得对生产库执行迁移**（即使只为验证）。
- **AC-2 若无「两个租户各自 publickey_ver=1 均成功」的用例 → 直接 FAIL。** 这是本票唯一的存在理由，不接受"约束看起来改对了"。
- **AC-4 若 `decrypt_worker.py` 的 diff 超出审计调用 → 直接 FAIL。** 那是生产唯一在跑的解密路径。
