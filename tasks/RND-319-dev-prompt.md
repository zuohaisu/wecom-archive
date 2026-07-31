[Goal check] This work advances 开发（Development） by 交付按租户留存策略锁定到期消息的幂等批处理脚本，收口 C3 epic。

# RND-319 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-319 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-319「C3-2 到期锁定/清理任务」｜父 Epic RND-272（C3 数据留存策略）
- 优先级：Medium｜风险等级：**R2**（批量触及生产消息数据；范围已刻意收窄为"锁定"，不做硬删除，见下）｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实）

C3-1（<issue>RND-318</issue>，留存配置表）**已 Done 并已上线**：`backend/app/db/models.py` 的 `RetentionConfig`（`tenant_id`/`retention_days`/`is_locked`），端点 `GET`/`PUT /api/admin/retention-config`（`backend/app/routers/retention.py`）。本票**消费**这张表，不改它。

**本项目的批处理脚本范式**：`backend/scripts/` 下已有一批 `*_once.py` 幂等批处理脚本（如 `backfill_media_file_size_once.py`、`reencrypt_app_secrets_once.py`），都是"可重复运行、只处理尚未处理的部分、不依赖内置调度器"的模式。**本票照这个范式写，不要引入新的任务队列/调度框架**（这个项目目前没有 Celery/cron-in-app 之类的东西，定时触发由部署侧的 cron/systemd timer 负责，脚本本身只负责"跑一次、幂等、可重复"）。

**❗ 关键澄清（本票最容易踩的坑）：**
- `RetentionConfig.is_locked` 是**策略本身是否被锁定不可再改**（RND-318 的语义，防止管理员事后放宽期限），**不是**"消息是否已锁定"。本票是全新概念——给 `ArchiveMessage` 加一个**独立的**"该条消息已过期锁定"标记，两者含义完全不同，命名上要避免混淆（建议 `retention_locked_at`，不要叫 `is_locked`）。
- `ArchiveMessage` 已有 `is_revoked`/`revoked_at`（`models.py:565-566`）——那是**企微消息撤回**功能的字段，与留存到期锁定是**完全不同的业务概念**（撤回是发送方主动做的；留存锁定是系统按时间到期自动做的）。**绝不能复用或混用这两组字段**，必须新增独立字段。

## 目标（Goal）
交付一个幂等的批处理脚本：对每个**已配置**留存策略的租户，把 `msgtime` 早于 `now - retention_days` 且尚未锁定的消息标记为"留存到期锁定"，并写审计。

## 范围边界（**已刻意收窄，不要自行扩大**）

**In scope：**
1. **迁移** `0028_archive_message_retention_lock.py`：`archive_messages` 表新增可空列 `retention_locked_at`（`DateTime(timezone=True)`, nullable，无需回填，纯新增列，代价低）。
2. `app/db/models.py`：`ArchiveMessage` **仅新增** `retention_locked_at` 列定义。
3. `backend/scripts/apply_retention_lock_once.py`（**新建**）：
   - 核心函数 `run_retention_lock_once(db: Session, *, now: Optional[datetime] = None) -> dict[str, int]`（`now` 参数化，供测试注入固定时间，不用 `datetime.now()` 硬编码在函数体里）。
   - 只处理 `RetentionConfig` 表里**存在配置行**的租户（`configured` 状态，见 RND-318 的 `GET` 语义——没配置的租户完全不受影响，永久保留，这是刻意的默认行为，不是遗漏）。
   - 对每个已配置租户：`UPDATE archive_messages SET retention_locked_at = :now WHERE tenant_id = :tid AND msgtime < :cutoff AND retention_locked_at IS NULL`（`cutoff = now - timedelta(days=retention_days)`）。
   - 每个租户若锁定条数 > 0，调用既有 `app/audit.py:54` 的 `write_audit(db, tenant_id=t.id, action="retention.messages_locked", object_type="tenant", detail={"locked_count": N, "cutoff": cutoff.isoformat()})`（fail-safe，**不要重造**）。
   - 返回 `{tenant_id: locked_count, ...}` 摘要字典，供脚本 CLI 层打印/日志。
   - CLI 入口（仿 `backfill_media_file_size_once.py` 的写法）：直接跑 `run_retention_lock_once(db)` 并打印摘要，**不 commit 到 git，不接入应用内调度器**。
4. 测试：`backend/tests/test_rnd319_retention_lock.py`。

**Out of scope（显式非目标，故意收窄，不是遗漏）：**
- **不做硬删除**（"清理"）：本票只实现"锁定"（标记 `retention_locked_at`），**不删除任何行、不清空任何字段**。硬删除是不可逆操作，风险量级完全不同，需要独立工单 + 更高审阅门槛，不在本票范围。
- **不改任何读路径**：`timeline_service` / `listing_service` / `external_contacts` 等现有查询**不检查** `retention_locked_at`（即本票暂不影响任何人能看到什么）。让"锁定"字段在读路径上真正生效（比如锁定后消息不可查看/不可导出）是独立的后续工单，本票只负责"打标记 + 审计"这一步。
- **不接入应用内定时任务框架**：本项目没有这类基础设施，不新增。部署侧如何定时触发这个脚本，由 Haisu 之后另行决定（cron/systemd timer/手动跑）。
- 不改 `RetentionConfig` / `retention.py`（RND-318 已交付，只读取 `retention_days`）。

**本工单拥有的文件（只许写这些）：**
- `backend/alembic/versions/0028_archive_message_retention_lock.py`（新，若 head 漂移则改用实际顺延版本号）
- `backend/app/db/models.py` —— **仅新增** `ArchiveMessage.retention_locked_at` 列
- `backend/scripts/apply_retention_lock_once.py`（新）
- `backend/tests/test_rnd319_retention_lock.py`（新）

**只读、绝不可写：** `app/routers/retention.py`、`app/db/models.py` 里的 `RetentionConfig`（只查询）、`app/audit.py`（只调用 `write_audit`）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 到期消息被锁定**：构造一条 `msgtime` 早于 `now - retention_days` 的消息 + 该租户已配置 `retention_days` → 跑脚本后该消息 `retention_locked_at` 被置为 `now`。
- **AC-2 未到期消息不受影响**：`msgtime` 晚于 cutoff 的消息 → `retention_locked_at` 仍为 `None`。
- **AC-3 未配置租户完全不受影响（关键）**：租户没有 `RetentionConfig` 行（`configured=False`）→ 该租户所有消息（无论多老）`retention_locked_at` 均不被触碰。须有测试显式验证"老消息 + 无配置 = 不锁定"，防止误锁没配置留存策略的租户。
- **AC-4 幂等**：连续跑两次脚本，第二次不重复锁定已锁定的消息（`retention_locked_at` 不被覆盖成新的时间戳），且第二次运行摘要中该租户 `locked_count` 应为 0（或反映"这次新锁的"而非"全部已锁的"，需在实现中明确语义并在测试断言）。
- **AC-5 审计**：每个本次锁定条数 > 0 的租户产生一条 `AuditLog`（`action="retention.messages_locked"`），经 `write_audit` 写入。锁定条数为 0 的租户**不**产生空审计记录（避免刷屏）。
- **AC-6 不做删除**：`git diff` 中不得出现任何 `DELETE` / `db.delete(...)` 语句针对 `ArchiveMessage`；`retention_locked_at` 之外的字段一律不变。
- **AC-7 未复用/混淆 `is_revoked`**：`grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py` **应无命中**——本票不得读写撤回字段。
- **AC-8 迁移可逆 + 回归**：`upgrade`/`downgrade` 均可执行；`alembic check` 无 drift；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
git pull   # 确认 alembic head 仍是 0027，若漂移按实际顺延
make verify
.venv/bin/python -m pytest backend/tests/test_rnd319_retention_lock.py -q
.venv/bin/python -m alembic check
grep -n "is_revoked\|revoked_at" backend/scripts/apply_retention_lock_once.py   # AC-7：应无命中
grep -n "db.delete\|DELETE FROM" backend/scripts/apply_retention_lock_once.py  # AC-6：应无命中
git diff -- backend/app/db/models.py    # 人工核对：只新增 retention_locked_at
git status --porcelain
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
- 风险 2：把"策略锁定"（`RetentionConfig.is_locked`）和"消息锁定"（`retention_locked_at`）搞混，写出的代码逻辑上锁错对象——由「关键澄清」段落 + AC-7 命名规范防守。
- 风险 3：脚本非幂等，重复跑产生错误的审计记录/覆盖时间戳——由 AC-4 防守。
- 回滚：`downgrade` 迁移即可（新增列不影响既有数据）；`retention_locked_at` 全部为 `NULL` 时业务行为等同于本票未上线。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2）**：批量触及生产消息数据，**Haisu 需人工审阅脚本的 SQL/查询逻辑**后才可 approve commit；且**在部署侧真正接入定时触发前，必须先在生产手动跑一次并核对锁定条数是否符合预期**（脚本交付本身不代表已经在生产启用）。
- **Escalation**：若"锁定"语义在实现中发现需要联动读路径才有意义（例如产品期望的效果是"锁定后立刻从审阅台消失"）→ `BLOCKED_NEEDS_HUMAN`，说明具体产品诉求，**不要**为了让"锁定"看起来生效而顺手去改 `timeline_service`/`listing_service` 等只读文件。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/scripts/backfill_media_file_size_once.py`（`_once` 脚本范式参考）、`app/db/models.py` 的 `RetentionConfig` 与 `ArchiveMessage`（重点区分 `is_locked` vs 待新增的 `retention_locked_at`，以及既有 `is_revoked`/`revoked_at` 不可混用）、`app/audit.py:54`（`write_audit`）。
2. `git pull` 确认 alembic head，写迁移（新增列）。
3. 写 `apply_retention_lock_once.py`：`run_retention_lock_once` 核心函数 + CLI 入口。
4. 写测试覆盖 AC-1~AC-5（**AC-3「未配置租户不受影响」是重点**）。
5. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假消息数据，固定 `now` 注入，不依赖真实系统时间。
- 不扩大 Scope：硬删除、读路径联动、应用内调度器一律 Out。
- 复用优先：`write_audit` 只调用不重写；脚本范式仿 `scripts/*_once.py`。
- 证据优先，以 exit 0 / 测试通过为证。
