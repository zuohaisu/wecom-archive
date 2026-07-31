[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-319 的 8 条 AC（重点验证未配置租户零影响、幂等、不改 archive_messages 表结构、零连累既有测试）并产出带证据的 PASS/FAIL 判定。

# RND-319 验收提示词（Acceptance / QA Prompt）

## ⚠️ 2026-07-31：本版取代旧稿（设计反转，见 dev prompt 开头说明）

旧版方案是给 `ArchiveMessage` 加 `retention_locked_at` 列，实测导致 508 个不相关既有测试失败（`archive_messages` 表结构被十余个测试文件手写复刻）。已改为**新建独立表** `retention_locks`（一条消息一行，`archive_message_id` 唯一约束），`archive_messages` 表结构零改动。验收时按本版 AC，若交付物仍是"加列"方案，判 `BLOCKED`（用的是旧稿）。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-319「C3-2 到期锁定/清理任务」｜风险等级 **R2**（批量触及生产消息数据）
- **AC-3（未配置租户零影响）、AC-6（不改 archive_messages 表结构）、AC-8（零连累既有测试）是本票最高风险项，从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 到期消息被锁定
- 证据：测试构造过期消息 + 已配置租户，跑脚本后 `retention_locks` 表出现对应 `archive_message_id` 的一行，`locked_at` 为传入的 `now`。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 未到期消息不受影响
- 判定：`msgtime` 在 cutoff 之后的消息，`retention_locks` 里没有对应行 = PASS。

### AC-3 — 未配置租户完全不受影响（关键）
- 证据：测试构造一个**没有** `RetentionConfig` 行的租户，其中有极老的消息（比如 msgtime 是几年前）→ 跑脚本后这些消息在 `retention_locks` 里**没有**对应行。
- 判定：符合 = PASS。**若未配置租户的老消息被误锁 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——这会在没有任何租户主动配置留存策略的情况下静默影响数据可见性，是本票最危险的失败模式。

### AC-4 — 幂等
- 证据：测试连续跑两次脚本，断言第二次运行**不**为已锁定的消息重复插入 `retention_locks` 行（`archive_message_id` 唯一约束应天然阻止，脚本查询逻辑也应提前排除，不能靠捕获约束异常兜底），第二次运行摘要中该租户 `locked_count` 反映"这次新锁的"而非重复计入旧的。
- 判定：真正幂等 = PASS。**若重复运行导致重复插入（唯一约束报错未被处理）或计数重复累加 → FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-5 — 审计
- 证据：新锁定条数 > 0 的租户产生恰好一条 `AuditLog`（`action="retention.messages_locked"`），经 `write_audit` 写入；新锁定条数为 0 的租户**不**产生审计记录。
- 判定：符合 = PASS。**缺审计 → FAIL（`SECURITY_VIOLATION`, major，合规产品的批量数据操作必须留痕）**；空审计刷屏（0 条也写审计）→ 记 minor finding。

### AC-6 — 不做删除 + 不改 `archive_messages` 表结构（关键）
- 证据：`grep -n "db.delete\|DELETE FROM\|\.delete(" backend/scripts/apply_retention_lock_once.py` **应无命中**；审阅脚本确认对 `retention_locks` 只有 INSERT，对 `archive_messages` 只有只读 SELECT。`git diff -- backend/app/db/models.py` 中 `ArchiveMessage` 类范围内**必须零改动**，只应看到新增的 `RetentionLock` 类。
- 判定：无删除逻辑 + `ArchiveMessage` 零改动 = PASS。**发现任何删除 `ArchiveMessage` 行的代码、或 `ArchiveMessage` 类被新增列/改动 → 直接 FAIL（`SCOPE_VIOLATION`, severity: blocker）**——本票被重新设计的唯一原因就是不碰这张表，这条是本票存在的核心边界。

### AC-7 — 未混淆撤回字段
- 证据：`grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py` **应无命中**。
- 判定：无命中 = PASS。**读写了 `is_revoked`/`revoked_at` → FAIL（`IMPLEMENTATION_DEFECT`, major）**——那是企微消息撤回字段，与留存到期锁定是完全不同的业务概念。

### AC-8 — 迁移可逆 + 回归 + 零连累既有测试（关键）
- 证据：`alembic upgrade`/`downgrade` 均可执行、`alembic check` 无 drift；**`make verify` 全项目全绿，包括之前"加列方案"下失败的那批测试（`tests/fakes.py` 相关、`test_staff_seats.py`、`test_http_contract.py` 等）现在应恢复原样通过**；`test_architecture_boundary.py` 通过。
- 判定：全部符合 = PASS。**若仍有任何既有测试因本票失败 → 直接 FAIL（`REGRESSION`, severity: blocker）**——这正是本票被重新设计要解决的问题，若仍然发生说明重新设计没有真正生效。

## 本项目专属检查（必查）
1. **未改 `RetentionConfig`/`retention.py`**：`git diff --stat -- backend/app/routers/retention.py` 应无输出（本票只读取该表，不改写入口）。
2. **`now` 已参数化**：审阅 `run_retention_lock_once` 签名，确认 `now` 是可注入参数而非函数体内硬编码 `datetime.now()`/`utcnow()`。
3. **未接入应用内调度器**：diff 中不应出现 Celery/APScheduler 或任何新的常驻任务框架引入。
4. **`RetentionLock` 表设计合理**：`archive_message_id` 应有唯一约束（幂等的关键防线）；`tenant_id` 应有索引（按租户查询/审计场景常用）。
5. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `alembic/versions/0030_*.py`（或实际顺延版本号）、`app/db/models.py`（仅新增 `RetentionLock`）、`scripts/apply_retention_lock_once.py`（新）、`tests/test_rnd319_retention_lock.py`（新）。**不应出现任何既有测试文件**（`tests/fakes.py`、`test_http_contract.py`、`test_staff_seats.py` 等一律不应在改动列表里——这是与旧方案的关键区别，见 AC-8）。

## 附加检查（Security）
- 测试中使用固定假租户/消息数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify   # 关键：AC-8 的全项目回归证据
.venv/bin/python -m pytest backend/tests/test_rnd319_retention_lock.py -q
.venv/bin/python -m alembic check
grep -n "db.delete\|DELETE FROM\|\.delete(" backend/scripts/apply_retention_lock_once.py   # AC-6：应无命中
grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py               # AC-7：应无命中
grep -n "def run_retention_lock_once" -A 5 backend/scripts/apply_retention_lock_once.py     # 核对 now 是否为参数
git diff -- backend/app/db/models.py    # AC-6：ArchiveMessage 类必须零改动
git diff --stat -- backend/app/routers/retention.py    # 应无输出
git status --porcelain    # 确认无既有测试文件出现
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-319-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：脚本调用签名、`RetentionLock` 表结构、建议的部署侧触发方式（cron 表达式）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-3 若未配置租户的消息被误锁 → 直接 FAIL（blocker）**，这是最高风险项。
- **AC-6 若 `ArchiveMessage`/`archive_messages` 有任何改动 → 直接 FAIL（blocker）**，本票被重新设计的核心边界。
- **AC-8 若仍有任何既有测试因本票变红 → 直接 FAIL（blocker）**，不接受"只是这几个文件的问题，无伤大雅"。
