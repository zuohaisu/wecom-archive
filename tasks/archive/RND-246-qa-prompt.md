[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-246 的 6 条 AC（重点验证无 tenant_id、无外键约束、迁移可逆）并产出带证据的 PASS/FAIL 判定。

# RND-246 验收提示词（Acceptance / QA Prompt）— 配置中心 T1

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-246「配置中心 T1：app_config_store 表」｜风险等级 R1

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 建表成功
- 证据：迁移后 `app_config_store` 表存在，字段（`key`/`group`/`value`/`is_secret`/`value_type`/`requires_restart`/`updated_at`/`updated_by`）齐全，`key` 为主键。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 无 `tenant_id`（关键，冻结设计 G2）
- 证据：`AppConfigStore` 模型定义与对应迁移文件中**不含** `tenant_id` 列。
- 判定：无 = PASS。**有 `tenant_id` → 直接 FAIL（`SCOPE_VIOLATION`, blocker）**——这是冻结设计（G2，全局单例，YAGNI），加了会制造出与设计文档矛盾的表结构，且会让人误以为这是租户级配置，与 `RetentionConfig`/`TenantWecomConfig` 混淆。

### AC-3 — 无外键约束
- 证据：`updated_by` 列不是 `ForeignKey`。
- 判定：符合 = PASS；若建了到 `admin_users` 的外键 → 记 finding（`SCOPE_VIOLATION`, minor——不是最高风险但违反了 dev prompt 的明确约束）。

### AC-4 — CRUD 基本可用
- 证据：测试直接用 ORM 插入/更新/查询一条配置项，行为符合预期。
- 判定：符合 = PASS。

### AC-5 — 迁移可逆
- 证据：`alembic upgrade`/`downgrade` 均可执行；`alembic check` 无 drift。
- 判定：符合 = PASS。

### AC-6 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：符合 = PASS。

## 本项目专属检查（必查）
1. **未接入任何业务逻辑**：`git status --porcelain` 中不应出现任何 router/service 文件改动——本票只交付表结构。
2. **文件所有权**：改动应限于 `alembic/versions/0030_*.py`（或实际顺延版本号）、`app/db/models.py`（仅新增 `AppConfigStore`）、`tests/test_rnd246_app_config_store.py`（新）。
   > 若 RND-319/RND-304 同天并行，三票都会各自新增迁移文件——先 `git status` 分离归因。

## 附加检查（Security）
- 测试中不含真实密钥/凭据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd246_app_config_store.py -q
.venv/bin/python -m alembic check
grep -n "tenant_id" backend/app/db/models.py    # AC-2：人工核对上下文，确认不在 AppConfigStore 类范围内
git diff -- backend/app/db/models.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-246-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若发现 `tenant_id` 列 → 直接 FAIL（blocker）**，这是冻结设计。
