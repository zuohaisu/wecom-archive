[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-245 的 8 条 AC（重点验证 fail-closed 语义、mask() 不泄露原文、未改动既有 app/crypto.py）并产出带证据的 PASS/FAIL 判定。

# RND-245 验收提示词（Acceptance / QA Prompt）— 配置中心 T2

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-245「配置中心 T2：加密模块」｜风险等级 **R1**（密钥管理）
- **AC-2（fail-closed）与 AC-5（mask 不泄露）是密钥管理类工单的最高风险项，从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 加解密往返正确
- 证据：测试对多组字符串（含中文/特殊字符/空字符串）断言 `decrypt_value(encrypt_value(x)) == x`。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — fail-closed（关键）
- 证据：测试在 `SETTINGS_ENCRYPTION_KEY` 未设置（unset/空字符串）时调用 `encrypt_value`/`decrypt_value`，断言**抛出异常**，不是返回原文/空值/静默成功。
- 判定：符合 = PASS。**若 key 缺失时静默返回明文或不报错 → 直接 FAIL（`SECURITY_VIOLATION`, severity: blocker）**——这会导致密钥"看起来加密了"实际是明文落库。

### AC-3 — 错误信息不泄露
- 证据：审阅异常抛出路径，确认 `raise` 语句的错误消息不包含被处理的原始明文变量。
- 判定：符合 = PASS。

### AC-4 — `is_encrypted` 正确区分
- 判定：对加密值/明文分别返回 `True`/`False` 且有测试覆盖 = PASS。

### AC-5 — `mask()` 不泄露原文（关键）
- 证据：测试覆盖多种长度输入，**尤其 ≤4 字符的短字符串**，断言返回值中除声明的"末 4 位"外不包含任何原文片段；短输入应整体掩码。
- 判定：符合 = PASS。**若短字符串因"取末4位"逻辑导致原文全部或大部分暴露 → 直接 FAIL（`SECURITY_VIOLATION`, major）**。

### AC-6 — 未改 `app/crypto.py`
- 证据：`git diff --stat -- backend/app/crypto.py` 无输出。
- 判定：无输出 = PASS；有任何改动 → FAIL（`SCOPE_VIOLATION`, blocker——那是生产解密链路依赖的模块）。

### AC-7 — `settings.py` 新增不破坏既有类
- 证据：`git diff -- backend/app/settings.py` 中既有类/函数逐行未变。
- 判定：符合 = PASS。

### AC-8 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：符合 = PASS。

## 本项目专属检查（必查）
1. **未复用 `app.crypto` 的 key**：审阅 `app/config/crypto.py`，确认读取的 env 变量是 `SETTINGS_ENCRYPTION_KEY`，不是 `FIELD_ENCRYPTION_KEY`。若发现复用了同一把 key（例如直接 `from app.crypto import encrypt_value` 而不是新写一份）→ 记 finding 并核实是否与 dev prompt 的既定设计冲突（本票明确要求独立模块/独立 key）。
2. **测试未使用真实密钥**：测试中的 Fernet key 应为 `Fernet.generate_key()` 现场生成，不是硬编码字符串。
3. **文件所有权**：`git status --porcelain` 中改动应限于 `app/config/crypto.py`（新）、`app/settings.py`（仅新增 `SettingsEncryptionSettings`）、`tests/test_rnd245_config_crypto.py`（新）。

## 附加检查（Security）
- 无真实密钥值出现在代码/测试/日志断言中。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd245_config_crypto.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/crypto.py    # AC-6：必须无输出
git diff -- backend/app/settings.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-245-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：`mask()` 的确切输出格式示例。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若 key 缺失时不 fail-closed → 直接 FAIL（blocker）**。
- **AC-6 若改动了 `app/crypto.py` → 直接 FAIL（blocker）**。
