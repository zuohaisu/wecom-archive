# RND-293 QA / 验收 agent 提示词 —— A7-1 AuditLog 表 + Alembic migration（immutable）

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-293-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认 `AuditLog` **immutable / 只读追加** 模型与配套 Alembic migration（`0019` / `down_revision="0018"`）已落地，**`alembic check` 绿**，**`make verify` 全绿**，**不可变语义成立**（无 update/delete 代码路径、无 `updated_at`），且**范围严格守门**（无端点、无写入钩子、未提交）。零回归。

## 二、逐条验收清单（PASS/FAIL，附证据）

### Schema 落地（模型 `backend/app/db/models.py`）
- [ ] S1 `AuditLog` 含全部列：`id` / `tenant_id` / `admin_user_id` / `action` / `object_type` / `object_id` / `detail` / `created_at` —— 证据：`python -c "from app.db import models; print([c.name for c in models.AuditLog.__table__.columns])"`
- [ ] S2 `tenant_id` 为 `String(36)`、`nullable=False`、`ForeignKey("tenants.id")` —— 证据：读模型 / 断言 `AuditLog.__table__.c.tenant_id`
- [ ] S3 `admin_user_id` 为 `String(36)`、`nullable=True`、`ForeignKey("admin_users.id")` —— 证据：断言 FK 目标
- [ ] S4 `action` / `object_type` 为 `Text` 且 `nullable=False`，**类型为 `Text` 非 `Enum`**（不得锁枚举） —— 证据：断言 `isinstance(col.type, Text)` 且 `not isinstance(col.type, Enum)`
- [ ] S5 `object_id` 为 `Text`、`nullable=True`
- [ ] S6 `detail` 类型为 `JSONB`、`nullable=True` —— 证据：断言 `isinstance(col.type, JSONB)`
- [ ] S7 `created_at` 为 `DateTime(timezone=True)`、`nullable=False`、`server_default is not None`（= `func.now()`） —— 证据：断言 `AuditLog.__table__.c.created_at.server_default`
- [ ] S8 三个索引存在：`ix_audit_logs_tenant_id` / `ix_audit_logs_admin_user_id` / `ix_audit_logs_created_at` —— 证据：`{i.name for i in AuditLog.__table__.indexes}`

### 不可变（immutable）信号 —— 本票核心
- [ ] I1 `updated_at` **不在** `AuditLog.__table__.columns` —— 证据：断言 `"updated_at" not in AuditLog.__table__.columns`
- [ ] I2 任何列都**无** `onupdate`（尤其 `created_at` 不得有 `onupdate`）—— 证据：遍历列断言 `col.onupdate is None`
- [ ] I3 工作树**无**任何对 `audit_logs` 的 UPDATE/DELETE 代码路径：grep 工作树（含 `routers/`、`services/`、`app/`、迁移外）确认无 `audit_logs` 相关 endpoint、无 `session.execute(text(... update/delete ... audit_logs ...))`、模型无 `update`/`delete` 方法 —— 证据：grep 结果（应为空）
- [ ] I4 `detail` 列语义合规：模型 docstring / 代码无承载消息正文或 `decrypted_payload` 的迹象（SF-1 数据最小化）—— 证据：读模型类注释与列定义

### 迁移（文件 `backend/alembic/versions/0019_audit_log.py`）
- [ ] M0 迁移文件名为 `0019_audit_log.py`（**不得** named `0018_audit_log.py`，`0018` 已被 RND-278 的 `0018_password_reset_tokens.py` 占用 → 撞 revision 会让 `alembic upgrade head`/`alembic check` 失败）
- [ ] M1 `revision=="0019"` 且 `down_revision=="0018"`（非 0018 以外的值）
- [ ] M2 `upgrade()` 用 `op.create_table("audit_logs", ...)` 建表，列/类型/可空/nullable 与模型**完全一致**（含 `created_at` 的 `server_default=sa.func.now()`、`detail` 用 `postgresql.JSONB()`）
- [ ] M3 `upgrade()` 建三个索引名与模型 `__table_args__` 一致；`downgrade()` 顺序正确（`drop_index` ×3 → `drop_table`），无 `Enum.drop`（因本票无原生枚举）
- [ ] M4 表含两个 FK：`fk_audit_logs_tenant_id` → `tenants.id`、`fk_audit_logs_admin_user_id` → `admin_users.id`
- [ ] M5 迁移**未改动** `admin_users` / `tenants` 等既有表

### 关键验收门槛
- [ ] K1 `alembic upgrade head` 成功，随后 `alembic check` 输出成功（无 pending change）—— 证据：命令输出
- [ ] K2 新测试 `test_rnd293_audit_log.py` 在 `make test` 中执行通过（无 DB 时按 `_DB_AVAILABLE` 自动 skip，不 FAIL）—— 证据：pytest 输出
- [ ] K3 若环境有 `DATABASE_URL`：DB 支撑测试通过（建 tenant+user → 插 AuditLog → 读回断言；并确认 `information_schema.columns` 中 `audit_logs` 无 `updated_at`）—— 证据：测试通过

### 范围守门（本票只动：模型 + 迁移 + 测试）
- [ ] G1 `git diff --name-only` 仅含：`backend/app/db/models.py` + `backend/alembic/versions/0019_audit_log.py` + `backend/tests/test_rnd293_audit_log.py`（或等价测试文件）
- [ ] G2 `git diff` 不含任何 `routers/*.py` 改动（无列表 API —— 属 A7-3）
- [ ] G3 `git diff` 不含 `services/` 或 `routers/` 中任何 `audit`/`AuditLog` 写入函数/钩子（写入钩子属 A7-2）
- [ ] G4 未改动 `auth.py` / `routers/auth.py` / `models.py` 中 `AdminUser` / `Tenant` 等既有定义（仅新增 `AuditLog` 类）
- [ ] G5 未引入承载消息正文或 `decrypted_payload` 的字段/表

### 全局契约
- [ ] C1 `make verify` 全绿（含 `test_architecture_boundary.py`：仅加模型类/迁移/测试，不引入反向依赖）
- [ ] C2 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）

## 三、回归套件

1. `cd backend && make verify`（全量；应包含新增 `test_rnd293_audit_log.py`）。
2. 若环境有 `DATABASE_URL`：额外确认 `alembic upgrade head` + `alembic check` 绿，以及 DB 支撑测试（K3）通过。
3. 若 `DATABASE_URL` 缺失：DB 支撑类测试按 `_DB_AVAILABLE` 门控自动 skip；此时 K1/K3 标记为「需有 DB 环境复测」，并在报告中注明，不得判 FAIL。

## 四、智能路由判定（每轮必给）

- 模型缺列 / 类型错（如 `action` 误用 `Enum`、`detail` 误用 `Text` 而非 `JSONB`）/ 漏索引 → 反馈开发 agent 修复，附 `models.py` 具体位置 + 期望；不自行改实现。
- 迁移 `revision`/`down_revision` 错（如写成 0018 或 0016/0017）或列定义与模型不一致（尤其 `created_at` 的 `server_default`、FK 命名）→ 反馈修复，附 `0019_audit_log.py` 行号。
- `alembic check` 不绿 → 优先排查模型与迁移不一致（列类型、`created_at` 的 `server_default` 渲染、索引名、FK 名）；反馈具体 diff。
- 发现任何 `audit_logs` 的 UPDATE/DELETE 代码路径或 `updated_at` 列（I1/I2/I3 命中）→ 判 FAIL 并附证据，要求移除（本票不可变语义硬约束）。
- 发现越界实现（新增 router 端点 / service 写钩子 / 改动既有表）→ 判 FAIL，引用 G2/G3/G4，要求裁剪到本票范围。

## 五、交付报告格式（验收结束必给）

```
RND-293 验收报告
- 结论：PASS / FAIL（附阻断项）
- Schema：S1-S8 全部 PASS/FAIL 清单
- 不可变：I1-I4 全部 PASS/FAIL 清单（必须全 PASS）
- 迁移：M1-M5 全部 PASS/FAIL 清单
- 门槛：K1/K2/K3 状态（K1/K3 注明是否有 DB 环境）
- 范围守门：G1-G5 / C1-C2 状态
- 证据：关键命令输出（alembic upgrade + check、make verify、pytest 该测试）
- 遗留/需复测：无 DB 环境下的 K1/K3 备注
```
