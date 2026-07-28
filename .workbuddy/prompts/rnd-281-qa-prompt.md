# RND-281 QA / 验收 agent 提示词 —— A1-1 UsageService 聚合模块

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-281-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认 `backend/app/services/usageservice.py` 提供 5 个**真实聚合**函数，语义正确、支持 `tenant_id=None` 跨租户复用（供 B1-3），`make verify` 全绿，**架构边界零违规**，**范围严格守门**（无端点/无迁移/无 F0 鉴权/未提交）。零回归。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 模块与函数（文件 `backend/app/services/usageservice.py`）
- [ ] M1 文件存在且可被 `from app.services import usageservice` 导入。
- [ ] M2 五个函数均存在且可调用：`count_messages` / `sum_storage` / `count_monitored_employees` / `get_archived_days` / `sync_health` —— 证据：python 导入 + `hasattr` 断言。
- [ ] M3 五个函数签名均为 `(db: Session, tenant_id: Optional[str] = None)`（支持跨租户全局聚合）—— 证据：读源码 / `inspect.signature`。

### 语义正确性（核心）
- [ ] S1 `count_messages(db, tenant_id)` = 该租户 `archive_messages` 行数（含撤回）；`count_messages(db, None)` = 全量（含 `tenant_id IS NULL` 的历史行）—— 证据：断言具体数值。
- [ ] S2 `sum_storage` = `coalesce(sum(media_files.file_size),0)`，仅 media 字节；`file_size` 为 NULL 的行被 coalesce 为 0；`tenant_id=None` 时全量求和 —— 证据：断言数值。**不含** DB 体积估算（属 A1-2，本票不做）。
- [ ] S3 `count_monitored_employees` = `count(distinct contacts.wecom_userid)`（去重），非 `count(*)`；`tenant_id=None` 时全量去重 —— 证据：插入重复 `wecom_userid` 验证 distinct。
- [ ] S4 `get_archived_days(db, tenant_id)` = `max(0, (first_msg_date - tenant.created_at.date()).days)`，无消息时为 `(now - tenant.created_at).days` —— 证据：用确定 `created_at` + 确定 `msgtime`(epoch-ms) 断言天数。
- [ ] S5 `get_archived_days(db, None)` = 全量消息的 **distinct 日历天数**（用 `to_timestamp(msgtime/1000)` 转日期去重）—— 证据：插入跨多天的消息后断言 distinct 天数。
- [ ] S6 `msgtime` 换算正确：所有涉及 `msgtime` 的换算都用 `÷1000`（epoch-ms→秒），无裸用毫秒当秒的错误 —— 证据：读 `get_archived_days` 实现。
- [ ] S7 `sync_health(db, tenant_id)` 聚合：多 `SyncState` 行 → `{status, error_message, last_seq, updated_at}`；优先级 `error` > `syncing` > `idle`；无行 → `status="unknown"`、`error_message/last_seq/updated_at=None`；`error_message` 取自 error 行、`last_seq`=max、`updated_at`=max 的 isoformat —— 证据：插 error+syncing 两行断言 `status=="error"` 且含 error_message。
- [ ] S8 `sync_health(db, None)` 对全量 `SyncState` 聚合（含跨租户）—— 证据：断言全局状态。

### 架构边界（硬约束，详见 `test_architecture_boundary.py`）
- [ ] A1 `usageservice.py` 位于 `app/services/` 包内，自动归 service 层；**确认 `_FLAT_SERVICE_MODULES` 未被改动**（该注册表只用于 `app/` 扁平模块，本文件不需要也不得加入）—— 证据：`git diff` 不含 `test_architecture_boundary.py`。
- [ ] A2 新文件**未** `import app.routers.*` 或 `import app.main`（仅 `import app.db.models` + `sqlalchemy` + 标准库）—— 证据：grep 源码。
- [ ] A3 `test_architecture_boundary.py` 的 `TestRealRepoHasNoReverseDependencies` 通过（新文件零反向依赖）—— 证据：`make verify` 输出。

### 范围守门（本票只建：一个 service 模块 + 一个测试文件）
- [ ] G1 `git diff --name-only` 仅含：`backend/app/services/usageservice.py` + `backend/tests/test_rnd281_usageservice.py`。
- [ ] G2 `git diff` 不含任何 `routers/*.py`（无 dashboard/usage 端点——属 A1-2）。
- [ ] G3 无 Alembic 迁移文件、无 `models.py` 改动（无 schema 变更）—— 证据：`git diff` 不含 `alembic/versions/` 与 `db/models.py`。
- [ ] G4 未引入 FastAPI `Depends` / `get_current_user` / `app.auth` 鉴权代码（属 F0/A1-2）—— 证据：grep 源码无 `Depends`/`get_current_user`/`app.auth`。
- [ ] G5 未写 `AuditLog` 或任何审计逻辑（属 A7）—— 证据：grep 无 `AuditLog`/`audit_logs`。
- [ ] G6 `test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES` 集合未被本票修改。

### 全局契约
- [ ] C1 `make verify` 全绿（lint-diff → typecheck → build → test，含 `test_architecture_boundary.py`）。
- [ ] C2 无 DB 环境时：测试文件中「无 DB 冒烟测试」必跑且通过；DB 支撑测试按 `_DB_AVAILABLE` 自动 skip，不 FAIL。
- [ ] C3 若环境有 `DATABASE_URL`：DB 支撑测试（S1-S8）全部通过。
- [ ] C4 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）。

## 三、回归套件

1. `cd backend && make verify`（全量；含新增 `test_rnd281_usageservice.py` 的无 DB 冒烟部分）。
2. 若环境有 `DATABASE_URL`：额外确认 DB 支撑测试（S1-S8）通过。
3. 若 `DATABASE_URL` 缺失：DB 支撑类测试按 `_DB_AVAILABLE` 门控自动 skip；此时 S1-S8 标注「需有 DB 环境复测」，并在报告中注明，不得判 FAIL。

## 四、智能路由判定（每轮必给）

- 函数缺失 / 签名缺 `tenant_id=None` 参数（无法跨租户复用）→ 反馈开发 agent 修复，附 `usageservice.py` 具体位置 + 期望签名。
- 语义错（如 `count_monitored_employees` 用了 `count(*)` 未去重、`sum_storage` 未 coalesce NULL、`msgtime` 未 ÷1000、`sync_health` 优先级错）→ 反馈修复，附断言期望与具体行号。
- `make verify` 不绿 → 优先排查 `test_architecture_boundary.py` 是否因新文件引入反向依赖（如误 import `app.routers.*`/`app.main`）；反馈具体违规行。
- 发现越界实现（新增 router 端点 / migration / F0 鉴权 / 写 AuditLog / 改 `_FLAT_SERVICE_MODULES`）→ 判 FAIL，引用 G2/G3/G4/G5/G6，要求裁剪到本票范围。
- `get_archived_days` 的「tenant 创建→首条消息跨度」与「distinct 日历天数」语义二选一：本票默认 planner 字面语义（per-tenant 跨度 / global distinct）。若开发 agent 实现与 S4/S5 不符，按 S4/S5 期望反馈修正。

## 五、交付报告格式（验收结束必给）

```
RND-281 验收报告
- 结论：PASS / FAIL（附阻断项）
- 模块：M1-M3 全部 PASS/FAIL 清单
- 语义：S1-S8 全部 PASS/FAIL 清单
- 架构边界：A1-A3 全部 PASS/FAIL 清单
- 范围守门：G1-G6 / C1-C4 状态
- 证据：关键命令输出（make verify、python 导入、pytest 该测试、git diff --name-only）
- 遗留/需复测：无 DB 环境下的 S1-S8 备注
```
