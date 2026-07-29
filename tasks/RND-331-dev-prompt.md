[Goal check] This work advances 开发（Development） by 回填 media_files.file_size 历史 NULL、收紧为 NOT NULL、并建立按租户的存储用量日滚动汇总，使「按存储量计费」有可信计量口径。

# RND-331 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-331「D1 存储计量修复：file_size 回填 + NOT NULL + 按租户日滚动汇总表」｜Linear team `Builder`
- 优先级：High｜风险等级：**R2（含 Alembic migration + 数据回填）**
- 波次：R1（与 4 张页面票并行，但文件完全不重叠）

## ⚠️ R2 风险等级的额外规则（与其他 R1 票不同，必读）
本票包含**数据库迁移与历史数据回填**，按 `docs/ticket-autopilot-workflow.md` 的风险分级属 **R2 = 转人工确认后执行**：
- 你可以**编写** migration 与回填脚本，并在**本地 / 测试库**上验证。
- 你**不得**对任何生产库或共享库执行回填与迁移。
- 生产执行由 Haisu 审阅后人工触发（CI 的 `alembic upgrade head` 在部署时跑）。
- 回填脚本必须**幂等**且**可分批**，支持先 dry-run 再实际写入。

## 背景与项目现状
2026-07-29 拍板：**云版本按存储量付费**。计费口径定义为：

```sql
SELECT tenant_id, SUM(file_size) FROM media_files GROUP BY tenant_id;
```

但实地核实发现（`backend/app/db/models.py:607`）：

```python
file_size = Column(BigInteger, nullable=True)   # ← 可为空
```

**不能拿一个允许为空的列去开发票。** 且 `MediaFile` 已有 `tenant_id`（`models.py:70`）与存储 provider 字段（`local` / `qiniu_kodo`），回填时需按 provider 分别取真实字节数。

**为什么现在做**：当前数据量还小，回填是一次脚本；等数据涨上去、又跨了本地与七牛两个 provider，回填就变成一次跨 provider 对账事故。

**既有可复用资产（先读，不要重造）：**
- `backend/app/media_storage.py` —— 存储抽象层，已有按 provider 取文件的能力。
- `backend/app/qiniu_storage.py` —— 七牛 provider。
- `backend/scripts/` 下已有多个一次性回填脚本范式（如 `backfill_thumbnails_once.py`、`backfill_missing_seqs_once.py`），**照它们的结构写**，不要发明新范式。
- `backend/alembic/versions/` —— 迁移目录，最新 head 见 `backend/scripts/verify_alembic_head.py`。

**❗ 本项目高频踩坑：**
- **Alembic schema drift 是 CI 硬闸**：ORM 模型与迁移必须一致，CI 会跑 `python -m alembic check`。改了 `models.py` 就必须有对应 migration，否则 CI 红。
- **架构边界硬闸**：service 层不得 import `app.routers.*`。
- 迁移需同时兼容 **PostgreSQL（生产）** 与 **SQLite（部分测试）** —— 参考既有迁移的写法。

## 目标（Goal）
让「每租户存储用量」成为一个可信、唯一、可开发票的数字，并消除 `file_size` 的 NULL 空洞。

## 范围边界

**In scope：**
1. **回填脚本** `backend/scripts/backfill_media_file_size_once.py`：
   - 找出 `file_size IS NULL` 的历史行，按其 provider 从存储层取真实字节数补齐。
   - 必须**幂等**、**可分批**（`--batch-size`）、支持 **`--dry-run`**。
   - 取不到字节数的行要**明确报告**而非静默跳过或填 0。
2. **Alembic migration**：`file_size` 收紧为 `NOT NULL`。
   - 迁移内需有前置校验：若仍存在 NULL 行则**明确失败并提示先跑回填**，不得静默填 0。
   - 必须提供 `downgrade()`。
3. **按租户存储用量日滚动汇总**：新表（建议 `tenant_storage_daily`）+ 写入任务。
   - 口径唯一：R3 dashboard 与 R5 账单**共用这一个数字**，避免两处口径打架。
4. 测试：`backend/tests/test_media_file_size_backfill.py` + 汇总表用例。

**Out of scope（显式非目标）：**
- **不实现计费 / 订阅 / 支付逻辑**（那是 R5，RND-165）。
- 不实现 dashboard / analytics 页面（R3）。
- 不改媒体上传 / 下载 / 存储 provider 的行为。
- 不对生产库执行任何写操作。
- 不改其他 R1 页面票拥有的任何文件。

**本工单拥有的文件（只许写这些）：**
- `backend/scripts/backfill_media_file_size_once.py`（新）
- `backend/alembic/versions/<新迁移>.py`（新）
- `backend/app/db/models.py` —— **仅限** `file_size` 的 nullable 改动 + 新汇总表模型
- `backend/app/services/<存储用量汇总服务>.py`（新，若需要）
- `backend/tests/test_media_file_size_backfill.py`（新）

**只读、绝不可写：** 4 张页面票的所有文件（`sidenav.py`、`admin_*_page.py`、各 `templates/*.html`、`i18n.js`）、`media_storage.py`、`qiniu_storage.py`、`routers/media*.py`。

## 验收标准（Acceptance Criteria）
- **AC-1 回填脚本幂等且可 dry-run**：Given 存在 `file_size IS NULL` 的行，When 以 `--dry-run` 运行，Then 报告将被回填的行数且**不写库**；When 实际运行两次，Then 第二次为 no-op（幂等）。
- **AC-2 取不到字节数时明确报告**：Given 某行对应的存储对象缺失，When 回填运行，Then 该行被列入失败报告且**不被填成 0**，脚本以非零退出码或明确汇总提示结束。
- **AC-3 迁移前置校验**：Given 库中仍有 `file_size IS NULL`，When 执行 `alembic upgrade head`，Then 迁移**明确失败并提示先跑回填**（不得静默填 0 或悄悄放行）。
- **AC-4 NOT NULL 生效且新写入受约束**：迁移后 `file_size` 为 `NOT NULL`；尝试插入 `file_size=None` 的 `MediaFile` 会被数据库拒绝。
- **AC-5 汇总口径唯一**：存在按 `tenant_id` + 日期的存储用量汇总，且有测试证明其数值等于 `SUM(media_files.file_size) GROUP BY tenant_id` 的同期结果。
- **AC-6 schema 无 drift**：`python -m alembic check` 通过（ORM 模型与迁移一致）。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_file_size_backfill.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
cd backend && .venv/bin/python -m alembic check          # schema drift 硬闸（AC-6）
# 迁移正反向验证（仅本地/测试库）
cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic downgrade -1
```

## 依赖（Dependencies）
无前置阻塞，可立即开始，**不阻塞任何其他票**。

## 完成定义
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿、`alembic check` 通过
- [ ] 迁移 `upgrade` / `downgrade` 均在本地验证过
- [ ] 回填脚本 dry-run 与幂等性均有测试
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**含「本地验证用的是什么库、共回填多少行、多少行取不到字节数」**
- [ ] **未 commit、未 push、未对生产库执行任何操作**

## 风险与回滚
- **风险 1（最高）**：回填把取不到大小的行填成 0 → 发票金额系统性偏低且难以事后发现。**由 AC-2 显式防守。**
- **风险 2**：NOT NULL 迁移在生产遇到残留 NULL → 部署失败。**由 AC-3 的前置校验防守**（宁可迁移明确失败，也不要静默填 0）。
- **风险 3**：大表回填锁表 → 必须分批，避免长事务。
- 回滚：migration 提供 `downgrade()`；回填是数据写入，**不可自动回滚** —— 因此 dry-run 与 AC-2 的失败报告是关键保护。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2 强制）**：迁移脚本与回填脚本必须经 **Haisu 审阅** 后才可对任何非本地库执行。Agent 只在本地/测试库验证。
- **Escalation**：若发现存储层无法可靠取到某 provider 的字节数 → `BLOCKED_NEEDS_HUMAN`，说明覆盖率，**不要猜一个值填进去**。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`models.py` 的 `MediaFile`（尤其 `file_size`、`tenant_id`、provider 字段）、`media_storage.py`、既有 `backfill_*_once.py` 脚本范式、`alembic/versions/` 最近几个迁移的写法。
2. 先写回填脚本（含 `--dry-run` / `--batch-size` / 失败报告），在本地库验证幂等。
3. 再写 migration（含前置 NULL 校验 + `downgrade`）。
4. 建汇总表与写入逻辑，确保口径与 `SUM(file_size)` 一致。
5. 写测试覆盖 AC-1~AC-5。
6. 跑 `make verify` + `alembic check` + 迁移正反向，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史。
- **不对生产库或共享库执行迁移、回填、DROP、TRUNCATE**（`DEV_AGENT_RULES.md` 明令禁止）。
- 不改 CI/CD、`.gitignore`、部署配置。
- 凭证只从环境变量读；`DATABASE_URL` 不得硬编码或写入测试。
- 不扩大 Scope：计费 / 订阅 / dashboard 一律 Out。
- 证据优先，以 exit 0 / 测试通过为证。
