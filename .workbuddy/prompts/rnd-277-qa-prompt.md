# RND-277 QA / 验收 agent 提示词 —— F0-1 AdminUser 模型扩展 + Alembic migration

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-277-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认 `AdminUser` 模型已按 Epic RND-263 / F0-1 扩展全部字段并配套 Alembic migration（`0016`/`down_revision=0015`），**`alembic check` 绿**，**不破坏现有登录写入路径**，**未引入任何鉴权逻辑**（属 F0-2），且**唯一约束守恒**（既有 `uq_admin_users_tenant_wecom` 不被重复/误改）。零回归。

## 二、逐条验收清单（PASS/FAIL，附证据）

### Schema 落地（模型 `backend/app/db/models.py`）
- [ ] S1 `AdminUser` 含全部新列：`password_hash` / `role` / `status` / `email` / `phone` / `department` / `last_active_at` / `invite_token` / `invited_by` / `invite_status` —— 证据：`python -c "from app.db import models; print([c.name for c in models.AdminUser.__table__.columns])"`
- [ ] S2 `role` 为 `Enum` 且 `name=="admin_user_role"`、`enums==('owner','admin','compliance','legal','readonlyaudit')` —— 证据：读模型 / 断言
- [ ] S3 `status` 为 `Enum` 且 `name=="admin_user_status"`、`enums==('active','disabled')`
- [ ] S4 `role` / `status` 在模型侧 `server_default is not None`（保证 `alembic check` 绿 + 登录兼容；**不得为 None**）—— 证据：断言 `AdminUser.__table__.c.role.server_default`
- [ ] S5 `invited_by` 为 `ForeignKey("admin_users.id")`、可空（自引用）
- [ ] S6 `invite_token` 在 `__table_args__` 有非唯一索引 `ix_admin_users_invite_token`
- [ ] S7 `password_hash` / `email` / `phone` / `department` / `last_active_at` / `invite_token` / `invited_by` / `invite_status` 均为可空 Text/String/DateTime

### 唯一约束守恒
- [ ] U1 `uq_admin_users_tenant_wecom`（tenant_id, wecom_user_id）仍存在且唯一 —— 证据：`AdminUser.__table__.constraints` 含该名
- [ ] U2 未新建任何同名/同语义唯一约束（无重复 `uq_admin_users_tenant_wecom`，无多余全局唯一）

### 迁移（文件 `backend/alembic/versions/0016_*.py`）
- [ ] M1 `revision=="0016"` 且 `down_revision=="0015"`
- [ ] M2 `upgrade()` 显式创建两个原生枚举：`admin_user_role` / `admin_user_status`（范式见 `0015_sync_state_status.py`）
- [ ] M3 `add_column` 对 `role`/`status` 带 `server_default=sa.text("'admin'")` / `sa.text("'active'")`；**无** `op.alter_column(..., server_default=None)`（关键，区别于 0015）
- [ ] M4 `upgrade()` 创建 `ix_admin_users_invite_token` 索引；`downgrade()` 顺序正确（drop_index → drop_column 反序 → drop 两个枚举类型）
- [ ] M5 迁移**不改动** `uq_admin_users_tenant_wecom`

### 关键验收门槛
- [ ] K1 `alembic upgrade head` 成功，随后 `alembic check` 输出成功（无 pending change）—— 证据：命令输出
- [ ] K2 **登录写入路径回归**：仅用 `(id, tenant_id, wecom_user_id, name, last_login_at)` 插入 `AdminUser` 并提交成功（证明 `routers/auth.py:288` 与 `:525` 两条路径不被 NOT NULL 破坏）—— 证据：DB 支撑测试通过

### 范围守门（本票只动数据模型 + 迁移 + 测试）
- [ ] G1 `git diff --name-only` 仅含：`backend/app/db/models.py` + `backend/alembic/versions/0016_*.py` + `backend/tests/test_rnd277_admin_user_extension.py`（或等价的测试文件）
- [ ] G2 `git diff` 不含 `backend/app/auth.py`、`backend/app/routers/auth.py` 的任何改动（无鉴权逻辑/路由/密码哈希/RBAC）
- [ ] G3 无新增 router 端点、无 `get_current_user` 改动、无 RBAC 判定

### 全局契约
- [ ] C1 `make verify` 全绿（含 `test_architecture_boundary.py`：仅加模型列/迁移/测试，不引入反向依赖）
- [ ] C2 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）

## 三、回归套件

1. `cd backend && make verify`（全量；应包含新增 `test_rnd277_admin_user_extension.py`）。
2. 若环境有 `DATABASE_URL`：额外确认 `alembic upgrade head` + `alembic check` 绿，以及登录写入路径回归测试（K2）通过。
3. 若 `DATABASE_URL` 缺失：DB 支撑类测试按 `_DB_AVAILABLE` 门控自动 skip；此时 K1/K2 标记为「需有 DB 环境复测」，并在报告中注明，不得判 FAIL。

## 四、智能路由判定（每轮必给）

- 模型缺列 / 枚举值错 / server_default 缺失 → 反馈开发 agent 修复，附 `models.py` 具体位置 + 期望；不自行改实现。
- 迁移 `revision`/`down_revision` 错（如写成 0014）或漏建枚举 / 误删 server_default → 反馈修复，附 `0016_*.py` 行号。
- `alembic check` 不绿 → 优先排查模型与迁移不一致（枚举 `name`、server_default、索引名）；反馈具体 diff。
- 超出本票范围（改了 `auth.py`、加了登录路由）→ 判 FAIL 并明确标注范围溢出。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式

```
RND-277 验收结论：PASS / FAIL
Schema 落地：S1–S7 各项 PASS/FAIL + 证据
唯一约束守恒：U1/U2 PASS/FAIL
迁移：M1–M5 PASS/FAIL（revision=0016/down=0015，枚举显式建，server_default 保留）
门槛：alembic check __（绿/红）；登录写入回归 K2 __（过/skip）
范围守门：diff 文件清单 __；auth.py/routers/auth.py 零改动 __；无新路由 __
回归：make verify __（绿/红）
契约：未 commit __
遗留：__
```
