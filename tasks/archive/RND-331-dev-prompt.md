[Goal check] This work advances 开发（Development） by 回填 media_files 中 download_status='downloaded' 却 file_size 为 NULL 的历史行、加条件性 CHECK 约束（而非列级 NOT NULL）、并建立只算已下载行的按租户存储用量日滚动汇总，使「按存储量计费」有可信计量口径且不影响 pending/failed 下载状态机。

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

## ⚠️ 2026-07-29 设计更正：不是盲目 NOT NULL（重要，改变 AC-3/4/5 与回填范围）

开发 agent 首轮正确停在 `BLOCKED_NEEDS_HUMAN`：`MediaFile.download_status`（`models.py:608`）有三态 `pending` / `downloaded` / `failed`；`file_size` **只在下载成功那一刻才知道**（`media_download.py:667-668`：`outcome, detail, file_size = "downloaded", final_ref, len(data)`，字节数取自内存中的真实 payload，这部分本来就是对的，不撒谎）。`pending` 行在尝试下载前就已建行（`media_download.py:538-542`），`file_size` 天然未知；重试会显式 `row.file_size = None`（`media_download.py:557`）。**这不是历史脏数据，是下载状态机的正常组成部分。**

原 AC-3「`file_size` 收紧为 `NOT NULL`」是错的设计——会把「还没下载完」和「下载完了但历史上没记账」这两件不同的事混为一谈，且会直接打断现有下载/重试流程。**正确约束是条件性的，不是列级的：**

> `file_size IS NOT NULL` **当且仅当** `download_status = 'downloaded'`。`pending` / `failed` 行允许 `file_size` 为空——这是正确状态，不是缺口。

落地方式：**DB 层 CHECK 约束**（不是列级 `NOT NULL`）：

```sql
CHECK (download_status != 'downloaded' OR file_size IS NOT NULL)
```

**这个设计的好处**：
1. 完全不用碰 `media_download.py`——`pending`/`failed` 行的写入/重试行为原样不变，Out of scope 条款保住了。
2. `AC-2`「不得填 0」自动满足——`pending`/`failed` 行根本不需要填任何值。
3. 回填范围收窄为**只**`download_status='downloaded' AND file_size IS NULL`的行——这才是真正的历史记账缺口，比原来「所有 NULL 行」更准确，工作量还更小。
4. 计费汇总查询需要显式加 `WHERE download_status='downloaded'`（不要只依赖 SQL `SUM()` 自动跳过 NULL 的隐式行为——显式过滤更防御性，以后状态机加新值也不会悄悄改变计费口径）。

**关于既有测试**：按新设计，构造 `pending`/`failed` 状态且不带 `file_size` 的既有测试 fixture **完全合法，不需要改**。只有**极少数**可能构造了 `download_status="downloaded"` 却没给 `file_size` 的 fixture 才需要修，因为它们在新约束下确实代表非法状态。**授权**：若找到这类 fixture，可以在对应测试文件里**只补一个 `file_size=<合理测试值>` 参数**，不做其他改动；开工前先 grep 一遍列出清单，QA Summary 里必须逐个列出改了哪些文件、哪一行、为什么（属于本条新授权，不算越权）。

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
   - 找出 **`download_status='downloaded' AND file_size IS NULL`** 的行（不是全部 NULL 行——`pending`/`failed` 的 NULL 是合法状态，不回填），按其 provider 从存储层取真实字节数补齐。
   - 必须**幂等**、**可分批**（`--batch-size`）、支持 **`--dry-run`**。
   - 取不到字节数的行要**明确报告**而非静默跳过或填 0。
2. **Alembic migration**：加 **CHECK 约束** `download_status != 'downloaded' OR file_size IS NOT NULL`（**不是**列级 `NOT NULL`，见上方设计更正）。
   - 迁移内需有前置校验：若仍存在 `download_status='downloaded' AND file_size IS NULL` 的行则**明确失败并提示先跑回填**，不得静默填 0。
   - SQLite 对 `ALTER TABLE ADD CONSTRAINT` 支持有限——参考 `alembic/versions/` 里既有迁移是否已用 `batch_alter_table` 处理过类似情况，照现有范式写，不要发明新模式。
   - 必须提供 `downgrade()`。
3. **按租户存储用量日滚动汇总**：新表（建议 `tenant_storage_daily`）+ 写入任务。
   - 口径唯一：R3 dashboard 与 R5 账单**共用这一个数字**；汇总查询**显式** `WHERE download_status='downloaded'`（不要只依赖 `SUM()` 隐式跳过 NULL）。
4. **（新授权，窄范围）** 若存在构造 `download_status="downloaded"` 却未设 `file_size` 的既有测试 fixture，可在该测试文件**只补 `file_size=` 参数**，不做其他改动，QA Summary 里逐个列出。
5. 测试：`backend/tests/test_media_file_size_backfill.py` + 汇总表用例 + CHECK 约束的正反用例。

**Out of scope（显式非目标）：**
- **不实现计费 / 订阅 / 支付逻辑**（那是 R5，RND-165）。
- 不实现 dashboard / analytics 页面（R3）。
- **不改 `media_download.py` 的下载 / 重试逻辑本身**（`pending`/`failed` 行为原样不变，这是本次设计更正要保住的东西）。
- 不对生产库执行任何写操作。
- 不改其他 R1 页面票拥有的任何文件。

**本工单拥有的文件（只许写这些）：**
- `backend/scripts/backfill_media_file_size_once.py`（新）
- `backend/alembic/versions/<新迁移>.py`（新）
- `backend/app/db/models.py` —— **仅限** `file_size` 相关的 CHECK 约束声明 + 新汇总表模型
- `backend/app/services/<存储用量汇总服务>.py`（新，若需要）
- `backend/tests/test_media_file_size_backfill.py`（新）
- **既有测试文件中构造 `download_status="downloaded"` 但缺 `file_size` 的具体 fixture（2026-07-29 追加授权，见上，仅限补一个参数）**

**只读、绝不可写：** 4 张页面票的所有文件（`sidenav.py`、`admin_*_page.py`、各 `templates/*.html`、`i18n.js`）、`media_storage.py`、`qiniu_storage.py`、`routers/media*.py`、**`media_download.py`（除非上述 fixture 授权覆盖到某测试文件，`media_download.py` 本身不属于任何授权，绝不可写）**。

## 验收标准（Acceptance Criteria）
- **AC-1 回填脚本幂等且可 dry-run**：Given 存在 `download_status='downloaded' AND file_size IS NULL` 的行，When 以 `--dry-run` 运行，Then 报告将被回填的行数且**不写库**；When 实际运行两次，Then 第二次为 no-op（幂等）。
- **AC-2 取不到字节数时明确报告**：Given 某 `downloaded` 行对应的存储对象缺失，When 回填运行，Then 该行被列入失败报告且**不被填成 0**，脚本以非零退出码或明确汇总提示结束。
- **AC-3 迁移前置校验**：Given 库中仍有 `download_status='downloaded' AND file_size IS NULL` 的行，When 执行 `alembic upgrade head`，Then 迁移**明确失败并提示先跑回填**（不得静默填 0 或悄悄放行）。
- **AC-4 CHECK 约束生效且不影响 pending/failed（关键，替代原「NOT NULL」设计）**：
  - Given `download_status="downloaded"` 且 `file_size=None`，When 插入 `MediaFile`，Then 被数据库拒绝。
  - Given `download_status="pending"` 或 `"failed"` 且 `file_size=None`，When 插入 `MediaFile`，Then **成功**（这是合法状态，不受约束影响）——**必须有测试覆盖这条正向用例**，证明未破坏现有下载/重试流程。
- **AC-5 汇总口径唯一且只算已下载**：存在按 `tenant_id` + 日期的存储用量汇总，**显式过滤 `download_status='downloaded'`**，且有测试证明其数值等于 `SELECT SUM(file_size) FROM media_files WHERE tenant_id=... AND download_status='downloaded'` 的同期结果。
- **AC-6 schema 无 drift**：`python -m alembic check` 通过（ORM 模型与迁移一致）。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；**`media_download.py` 相关既有测试全绿且该文件本身零改动**（`git diff --stat -- backend/app/media_download.py` 必须无输出）。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_file_size_backfill.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m pytest backend/tests/ -k "media_download" -q   # AC-7：pending/failed 流程无回归
cd backend && .venv/bin/python -m alembic check          # schema drift 硬闸（AC-6）
# 迁移正反向验证（仅本地/测试库）
cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic downgrade -1
git diff --stat -- backend/app/media_download.py         # 必须无输出（AC-7）
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
- **风险 2**：CHECK 约束迁移在生产遇到残留 `downloaded+NULL` → 部署失败。**由 AC-3 的前置校验防守**（宁可迁移明确失败，也不要静默填 0）。
- **风险 3**：大表回填锁表 → 必须分批，避免长事务。
- **风险 4（2026-07-29 新增）**：误把约束写成列级 `NOT NULL` 而非条件性 CHECK → 直接打断 `pending`/`failed` 下载流程，属于严重回归。**由 AC-4 的正向用例（pending/failed + NULL 必须插入成功）显式防守。**
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
