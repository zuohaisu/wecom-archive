[Goal check] This work advances 开发（Development） by 交付 KeyProvider 双实现、KeyVersion 租户化迁移与解密审计钩子，使第二个租户可以从 publickey_ver=1 开始而不冲突。

# RND-332 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-332 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-332「D2-2 KeyProvider 抽象 + KeyVersion 租户化 + 解密审计钩子」
- 优先级：**Urgent**｜风险等级：**R2（迁移 + 密钥处理）**｜milestone：R2 · 开源发布闭环
- **本票 blocks RND-269**（onboarding 向导第 2 步密钥上传）

## 背景与现状（已实地核实）

云版本由我们托管客户私钥（2026-07-29 Haisu 拍板）。当前解密路径是**单租户硬编码**：

1. **私钥来自环境变量**：`backend/scripts/decrypt_wecom_messages_once.py:178` 读 `WECOM_PRIVATE_KEY_PATH` / `WECOM_PUBLIC_KEY_VERSION`。一套部署只能有一把私钥。
2. **`KeyVersion` 是死表**：模型在 `backend/app/db/models.py:318`，但**解密路径根本不查它**（全仓 grep `KeyVersion` 只命中模型定义本身）。
3. **`publickey_ver` 全局唯一**（`models.py:332` `Column(Integer, unique=True, nullable=False)`）——每家企微 corp 的 `publickey_ver` 都从 1 开始，**第二个租户插入必然唯一约束冲突**。这是"卖云版本"的头号硬阻塞。

**两个有利结构（决定实现方式，务必先读）：**

- **接缝已经在了**：`backend/app/services/decrypt_worker.py:424` 的 `run_decrypt_once(...)` 把 `private_key: rsa.RSAPrivateKey` 与 `expected_pubkey_ver` 作为**参数**接收，自己不读 env——env 读取发生在 CLI 脚本层。所以 `KeyProvider` 接入点在 **CLI/调用方**，**不需要改 worker 主循环签名**。
- **加密原语已就绪**：RND-333 已交付 `backend/app/crypto.py`（`encrypt_value` / `decrypt_value` / `is_encrypted`，Fernet，密钥取自 env，fail-closed）。`kms_envelope` 的信封加密**建在它之上**，**不要重造对称加密**。

**审计基础设施也已就绪**：`backend/app/audit.py:54` 的 `write_audit(db, *, tenant_id, action, object_type, admin_user_id=None, object_id=None, detail=None)`，append-only 且 **fail-safe（永不抛异常）**。直接调用，不要重造。

**❗ 本项目高频踩坑：**
- **Alembic schema drift 是 CI 硬闸**：改了 `models.py` 必须有对应迁移，CI 跑 `python -m alembic check`。
- **迁移需双库兼容**：PostgreSQL（生产）+ SQLite（部分测试）。改唯一约束在 SQLite 上需 `batch_alter_table`，照既有迁移范式写。
- **架构边界硬闸**：新建扁平域模块必须登记进 `backend/tests/test_architecture_boundary.py:70` 的 `_FLAT_SERVICE_MODULES`（`app.crypto` 已在里面，新模块要自己加）。

## 迁移序位（今日并行批次，重要）

当前 alembic head = **0023**（`0023_media_file_size_storage_rollup.py`）。今日 4 张票需迁移，已分配：

| 票 | 迁移号 |
|---|---|
| **RND-332（本票）** | **`0024`** |
| RND-311 | `0025` |
| RND-316 | `0026` |
| RND-318 | `0027` |

**开工先确认 head 仍是 0023**（`ls backend/alembic/versions/ | sort | tail -3`）；若已漂移，按实际 head 顺延并在 QA Summary 注明。

## 目标（Goal）
让解密链路支持多租户：每个租户用自己的私钥与自己的 `publickey_ver` 序列，且每次解密可审计；同时保持单租户自托管路径行为完全不变。

## 范围边界

**In scope：**

1. **`KeyProvider` 抽象 + 双实现**（建议 `backend/app/key_provider.py`，需登记 `_FLAT_SERVICE_MODULES`）：
   - 接口：给定 `tenant_id` + `publickey_ver`，返回可用于解密的 `rsa.RSAPrivateKey`。
   - `local_file` —— 读本地 PEM 路径（**等价今天的行为**，开源自托管版走这条，单租户零回归）。
   - `kms_envelope` —— 信封加密：私钥密文存库（用 `app.crypto.encrypt_value` 封装），使用时解封为内存对象，**永不明文落盘**。
   - 由配置/env 决定用哪个实现（如 `KEY_PROVIDER=local_file|kms_envelope`，默认 `local_file` 保持向后兼容）。
2. **`KeyVersion` 租户化**（迁移 `0024`）：
   - 加 `tenant_id` 列（外键 `tenants.id`）。
   - 唯一约束从全局 `publickey_ver` 改为**复合唯一 `(tenant_id, publickey_ver)`**。
   - 让解密路径**真正使用这张表**（当前是死表）——按 `(tenant_id, publickey_ver)` 查到对应密钥来源，交给 `KeyProvider`。
   - 迁移需处理既有行（当前若有数据，`tenant_id` 回填为默认租户；无数据则直接加列）。**必须提供 `downgrade()`。**
3. **解密审计钩子**：每次解密（批次级或消息级，自行判断合理粒度并在 QA Summary 说明）调用 `write_audit(...)` 写一条记录，含 `tenant_id` + 动作类型 + 密钥版本。**必须调用既有 `app/audit.py` 的函数，不要自造审计写入。**
4. **对外安全说明文档**（`docs/` 下新建）：密钥托管与隔离设计——托管客户私钥时这份文档本身是销售材料。说明：私钥如何存储、谁能访问、每次访问如何留痕、自托管与云托管的差别。
5. 测试：`backend/tests/test_key_provider.py`。

**Out of scope（显式非目标）：**
- **不重写对称加密原语**——`app.crypto`（RND-333）已交付，只调用。
- **不改 `run_decrypt_once` 的核心解密循环逻辑与签名**——接缝在 CLI/调用方。
- 不做 onboarding 向导 UI（RND-269）。
- 不做租户开通端点（RND-311）。
- 不改 `decrypt_isolation.py`（SDK 进程隔离，与本票无关）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/key_provider.py`（新）
- `backend/app/db/models.py` —— **仅限 `KeyVersion`**（加 `tenant_id`、改唯一约束）
- `backend/alembic/versions/0024_*.py`（新）
- `backend/scripts/decrypt_wecom_messages_once.py` —— 仅限「改为经 `KeyProvider` 取密钥」的最小改动
- `backend/app/services/decrypt_worker.py` —— **仅限**新增审计钩子调用；**不得**改解密循环逻辑或函数签名
- `backend/tests/test_architecture_boundary.py` —— 仅加 `"app.key_provider",` 一行
- `backend/tests/test_key_provider.py`（新）
- `docs/<密钥托管安全说明>.md`（新）

**只读、绝不可写：** `app/crypto.py`（RND-333 交付，只调用）、`app/audit.py`（只调用 `write_audit`）、`app/decrypt_isolation.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 双实现均可跑通**：`local_file` 与 `kms_envelope` 两条路径都能取到私钥并完成一次解密（`kms_envelope` 可用测试夹具模拟托管存储，不需要真实 KMS）。
- **AC-2 第二租户不再冲突（本票核心价值）**：Given 租户 A 已有 `publickey_ver=1`，When 租户 B 也插入 `publickey_ver=1`，Then **成功**（复合唯一生效）。**必须有这条测试**——这是本票存在的唯一理由。
- **AC-3 每次解密写审计**：解密后 `audit_logs` 新增记录，含 `tenant_id` 与密钥版本；**经 `write_audit` 写入**（代码可见 import + 调用，非自造）。
- **AC-4 单租户零回归（关键）**：`local_file` 路径下，既有解密行为与今天**完全一致**——`pytest -k "decrypt"` 既有测试全绿，`decrypt_worker` 的解密循环逻辑与签名未变（`git diff` 该文件只应见新增审计调用）。
- **AC-5 私钥不明文落盘**：`kms_envelope` 路径下，存储层里的私钥是密文（直查断言 ≠ 明文 PEM）；且**异常消息 / 日志中不回显私钥内容**（审阅所有 `raise` 与日志语句）。
- **AC-6 迁移可正反向**：`alembic upgrade head` 与 `downgrade -1` 均成功；`alembic check` 无 drift；SQLite 与 PostgreSQL 均可执行。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过（`app.key_provider` 已登记）。若新增了路由，须同步契约测试（见 `docs/ticket-autopilot-workflow.md` §3.3）——本票**预期不新增路由**。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_key_provider.py -q
.venv/bin/python -m pytest backend/tests/ -k "decrypt" -q      # AC-4：单租户零回归
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
cd backend && .venv/bin/python -m alembic check
cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic downgrade -1
git diff -- backend/app/services/decrypt_worker.py   # 人工核对：只多了审计调用，解密逻辑与签名未动
git diff --stat -- backend/app/crypto.py backend/app/audit.py backend/app/decrypt_isolation.py   # 必须全无输出
```

## 依赖（Dependencies）
RND-333（`app.crypto`）**已 Done 并核实落地**。无剩余前置。**本票 blocks RND-269。**

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿、`alembic check` 通过、迁移正反向验证过
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**说明：审计粒度选择（批次级还是消息级）及理由、`kms_envelope` 的托管存储在测试中如何模拟、迁移实际用的编号**
- [ ] **未 commit、未 push、未对生产库执行迁移**

## 风险与回滚
- **风险 1（最高）**：改唯一约束时误删既有约束保护，导致同租户内 `publickey_ver` 可重复 → 解密选错密钥。**复合唯一必须真的是 `(tenant_id, publickey_ver)` 两列，不是只留 `tenant_id`。**
- **风险 2**：`local_file` 路径被改坏 → 打断现有单租户自托管归档解密（这是当前唯一在跑的生产路径）。**由 AC-4 显式防守。**
- **风险 3**：私钥出现在异常消息或日志里 → 最严重的凭据泄露。**由 AC-5 防守**；写异常时只说"密钥获取失败"，绝不回显内容。
- **风险 4**：审计写入若被 `try/except` 吞掉，合规链条断裂。`write_audit` 本身 fail-safe，但**不要**再包一层静默吞异常。
- 回滚：迁移提供 `downgrade()`；`KeyProvider` 默认 `local_file` 保持向后兼容，回滚即恢复今天行为。

## 人工点位
- **Trigger**：Haisu 置 In Progress（已置）。
- **Gate（R2 强制）**：迁移与密钥相关改动**必须经 Haisu 审阅后**才可 commit / 对任何非本地库执行。
- **Escalation**：若发现 `KeyVersion` 表在生产已有数据且 `tenant_id` 无法安全回填 → `BLOCKED_NEEDS_HUMAN`，说明数据现状，**不要猜一个 tenant_id 填进去**。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`models.py:318-340`（`KeyVersion`）、`decrypt_worker.py:424` 附近（理解 `run_decrypt_once` 的参数契约）、`decrypt_wecom_messages_once.py:170-200`（env 读取处）、`app/crypto.py`（RND-333 交付物）、`app/audit.py:54`（`write_audit` 签名）、既有迁移里 `batch_alter_table` 的写法。
2. 确认 alembic head 仍是 `0023`。
3. 写 `key_provider.py` 双实现 + 登记 `_FLAT_SERVICE_MODULES`。
4. 改 `models.py` 的 `KeyVersion` + 写迁移 `0024`（含 `downgrade`）。
5. 改 CLI 脚本改走 `KeyProvider`；在 `decrypt_worker` 加审计调用（**只加调用**）。
6. 写安全说明文档。
7. 写测试覆盖 AC-1~AC-6（**AC-2 的双租户同版本号用例是重点**）。
8. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史。
- **不对生产库或共享库执行迁移**。
- 不改 CI/CD、`.gitignore`、部署配置。
- 密钥只从环境变量 / 托管存储读；**不得硬编码进代码、测试、文档**；测试用临时生成的密钥对。
- 不扩大 Scope：onboarding UI、租户开通端点一律 Out。
- 复用优先：`app.crypto`、`write_audit` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
