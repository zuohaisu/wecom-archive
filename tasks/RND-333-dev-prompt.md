[Goal check] This work advances 开发（Development） by 交付 Fernet 字段级加密原语与 app_secret 加密落盘访问器 + 幂等再加密脚本，解除 RND-311 与 RND-332 的加密硬阻塞。

# RND-333 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-333「D2-1 字段级加密原语（Fernet）+ app_secret 加密落盘」｜Linear team `Builder`
- 优先级：**Urgent**｜风险等级：**R2（触及凭据处理）**｜milestone：R2 · 开源发布闭环
- **本票 blocks RND-311 与 RND-332** —— 是当前解锁面最大的一张票

## 背景与现状（已实地核实）

`TenantWecomConfig.app_secret`（`backend/app/db/models.py:84`）当前**明文存库**。模型自己的 docstring（`models.py:56-58`）就写着：

> `app_secret: Phase 1 stores plaintext (internal deployment only). Phase 3 must encrypt at rest using Fernet or Vault/KMS. Do NOT log app_secret — it is a permanent credential.`

**本票即该 Phase 3 的第一步。** 收费托管别人的企微凭据，明文存库不可接受。

**两个关键有利事实（已核实，直接影响你的做法）：**
1. **`cryptography==49.0.0` 已在 `backend/requirements.txt` 第 1 行** —— Fernet 开箱即用，**不要新增任何依赖**（`DEV_AGENT_RULES.md` 禁止未经批准加大依赖）。
2. **`app_secret` 运行期零消费** —— 全仓 grep 只出现在：`models.py`（定义）、`scripts/bootstrap_default_tenant.py`（一次性 upsert 写）、测试、`0001`/`0002` 迁移。**没有任何 router / sync / decrypt 代码从 DB 读它**（归档同步与解密链路读的是 env `WECOM_*`）。→ 把它改成密文的爆裂半径极低，**不会弄坏归档同步链路**。

**❗ 本项目高频踩坑：**
- **架构边界硬闸**：新建扁平域模块（如 `app/crypto.py`）**必须登记进 `backend/tests/test_architecture_boundary.py:70` 的 `_FLAT_SERVICE_MODULES` 集合**，否则该测试失败（硬停止，见 `AGENTS.md`）。照现有条目格式加一行 `"app.crypto",`。
- **service 层不得 import `app.routers.*`**。
- **密钥规则（`DEV_AGENT_RULES.md`）**：密钥只从环境变量读；不得硬编码进代码、文档、测试、脚本；`.env.example` 只放占位符。

## 目标（Goal）
提供一层最小、可复用、fail-closed 的字段级对称加密原语，并让 `TenantWecomConfig.app_secret` 以密文落盘，从而解除 RND-311（租户开通）与 RND-332（KeyProvider）的加密依赖。

## 范围边界

**In scope：**
1. **`backend/app/crypto.py`**（新）：
   - `encrypt_value(plain: str) -> str` / `decrypt_value(cipher: str) -> str`，Fernet 对称加密。
   - 密钥从环境变量读（建议 `FIELD_ENCRYPTION_KEY`，值为 Fernet 标准 44 字节 base64url key）。
   - **缺 key 时 fail-closed**：`encrypt_value` 抛明确异常，**绝不返回明文、绝不静默降级**。
   - `is_encrypted(value: str) -> bool`：Fernet token 结构可识别（版本字节 `0x80` → base64url 后以 `gAAAAA` 开头）。用于再加密脚本的幂等判断。**注意**：这是启发式判断，需在 docstring 里如实说明其局限（一个恰好以 `gAAAAA` 开头的明文会被误判），并说明为何在本场景可接受（`app_secret` 是企微 secret，格式已知，不会长这样）。
2. **登记 `_FLAT_SERVICE_MODULES`**：`backend/tests/test_architecture_boundary.py:70` 加 `"app.crypto",`。
3. **`models.py` 加解密访问器**（紧随 `app_secret` 定义之后）：
   - 写入侧方法（如 `set_app_secret(plain)`）经 `encrypt_value`。
   - 读取侧 `decrypted_app_secret` property 经 `decrypt_value`。
   - **列名 `app_secret` 保持不变**（避免动契约 schema 与 `bootstrap_default_tenant.py` 的列名，也避免动迁移）。
   - **懒导入** `from app.crypto import ...` 放函数体内，防循环导入。
4. **`backend/scripts/reencrypt_app_secrets_once.py`**（新）：把既有明文行转密文。
   - 幂等（`is_encrypted` 为真则跳过）、支持 `--dry-run`、照既有 `backfill_*_once.py` 脚本范式写。
5. **`.env.example`**：加 `FIELD_ENCRYPTION_KEY=` 占位符 + 一行生成方法注释（如 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`）。**绝不写真实 key。**
6. **`backend/tests/test_field_encryption.py`**（新）。

**Out of scope（显式非目标）：**
- **不做 `KeyProvider` 抽象 / `local_file` / `kms_envelope` 双实现** —— 那是 RND-332。本票只提供底层对称原语；RND-332 会在其之上建 provider 分层。
- **不做 `KeyVersion` 租户化 / `publickey_ver` 唯一约束修改** —— RND-332。
- **不做解密审计钩子** —— RND-332。
- **不碰 WeCom 归档同步 / 解密链路**（`decrypt_worker.py`、`decrypt_wecom_messages_once.py`、`decrypt_isolation.py`）—— 本票只管 `app_secret` 这一个字段。
- 不实现 `POST /api/platform/tenants`（RND-311）。
- 不做密钥轮换机制（`MultiFernet` 等）——可在 docstring 记为已知后续项，不实现。

**本工单拥有的文件（只许写这些）：**
- `backend/app/crypto.py`（新）
- `backend/app/db/models.py` —— **仅限** `TenantWecomConfig` 的加解密访问器（不改列定义、不改列名）
- `backend/scripts/reencrypt_app_secrets_once.py`（新）
- `backend/tests/test_architecture_boundary.py` —— **仅限** `_FLAT_SERVICE_MODULES` 加一行 `"app.crypto",`
- `backend/tests/test_field_encryption.py`（新）
- `.env.example` —— 仅加 `FIELD_ENCRYPTION_KEY` 占位符 + 生成说明

**只读、绝不可写：** `decrypt_worker.py`、`decrypt_wecom_messages_once.py`、`decrypt_isolation.py`、所有 router、4 张 R1 页面票的文件、`bootstrap_default_tenant.py`（见下方风险 3）。

## 验收标准（Acceptance Criteria）

- **AC-1 原语可用且对称**：`decrypt_value(encrypt_value(x)) == x`；且密文 `!= x`（有测试）。
- **AC-2 缺 key fail-closed（最高风险项）**：Given `FIELD_ENCRYPTION_KEY` 未设置，When 调用 `encrypt_value`，Then **抛出明确异常**。**必须有测试断言它抛错，而不是返回明文。** 静默降级存明文 = 本票最严重的失败模式（会造成"以为加密了其实没有"的假安全）。
- **AC-3 新写入加密**：经访问器写入的 `TenantWecomConfig.app_secret` 落库为密文（直查 DB 断言 `!= 入参明文`），`decrypted_app_secret` 可还原。
- **AC-4 再加密脚本幂等且可 dry-run**：`--dry-run` 报告待处理行数且**不写库**；对已加密行重复运行为 no-op。
- **AC-5 零泄露**：密钥不出现在代码、测试固定值、日志、`.env.example` 真实值中；`app_secret` 明文不写进任何日志或异常消息（**注意异常消息**——不要把明文塞进 error string）。
- **AC-6 架构边界与回归**：`"app.crypto"` 已登记进 `_FLAT_SERVICE_MODULES` 且 `test_architecture_boundary.py` 通过；`make verify` 全绿；**归档同步 / 解密链路零改动**（`git diff --stat` 对那三个文件必须无输出）。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_field_encryption.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/services/decrypt_worker.py backend/scripts/decrypt_wecom_messages_once.py backend/app/decrypt_isolation.py   # 必须无输出（AC-6）
grep -n "FIELD_ENCRYPTION_KEY" .env.example    # 只应是占位符 + 生成说明，不得是真实 key
grep -rn "gAAAAA\|Fernet(" backend/tests/test_field_encryption.py   # 确认测试用的是动态生成的 key，不是写死的
```

## 依赖（Dependencies）
无前置阻塞，**可立即开始**。本票 **blocks RND-311 与 RND-332**。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿、架构边界测试通过
- [ ] `.env.example` 有占位符与生成说明，**无真实 key**
- [ ] 再加密脚本 dry-run 与幂等性均有测试
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，含「本地用什么 key 验证的（**不要贴 key 本身**，只说来源是临时生成）」
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：缺 key 时静默降级存明文 → 造成"以为加密了其实没有"的假安全，且不报错、几乎不可能被发现。**由 AC-2 显式防守，必须有抛错测试。**
- **风险 2**：明文进日志/异常消息 → 凭据泄露到日志系统。**由 AC-5 防守**；写异常时只说"解密失败"，不要回显值。
- **风险 3（需上报，不要自行处理）**：`scripts/bootstrap_default_tenant.py` 用 raw SQL 把 `WECOM_OAUTH_SECRET` 明文写入 `app_secret`。理想上它也该改用 `encrypt_value`，但**该文件不在本票拥有清单内**（它是 B 层脚本，且改它会牵动本地开发引导流程）。**发现即在 QA Summary 中如实记为已知缺口并上报，不要自行修改**——由 Haisu 决定单开票还是并入 RND-332。
- 回滚：新增文件为主 + `models.py` 加方法；`git checkout -- <files>` 即可。再加密脚本是数据写入，**不可自动回滚** —— 因此 `--dry-run` 与幂等性是关键保护。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2 强制）**：本票触及凭据处理，**必须经 Haisu 审阅后才可 commit**；再加密脚本对任何非本地库执行前需 Haisu 确认。
- **Escalation**：若发现 `app_secret` 实际上有运行期读取方（与本提示词"零消费"结论矛盾）→ `BLOCKED_NEEDS_HUMAN`，**不要擅自改那个读取方**，先报上来重新评估爆裂半径。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`（尤其 Secrets 章节）、`docs/ticket-autopilot-workflow.md`、`models.py:53-90`（`TenantWecomConfig` 定义与 docstring）、`test_architecture_boundary.py:70`（`_FLAT_SERVICE_MODULES` 格式）、既有 `backfill_*_once.py` 脚本范式。
2. **先自行复核"`app_secret` 运行期零消费"**：`grep -rn "app_secret" backend/ --include=*.py`。与本提示词结论不符即上报（见 Escalation）。
3. 写 `app/crypto.py`（含 fail-closed 与 `is_encrypted` 的诚实 docstring）。
4. 登记 `_FLAT_SERVICE_MODULES`。
5. `models.py` 加访问器（懒导入）。
6. 写再加密脚本（`--dry-run` / 幂等）。
7. `.env.example` 加占位符 + 生成说明。
8. 写 `tests/test_field_encryption.py` 覆盖 AC-1~AC-5（**AC-2 的抛错用例是重点**）。
9. 跑 `make verify`，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史。
- **不新增依赖**（`cryptography` 已在 requirements.txt，直接用）。
- 密钥只从环境变量读；**不得硬编码进代码/文档/测试/脚本**；`.env.example` 只放占位符。
- 不改 CI/CD、部署配置。
- 不扩大 Scope：KeyProvider / KeyVersion / 审计钩子 / 密钥轮换一律 Out。
- 证据优先：以命令 exit 0 / 测试通过为证。
