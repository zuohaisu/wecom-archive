[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-248 的 8 条 AC（重点验证解析顺序 DB>env>默认、密钥自动解密、monkeypatch 语义不受影响）并产出带证据的 PASS/FAIL 判定。

# RND-248 验收提示词（Acceptance / QA Prompt）— 配置中心 T4

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-248「配置中心 T4：配置仓储 + 解析器」｜风险等级 **R2**（解析顺序是配置中心的核心正确性保证）
- **AC-1/AC-2（解析顺序）与 AC-4（密钥解密）是最高风险项，从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-0 — 前置核实
- 证据：`.venv/bin/python -c "from app.db.models import AppConfigStore; from app.config.crypto import encrypt_value, decrypt_value; from app.config.schema import CONFIG_REGISTRY"` 无报错。
- 判定：无报错 = 继续验收。**若报错但本票判定不是 BLOCKED（说明开发 agent 绕过了前置检查）→ 直接 FAIL（`SCOPE_VIOLATION`, blocker）**。

### AC-1 — DB 优先（关键）
- 证据：测试写入 `AppConfigStore` 一个值，同时 `monkeypatch.setenv` 设置对应 env 变量为**不同的值**，断言 `resolve()` 返回 DB 值。
- 判定：符合 = PASS。**若返回了 env 值 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——解析顺序颠倒，用户在 UI 保存的配置不生效。

### AC-2 — env 兜底
- 判定：DB 无值、env 有值时返回 env 值，且经过对应 `get_xxx_settings()` 读取（不是直接 `os.environ.get`）= PASS。

### AC-3 — 默认值兜底
- 判定：DB 和 env 都无值时返回 `CONFIG_REGISTRY` 的 `default` = PASS。

### AC-4 — 密钥自动解密（关键）
- 证据：测试写入一个 `is_secret=True` 字段的**加密**值，`resolve()` 读出后断言等于**原始明文**（不是密文）。
- 判定：符合 = PASS。**若 `resolve()` 返回密文而非明文 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, blocker）**——调用方（T5/后续业务逻辑）会拿密文当明文用，功能直接损坏。

### AC-5 — 缓存与失效生效
- 证据：测试用 mock/计数器断言连续两次 `resolve()` 同一 key 只查了一次库；调用 `invalidate(key)` 后下一次重新查库。
- 判定：符合 = PASS。

### AC-6 — `monkeypatch` 语义不受影响（关键）
- 证据：测试用 `monkeypatch.setenv` 改环境变量后（缓存未命中场景），`resolve()` 读到新值。
- 判定：符合 = PASS。**若因为解析器内部缓存导致读到旧值 → FAIL（`REGRESSION`, major）**——这会连累全项目依赖 `monkeypatch.setenv` 的既有测试套件。

### AC-7 — 未改 env settings 函数
- 判定：`git diff --stat -- backend/app/settings.py` 无输出 = PASS。

### AC-8 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：符合 = PASS。

## 本项目专属检查（必查）
1. **`_ENV_ACCESSORS` 映射完整性**：审阅映射表，确认 `CONFIG_REGISTRY` 里每个 key 都有对应的 env 访问器，没有遗漏（遗漏的字段在"DB 无值"时会解析失败/返回 None，需确认这种情况有合理的兜底行为而不是抛未捕获异常）。
2. **架构边界**：`app/config/repository.py`/`resolver.py` 未 import `app.routers.*`/`app.main`。
3. **文件所有权**：`git status --porcelain` 中改动应限于 `app/config/repository.py`（新）、`app/config/resolver.py`（新）、`tests/test_rnd248_config_resolver.py`（新）。

## 附加检查（Security）
- 测试中的密钥值为测试专用固定值，非真实凭据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
.venv/bin/python -c "from app.db.models import AppConfigStore; from app.config.crypto import encrypt_value, decrypt_value; from app.config.schema import CONFIG_REGISTRY; print('OK')"
make verify
.venv/bin/python -m pytest backend/tests/test_rnd248_config_resolver.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/settings.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-248-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中转录 `_ENV_ACCESSORS` 映射表（供 T5 review）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-1 若解析顺序颠倒 → 直接 FAIL（blocker）**。
- **AC-4 若密钥解密缺失 → 直接 FAIL（blocker）**。
