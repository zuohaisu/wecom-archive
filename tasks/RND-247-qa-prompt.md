[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-247 的 7 条 AC（重点验证注册表字段名与 app/settings.py 逐字一致、密钥/必填标注准确）并产出带证据的 PASS/FAIL 判定。

# RND-247 验收提示词（Acceptance / QA Prompt）— 配置中心 T3

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-247「配置中心 T3：配置项元数据注册表与常量」｜风险等级 R0
- **本票是整个配置中心 epic 的地基契约，字段名或标注出错会连累 T4/T5/T8——从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 注册表结构完整
- 证据：遍历 `CONFIG_REGISTRY`，每项 `ConfigItemSpec` 的七个字段均非 `None`/缺失。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — key 与 `app/settings.py` 一致（关键）
- 证据：测试对 `CONFIG_REGISTRY` 每个 key，用其 `group` 对应的 Settings 类实例化后 `getattr(instance, key)` 不抛 `AttributeError`。
- 判定：全部命中 = PASS。**任一 key 在对应 Settings 类里找不到 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: major）**——这会导致 T4 解析器读取时直接崩溃。

### AC-3 — 密钥字段类型一致
- 证据：`is_secret=True` 的项 `value_type == "secret"`；反查 `wecom_oauth_secret`/`qiniu_secret_key`/`smtp_password` 三个确实标了 `is_secret=True`，`wecom_corp_id`/`wecom_agent_id` 确实标了 `is_secret=False`。
- 判定：符合 = PASS。**密钥字段未标 `is_secret=True` → 直接 FAIL（`SECURITY_VIOLATION`, blocker）**——这会导致 T5/T9 的掩码逻辑漏掉这个字段，明文密钥可能被 GET 接口原样返回。

### AC-4 — 企微四件套必填
- 判定：`wecom_corp_id`/`wecom_agent_id`/`wecom_oauth_secret`/`wecom_callback_token`/`wecom_callback_encoding_aes_key` 的 `required` 均为 `True` = PASS；任一非 `True` → FAIL（`IMPLEMENTATION_DEFECT`）——T6 的 bootstrap 判定依赖这五个必填。

### AC-5 — `validate_registry()` 自检生效
- 证据：测试构造重复 key 的假注册表调用 → 抛异常；正常注册表 → 不抛。
- 判定：符合 = PASS。

### AC-6 — 无歧义 key
- 判定：不存在同名字段被登记两次（来自不同分组/Settings 类）导致语义冲突 = PASS。

### AC-7 — 架构边界 + 回归
- 证据：`grep -rn "from app.routers\|from app.main" backend/app/config/` 无命中；`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：全部满足 = PASS。

## 本项目专属检查（必查）
1. **未碰 `app/settings.py`**：`git diff --stat -- backend/app/settings.py` 应无输出（本票只读取，不修改）。
2. **`restart_required` 标注合理性抽查**：审阅 3-5 个字段的 `restart_required` 值，判断是否符合直觉（如 `storage_backend` 切换存储实现类需要重启，标 `True` 合理；`wecom_admin_domain` 这类纯展示用途的字段热加载即可，标 `False` 合理）。若发现全部字段统一标了同一个值（如全 `False` 或全 `True`），大概率是偷懒没有逐项判断 → 记 finding（`IMPLEMENTATION_DEFECT`, minor）。
3. **文件所有权**：`git status --porcelain` 中改动应限于 `app/config/__init__.py`（新）、`app/config/constants.py`（新）、`app/config/schema.py`（新）、`tests/test_rnd247_config_schema.py`（新）。

## 附加检查（Security）
- 无真实密钥/凭据被写入注册表的 `default` 字段。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd247_config_schema.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -rn "from app.routers\|from app.main\|import app.routers\|import app.main" backend/app/config/
git diff --stat -- backend/app/settings.py    # 应无输出
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-247-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中完整转录 `CONFIG_REGISTRY` 的字段清单（供 T4/T5/T8 验收时直接对照）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若发现 key 与 `app/settings.py` 不一致 → 直接 FAIL（major）**，这会连累后续所有子票。
- **AC-3 若发现密钥字段未标 `is_secret` → 直接 FAIL（blocker）**。
