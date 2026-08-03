[Goal check] This work advances 开发（Development） by 交付配置中心专用的 Fernet 加解密模块，供 T5 Settings API 写入密钥类配置项前加密使用。

# RND-245 开发提示词（Developer Prompt）— 配置中心 T2

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-245 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-245「配置中心 T2：加密模块 crypto + SETTINGS_ENCRYPTION_KEY 读取」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R1**（密钥管理，fail-closed 语义写错会导致密钥明文落库）｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实，本票的核心参照）

`backend/app/crypto.py`（<issue>RND-333</issue> 已 Done）是一个**结构几乎完全适用**的现成参照：Fernet 对称加密、`encrypt_value`/`decrypt_value`/`is_encrypted` 三个函数、key 从 env（`FIELD_ENCRYPTION_KEY`）读取、缺 key 时 `FieldEncryptionConfigurationError` fail-closed、错误信息不泄露密钥值。**本票不是复用这个模块**（它是给业务字段级加密用的，key 名固定 `FIELD_ENCRYPTION_KEY`），而是**新建一个结构几乎相同、但 key 名不同**（`SETTINGS_ENCRYPTION_KEY`）的独立模块——这是 RND-244 设计冻结时的既定决定（"新增 `backend/app/config/crypto.py`"），因为配置中心的密钥材料应该能独立于字段级 PII 加密单独轮换/管理，不共享同一把 key。

**照抄 `app/crypto.py` 的结构与失败处理模式**（错误类型命名、fail-closed 行为、不记录明文），**只换 env 变量名和模块位置**。

**❗ 本项目高频踩坑：**
- **不要修改 `app/crypto.py` 本身**——那是 <issue>RND-333</issue> 已上线、被生产解密链路依赖的模块，本票只是"参考它的写法"，不是"扩展它"。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个独立的、fail-closed 的配置密钥加解密模块，key 来自 `SETTINGS_ENCRYPTION_KEY`，供 T5（Settings API）写入密钥类配置项前调用。

## 范围边界

**In scope：**
1. `backend/app/config/crypto.py`（**新建**，结构照抄 `app/crypto.py`）：
   - `encrypt_value(plain: str) -> str` / `decrypt_value(cipher: str) -> str` / `is_encrypted(value: str) -> bool`（同名函数，同语义，key 换成 `SETTINGS_ENCRYPTION_KEY`）。
   - `mask(value: str) -> str`：**新函数**（`app/crypto.py` 没有这个，是本票新增），返回末 4 位 + `"****"`（如 `"****ab12"` 或按 T9/`RND-253` 前端约定的具体格式——本票先定义清楚：掩码后长度固定、原文长度 ≤4 时全掩码不暴露任何原文字符）。
   - key 缺失时的自定义异常类（可直接复用 `app/crypto.py` 的异常类命名风格，如 `SettingsEncryptionConfigurationError`/`SettingsDecryptionError`，**不要 import `app.crypto` 的异常类**，本票是独立模块）。
2. `backend/app/settings.py` **新增** `SettingsEncryptionSettings` 类 + `get_settings_encryption_settings()` 工厂函数（读 `SETTINGS_ENCRYPTION_KEY`），**照抄该文件里其他 `get_xxx_settings()` 的写法**（无 `lru_cache`，每次调用现读 env，保持 monkeypatch 语义一致——见该文件开头的模块 docstring 说明，务必读一遍再动手）。
3. 测试：`backend/tests/test_rnd245_config_crypto.py`。

**Out of scope（显式非目标）：**
- 不改 `app/crypto.py`（只参考结构）。
- 不做密钥落库逻辑（属 T5，本票只提供加解密原语）。
- 不做密钥轮换（`MultiFernet` 之类，超出本票范围，`app/crypto.py` 同样没做，保持一致的简化程度）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/config/crypto.py`（新）
- `backend/app/settings.py` —— **仅新增** `SettingsEncryptionSettings` 类 + `get_settings_encryption_settings()` 函数，不改任何既有类
- `backend/tests/test_rnd245_config_crypto.py`（新）

**只读、绝不可写：** `app/crypto.py`（只作参照，不修改）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 加解密往返正确**：`decrypt_value(encrypt_value(x)) == x`，对含中文/特殊字符的字符串同样成立。
- **AC-2 fail-closed（关键）**：`SETTINGS_ENCRYPTION_KEY` 未设置时，调用 `encrypt_value`/`decrypt_value` **抛异常**，不返回明文、不静默降级、不返回空字符串假装成功。
- **AC-3 错误信息不泄露**：异常消息中不包含被加密/解密的原始明文内容。
- **AC-4 `is_encrypted` 正确区分**：对已加密值返回 `True`，对明文返回 `False`（同 `app/crypto.py` 的启发式判定逻辑，照抄其边界处理）。
- **AC-5 `mask()` 不泄露原文（关键）**：对任意长度输入（含 ≤4 字符的短字符串），`mask()` 的返回值**不包含**除末 4 位以外的任何原文字符；短字符串（≤4）应整体掩码，不能因为"末4位"逻辑把全部原文都暴露出来。
- **AC-6 未改 `app/crypto.py`**：`git diff --stat -- backend/app/crypto.py` 必须无输出。
- **AC-7 `settings.py` 新增不破坏既有类**：`git diff -- backend/app/settings.py` 中既有 `class`/`def` 逐行未变，只有新增内容；既有测试（若有覆盖 `settings.py` 的）全绿。
- **AC-8 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd245_config_crypto.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/crypto.py    # AC-6：必须无输出
git diff -- backend/app/settings.py          # AC-7：人工核对只新增
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
无剩余前置（可与 T1/T3 并行）。本票 **blocks** T4（<issue>RND-248</issue>）与 T5（<issue>RND-249</issue>）里涉及密钥落库的部分。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-8 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明 `mask()` 的确切输出格式（供 T9/前端展示对齐）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：fail-closed 语义写错，key 缺失时静默返回明文或空值——由 AC-2 防守，这是密钥管理最容易犯的错。
- 风险 2：`mask()` 对短字符串处理不当，意外暴露全部原文——由 AC-5 防守。
- 回滚：纯新增文件 + `settings.py` 局部新增，`git checkout -- <files>` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿即可，无需额外人工审阅（R1，纯工具函数，尚未接入任何写路径）。
- **Escalation**：若发现 `SETTINGS_ENCRYPTION_KEY` 与 `FIELD_ENCRYPTION_KEY` 应该是同一把 key（而不是两把独立的）→ `BLOCKED_NEEDS_HUMAN`，说明理由，**不要**自行改成复用 `app.crypto` 的 key。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/crypto.py`（**逐行**，这是本票的结构模板）、`app/settings.py`（模块开头的 docstring，理解"每次现读 env、不缓存"的约束为什么存在）。
2. 新建 `app/config/crypto.py`，照抄 `app/crypto.py` 的加解密逻辑，key 换成 `SETTINGS_ENCRYPTION_KEY`，新增 `mask()`。
3. `settings.py` 新增 `SettingsEncryptionSettings` + 工厂函数。
4. 写测试覆盖 AC-1~AC-5（**AC-2 fail-closed 与 AC-5 mask 边界是重点**）。
5. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假 Fernet key（`Fernet.generate_key()` 现生成，不硬编码真实密钥字符串）。
- 不扩大 Scope：不做落库、不做轮换。
- 复用优先：结构照抄 `app/crypto.py`，不是从零设计。
- 证据优先，以 exit 0 / 测试通过为证。
