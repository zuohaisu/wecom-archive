[Goal check] This work advances 开发（Development） by 交付按租户留存策略锁定到期消息的幂等批处理脚本，收口 C3 epic。

# RND-319 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-319 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 2026-07-31：设计反转——从"给 archive_messages 加列"改为"新建独立表"（PM 决策，回应 dev agent 的 BLOCKED_NEEDS_HUMAN 上报）

第一版设计给 `ArchiveMessage` 加 `retention_locked_at` 列。dev agent 上报：`archive_messages` 的表结构被**至少 11 个测试文件手写复刻**（不是反射真实 ORM 元数据），其中几个是被广泛复用的共享 fixture（`tests/fakes.py`、`test_http_contract.py`、`test_staff_seats.py` 等），新增列导致 `make verify` 出现 **508 个不相关测试失败**。这不是"随手同步几个文件"能解决的规模——`archive_messages` 是这个项目测试覆盖面最广的核心表，blast radius 远超预期，是本 PM 在写第一版设计时没有先排查就直接选了"加列"方案的失误。

**决策：不碰 `archive_messages` 表结构，改为新建独立表** `retention_locks`（`id`/`tenant_id`/`archive_message_id` FK/`locked_at`，一条消息一行，"是否已锁定"= "这张表里存不存在对应行"）——这是本项目已有的先例模式（`MessageRevocation` 就是"用独立表记录某条消息的额外状态，不往 `ArchiveMessage` 本体加列"的同类设计，见 `models.py:769`）。**新建表不会破坏任何现有测试**——没有任何既有测试会去构造一张还不存在的表的行，这正是它相对"加列"的根本优势。已更新下方全部 Scope/AC，请按本版执行，不要沿用旧版"加列"方案。

## 任务身份
- 工单：RND-319「C3-2 到期锁定/清理任务」｜父 Epic RND-272（C3 数据留存策略）
- 优先级：Medium｜风险等级：**R2**（批量触及生产消息数据；范围已刻意收窄为"锁定"，不做硬删除，见下）｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实）

C3-1（<issue>RND-318</issue>，留存配置表）**已 Done 并已上线**：`backend/app/db/models.py` 的 `RetentionConfig`（`tenant_id`/`retention_days`/`is_locked`），端点 `GET`/`PUT /api/admin/retention-config`（`backend/app/routers/retention.py`）。本票**消费**这张表，不改它。

**本项目的批处理脚本范式**：`backend/scripts/` 下已有一批 `*_once.py` 幂等批处理脚本（如 `backfill_media_file_size_once.py`、`reencrypt_app_secrets_once.py`），都是"可重复运行、只处理尚未处理的部分、不依赖内置调度器"的模式。**本票照这个范式写，不要引入新的任务队列/调度框架**（这个项目目前没有 Celery/cron-in-app 之类的东西，定时触发由部署侧的 cron/systemd timer 负责，脚本本身只负责"跑一次、幂等、可重复"）。

**❗ 关键澄清（本票最容易踩的坑）：**
- `RetentionConfig.is_locked` 是**策略本身是否被锁定不可再改**（RND-318 的语义，防止管理员事后放宽期限），**不是**"消息是否已锁定"。本票是全新概念，命名/表都要独立，不要混淆。
- `ArchiveMessage` 已有 `is_revoked`/`revoked_at`（`models.py:565-566`）——那是**企微消息撤回**功能的字段，与留存到期锁定是**完全不同的业务概念**。**绝不能复用或混用这两组字段。**
- **`ArchiveMessage`/`archive_messages` 是只读引用对象，本票绝不新增列、绝不修改其表结构**（见上方设计反转说明——这是本票唯一、也是最重要的一条纪律）。

## 目标（Goal）
交付一个幂等的批处理脚本：对每个**已配置**留存策略的租户，把 `msgtime` 早于 `now - retention_days` 且尚未锁定的消息，在新建的 `retention_locks` 表里各插入一行记录（= 锁定），并写审计。

## 范围边界（**已刻意收窄，不要自行扩大**）

**In scope：**
1. **迁移** `0030_retention_locks.py`（或实际顺延版本号）：**新建独立表** `retention_locks`：
   - `id`（`String(36)`, 主键）
   - `tenant_id`（`String(36)`, `ForeignKey("tenants.id")`, not null——供按租户查询/审计使用）
   - `archive_message_id`（`Integer`, `ForeignKey("archive_messages.id")`, not null，**唯一约束**——一条消息最多一条锁定记录，这个唯一约束本身就是幂等的保证，见下）
   - `locked_at`（`DateTime(timezone=True)`, not null, server_default `now()`）
   - **`archive_messages` 表结构本身零改动**——迁移只 `CREATE TABLE`，不对 `archive_messages` 做任何 `ALTER`。
2. `app/db/models.py`：**新增** `RetentionLock` ORM 类（放在 `MessageRevocation` 附近，参考其"独立表记录消息附加状态"的写法），**不改** `ArchiveMessage` 类一个字符。
3. `backend/scripts/apply_retention_lock_once.py`（**新建**）：
   - 核心函数 `run_retention_lock_once(db: Session, *, now: Optional[datetime] = None) -> dict[str, int]`（`now` 参数化，供测试注入固定时间，不用 `datetime.now()` 硬编码在函数体里）。
   - 只处理 `RetentionConfig` 表里**存在配置行**的租户（`configured` 状态，见 RND-318 的 `GET` 语义——没配置的租户完全不受影响，永久保留，这是刻意的默认行为，不是遗漏）。
   - 对每个已配置租户：查询 `msgtime < cutoff`（`cutoff = now - timedelta(days=retention_days)`）且**在 `retention_locks` 里还没有对应行**的 `ArchiveMessage`（`NOT EXISTS`/`LEFT JOIN ... IS NULL` 均可），为每条命中的消息插入一行 `RetentionLock(archive_message_id=..., tenant_id=..., locked_at=now)`。
   - 每个租户若新锁定条数 > 0，调用既有 `app/audit.py:54` 的 `write_audit(db, tenant_id=t.id, action="retention.messages_locked", object_type="tenant", detail={"locked_count": N, "cutoff": cutoff.isoformat()})`（fail-safe，**不要重造**）。
   - 返回 `{tenant_id: locked_count, ...}` 摘要字典（`locked_count` = 本次新插入的行数，不是累计总数），供脚本 CLI 层打印/日志。
   - CLI 入口（仿 `backfill_media_file_size_once.py` 的写法）：直接跑 `run_retention_lock_once(db)` 并打印摘要，**不 commit 到 git，不接入应用内调度器**。
4. 测试：`backend/tests/test_rnd319_retention_lock.py`。

**Out of scope（显式非目标，故意收窄，不是遗漏）：**
- **不做硬删除**（"清理"）：本票只实现"锁定"（`retention_locks` 插入记录），**不删除 `archive_messages` 任何行、不清空任何字段**。硬删除是不可逆操作，风险量级完全不同，需要独立工单 + 更高审阅门槛，不在本票范围。
- **不改任何读路径**：`timeline_service` / `listing_service` / `external_contacts` 等现有查询**不检查** `retention_locks`（即本票暂不影响任何人能看到什么）。让"锁定"在读路径上真正生效（比如锁定后消息不可查看/不可导出）是独立的后续工单。
- **不接入应用内定时任务框架**：本项目没有这类基础设施，不新增。部署侧如何定时触发这个脚本，由 Haisu 之后另行决定（cron/systemd timer/手动跑）。
- 不改 `RetentionConfig` / `retention.py`（RND-318 已交付，只读取 `retention_days`）。
- **不改 `ArchiveMessage`/`archive_messages`**（见上方设计反转说明，这是本票最高优先级的边界）。

**本工单拥有的文件（只许写这些）：**
- `backend/alembic/versions/0030_retention_locks.py`（新，若 head 漂移则改用实际顺延版本号）
- `backend/app/db/models.py` —— **仅新增** `RetentionLock` 类；**不得触碰** `ArchiveMessage` 类的任何一行
- `backend/scripts/apply_retention_lock_once.py`（新）
- `backend/tests/test_rnd319_retention_lock.py`（新）

**只读、绝不可写：** `app/db/models.py` 里的 `ArchiveMessage`/`RetentionConfig`（只查询）、`app/routers/retention.py`、`app/audit.py`（只调用 `write_audit`）、其他票拥有的一切文件（**尤其是任何手写 `archive_messages` 测试 fixture 的文件——本设计下不应该需要碰它们，若发现仍然需要改动，说明设计理解有误，立即上报，不要动手改**）。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 到期消息被锁定**：构造一条 `msgtime` 早于 `now - retention_days` 的消息 + 该租户已配置 `retention_days` → 跑脚本后 `retention_locks` 表出现对应 `archive_message_id` 的一行。
- **AC-2 未到期消息不受影响**：`msgtime` 晚于 cutoff 的消息 → `retention_locks` 里没有对应行。
- **AC-3 未配置租户完全不受影响（关键）**：租户没有 `RetentionConfig` 行（`configured=False`）→ 该租户所有消息（无论多老）均不在 `retention_locks` 里出现。须有测试显式验证"老消息 + 无配置 = 不锁定"，防止误锁没配置留存策略的租户。
- **AC-4 幂等**：连续跑两次脚本，第二次不为已锁定的消息重复插入 `retention_locks` 行（唯一约束 `archive_message_id` 天然防重复插入；脚本自身的查询逻辑也要提前排除已锁定的，不能靠捕获唯一约束异常兜底），第二次运行摘要中该租户 `locked_count` 应为 0（除非期间又有新消息过期）。
- **AC-5 审计**：每个本次新锁定条数 > 0 的租户产生一条 `AuditLog`（`action="retention.messages_locked"`），经 `write_audit` 写入。新锁定条数为 0 的租户**不**产生空审计记录（避免刷屏）。
- **AC-6 不做删除、不改核心表（关键）**：`git diff` 中不得出现任何 `DELETE`/`db.delete(...)` 针对 `ArchiveMessage`；**`git diff --stat -- backend/app/db/models.py` 中 `ArchiveMessage` 类范围内必须零改动**（只应看到新增的 `RetentionLock` 类）；`archive_messages` 表结构本身的迁移历史不应出现新的 `ALTER TABLE`。
- **AC-7 未复用/混淆 `is_revoked`**：`grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py` **应无命中**——本票不得读写撤回字段。
- **AC-8 迁移可逆 + 回归 + 零测试连累（关键）**：`upgrade`/`downgrade` 均可执行；`alembic check` 无 drift；`make verify` 全绿——**包括之前因为"加列方案"导致失败的全部既有测试，现在必须原样全绿，不需要改动它们一个字符**；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
git pull   # 确认 alembic head，见「迁移序位」
make verify   # 关键：全项目回归，验证零连累（AC-8）
.venv/bin/python -m pytest backend/tests/test_rnd319_retention_lock.py -q
.venv/bin/python -m alembic check
grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py   # AC-7：应无命中
grep -n "db.delete\|DELETE FROM" backend/scripts/apply_retention_lock_once.py  # AC-6：应无命中
git diff -- backend/app/db/models.py    # AC-6：人工核对 ArchiveMessage 类零改动，只新增 RetentionLock
git status --porcelain   # 确认没有任何既有测试文件出现在改动列表里
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-318（C3-1）**已 Done 并已核实落地**。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-8 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明脚本的部署侧调用方式建议（cron 表达式示例，不要求真的接入）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：脚本被误跑在未配置留存策略的租户上导致误锁定——由 AC-3 防守，这是本票最该测的场景。
- 风险 2：把"策略锁定"（`RetentionConfig.is_locked`）和"消息锁定"（`retention_locks` 表）搞混，写出的代码逻辑上锁错对象——由「关键澄清」段落防守。
- 风险 3：脚本非幂等，重复跑产生重复的 `retention_locks` 行/错误的审计记录——由 AC-4 防守，唯一约束是最后一道防线。
- 风险 4：不小心又改回给 `archive_messages` 加列的旧方案——由 AC-6 防守，这是本票被重新设计的根本原因。
- 回滚：`downgrade` 迁移（`DROP TABLE retention_locks`）即可；`archive_messages` 未被触碰，零回滚代价。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2）**：批量触及生产消息数据的批处理逻辑，**Haisu 需人工审阅脚本的 SQL/查询逻辑**后才可 approve commit；且**在部署侧真正接入定时触发前，必须先在生产手动跑一次并核对锁定条数是否符合预期**（脚本交付本身不代表已经在生产启用）。
- **Escalation**：若"锁定"语义在实现中发现需要联动读路径才有意义（例如产品期望的效果是"锁定后立刻从审阅台消失"）→ `BLOCKED_NEEDS_HUMAN`，说明具体产品诉求，**不要**为了让"锁定"看起来生效而顺手去改 `timeline_service`/`listing_service` 等只读文件。若发现新表设计仍然无法完全避免碰 `archive_messages` 相关的既有测试文件 → `BLOCKED_NEEDS_HUMAN`，说明具体哪个文件、为什么，不要绕过本票的核心边界自行改动。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/scripts/backfill_media_file_size_once.py`（`_once` 脚本范式参考）、`app/db/models.py` 的 `RetentionConfig`、`MessageRevocation`（独立表记录消息附加状态的范式，直接照抄这个思路）、`app/audit.py:54`（`write_audit`）。
2. `git pull` 确认 alembic head，写迁移（**新建表**，不碰 `archive_messages`）。
3. 写 `apply_retention_lock_once.py`：`run_retention_lock_once` 核心函数 + CLI 入口。
4. 写测试覆盖 AC-1~AC-5（**AC-3「未配置租户不受影响」是重点**）。
5. 跑 `make verify` 全项目回归，确认**没有任何既有测试因为本票而变红**（AC-8）。
6. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假消息数据，固定 `now` 注入，不依赖真实系统时间。
- 不扩大 Scope：硬删除、读路径联动、应用内调度器一律 Out。
- 复用优先：`write_audit` 只调用不重写；脚本范式仿 `scripts/*_once.py`。
- 证据优先，以 exit 0 / 测试通过为证。
