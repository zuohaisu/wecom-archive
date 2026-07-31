[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-249 的 10 条 AC（重点验证密钥掩码、PUT 部分失败不落库、空串语义、既有密码端点零回归）并产出带证据的 PASS/FAIL 判定。

# RND-249 验收提示词（Acceptance / QA Prompt）— 配置中心 T5

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-249「配置中心 T5：Settings API」｜风险等级 **R2**
- **AC-2（密钥掩码）与 AC-4（部分失败不落库）是最高风险项，从严判定。既有 `POST /settings/password` 是生产在用端点，AC-9 同样从严。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — GET 返回全部字段
- 判定：响应含 `CONFIG_REGISTRY` 全部 key，按分组组织 = PASS。

### AC-2 — 密钥掩码（关键）
- 证据：测试写入一个密钥字段的已知值，GET 响应断言：① 序列化后的 JSON 字符串中**不包含**该原始值；② 返回的是掩码格式。
- 判定：符合 = PASS。**任何密钥字段的明文出现在 GET 响应中 → 直接 FAIL（`SECURITY_VIOLATION`, severity: blocker）**。

### AC-3 — 来源标注正确
- 判定：`default`/`env`/`db` 三种场景各自标注正确 = PASS。

### AC-4 — PUT 强校验 + 部分失败不落库（关键）
- 证据：测试构造一批更新，其中一个字段非法，断言：① 响应 400 + `errors` 含具体 key；② 查库确认**其余合法字段也未被写入**（不是"合法的先落库，非法的报错"这种半成品结果）。
- 判定：全有或全无 = PASS。**若发现部分字段已落库而另一部分因校验失败被拒绝（整批操作不是原子的） → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: major）**——这会让配置陷入不一致状态，且用户从错误响应里看不出哪些其实已经生效。

### AC-5 — 密钥加密落库
- 证据：PUT 密钥字段后直接查 `AppConfigStore.value`，断言是密文（`is_encrypted() == True`，且不等于原始明文）。
- 判定：符合 = PASS。**明文落库 → 直接 FAIL（`SECURITY_VIOLATION`, blocker）**。

### AC-6 — 空串语义正确（关键）
- 证据：① secret 字段已有值时 PUT 空串 → 查库原值不变；② 非 secret 字段 PUT 空串 → 查库该字段被清空。两个方向都要有测试。
- 判定：两种语义分别正确 = PASS。**任一方向搞反（比如 secret 传空串被清空、丢失已保存的密钥）→ 直接 FAIL（`IMPLEMENTATION_DEFECT`, major）**——这会导致用户一次误触就把已配置的密钥清空，且没有恢复手段。

### AC-7 — 失效缓存
- 判定：PUT 后紧接着读取应看到新值（不是缓存里的旧值）= PASS。

### AC-8 — 重启提示
- 判定：涉及 `requires_restart=True` 字段的 PUT，响应正确列出这些 key = PASS。

### AC-9 — 未改既有密码端点（关键）
- 证据：`git diff -- backend/app/routers/settings.py` 中 `change_password`/`POST /settings/password` 函数体逐字未变。
- 判定：符合 = PASS。**任何改动（哪怕看似无害的重构）→ 直接 FAIL（`REGRESSION`, blocker）**——这是生产在用端点。

### AC-10 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；既有覆盖 `POST /settings/password` 的测试全绿。
- 判定：符合 = PASS。

## 本项目专属检查（必查）
1. **`require_role()` 而非臆造的 `require_settings_admin`**：确认本票用的是既有 `require_role()`（G4 冻结：MVP 已认证=可管理），**没有**提前造一个 `require_settings_admin` 依赖占位——那是 T6 的交付物，本票提前造会与 T6 实际签名对不上。
2. **未新建重复的 router 变量**：确认新端点加在**已有的** `settings_router` 变量上，不是新建了一个 `APIRouter()` 实例。
3. **文件所有权**：`git status --porcelain` 中改动应限于 `routers/settings.py`（追加）、`schemas/settings.py`（新）、`tests/test_rnd249_settings_api.py`（新）。

## 附加检查（Security）
- 测试中的密钥值为测试专用固定值，非真实凭据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd249_settings_api.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/routers/settings.py    # AC-9：人工核对既有端点逐字未变
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-249-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录 GET/PUT 的确切请求/响应结构。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若明文密钥出现在响应中 → 直接 FAIL（blocker）**。
- **AC-9 若既有密码端点有任何改动 → 直接 FAIL（blocker）**。
