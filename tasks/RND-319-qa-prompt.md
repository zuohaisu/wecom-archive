[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-319 的 8 条 AC（重点验证未配置租户零影响、幂等、不做硬删除、不混淆撤回字段）并产出带证据的 PASS/FAIL 判定。

# RND-319 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-319「C3-2 到期锁定/清理任务」｜风险等级 **R2**（批量触及生产消息数据）
- **AC-3（未配置租户零影响）与 AC-6（不做硬删除）是本票最高风险项，从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 到期消息被锁定
- 证据：测试构造过期消息 + 已配置租户，跑脚本后 `retention_locked_at` 被置为传入的 `now`。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 未到期消息不受影响
- 判定：`msgtime` 在 cutoff 之后的消息 `retention_locked_at` 仍为 `None` = PASS。

### AC-3 — 未配置租户完全不受影响（关键）
- 证据：测试构造一个**没有** `RetentionConfig` 行的租户，其中有极老的消息（比如 msgtime 是几年前）→ 跑脚本后这些消息 `retention_locked_at` 仍为 `None`。
- 判定：符合 = PASS。**若未配置租户的老消息被误锁 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）**——这会在没有任何租户主动配置留存策略的情况下静默影响数据可见性，是本票最危险的失败模式。

### AC-4 — 幂等
- 证据：测试连续跑两次脚本，断言第二次运行**不**改变已锁定消息的 `retention_locked_at` 时间戳（不是覆盖成新时间），且第二次的锁定计数摘要能反映"这次新锁的是 0（或对应新过期的）"而非重复计入已锁定的旧消息。
- 判定：真正幂等 = PASS。**若重复运行导致时间戳被覆盖或计数重复累加 → FAIL（`IMPLEMENTATION_DEFECT`, major）**。

### AC-5 — 审计
- 证据：锁定条数 > 0 的租户产生恰好一条 `AuditLog`（`action="retention.messages_locked"`），经 `write_audit` 写入；锁定条数为 0 的租户**不**产生审计记录。
- 判定：符合 = PASS。**缺审计 → FAIL（`SECURITY_VIOLATION`, major，合规产品的批量数据操作必须留痕）**；空审计刷屏（0 条也写审计）→ 记 minor finding。

### AC-6 — 不做删除（关键）
- 证据：`grep -n "db.delete\|DELETE FROM\|\.delete(" backend/scripts/apply_retention_lock_once.py` **应无命中**；审阅脚本确认唯一写操作是 `UPDATE ... SET retention_locked_at`。
- 判定：无删除逻辑 = PASS。**发现任何删除 `ArchiveMessage` 行或清空其他字段的代码 → 直接 FAIL（`SCOPE_VIOLATION`, severity: blocker）**——本票明确排除硬删除，这是不可逆操作，agent 擅自实现是严重越界。

### AC-7 — 未混淆撤回字段
- 证据：`grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py` **应无命中**。
- 判定：无命中 = PASS。**读写了 `is_revoked`/`revoked_at` → FAIL（`IMPLEMENTATION_DEFECT`, major）**——那是企微消息撤回字段，与留存到期锁定是完全不同的业务概念，混用会产生错误的消息状态。

### AC-8 — 迁移可逆 + 回归
- 判定：`alembic upgrade`/`downgrade` 均可执行、`alembic check` 无 drift = PASS；`make verify` exit 0；`test_architecture_boundary.py` 通过。

## 本项目专属检查（必查）
1. **未改 `RetentionConfig`/`retention.py`**：`git diff --stat -- backend/app/routers/retention.py` 应无输出（本票只读取该表，不改写入口）。
2. **`now` 已参数化**：审阅 `run_retention_lock_once` 签名，确认 `now` 是可注入参数而非函数体内硬编码 `datetime.now()`/`utcnow()`——否则测试无法确定性构造过期/未过期边界，且测试若真依赖系统当前时间会 flaky。
3. **未接入应用内调度器**：diff 中不应出现 Celery/APScheduler 或任何新的常驻任务框架引入。
4. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `alembic/versions/0028_*.py`（或实际顺延版本号）、`app/db/models.py`（仅新增 `retention_locked_at`）、`scripts/apply_retention_lock_once.py`（新）、`tests/test_rnd319_retention_lock.py`（新）。

## 附加检查（Security）
- 测试中使用固定假租户/消息数据，非真实客户数据。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd319_retention_lock.py -q
.venv/bin/python -m alembic check
grep -n "db.delete\|DELETE FROM\|\.delete(" backend/scripts/apply_retention_lock_once.py   # AC-6：应无命中
grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py               # AC-7：应无命中
grep -n "def run_retention_lock_once" -A 5 backend/scripts/apply_retention_lock_once.py     # 核对 now 是否为参数
git diff --stat -- backend/app/routers/retention.py    # 应无输出
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-319-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：脚本调用签名、建议的部署侧触发方式（cron 表达式）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-3 若未配置租户的消息被误锁 → 直接 FAIL（blocker）**，这是最高风险项。
- **AC-6 若发现任何硬删除逻辑 → 直接 FAIL（blocker）**，本票明确排除删除。
