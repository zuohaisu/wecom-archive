# RND-277 开发 agent 执行提示词 —— F0-1 AdminUser 模型扩展 + Alembic migration

> 面向开发 agent（单人端到端实现 RND-277）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

扩展 `AdminUser` 模型，新增账号体系所需的字段（`password_hash` / `role` / `status` / `email` / `phone` / `department` / `last_active_at` / 邀请相关字段），并配套 Alembic migration，**不实现任何鉴权逻辑**（属 F0-2），最终 `alembic check` 必须全绿、且不能破坏现有登录写入路径。

## 二、决策背景（已全部拍板，不要再问）

来源：Epic `RND-263`（F0 账号体系重构）子任务 F0-1 + 现有账号体系研究文档 `docs/research/wecom_employee_login_tenant_saas_foundation.md`。

- **枚举值已锁定**：
  - `role`：`owner` / `admin` / `compliance` / `legal` / `readonlyaudit`（完整 RBAC 在 F0-5 落地，本票只建枚举）。
  - `status`：`active` / `disabled`（`status=disabled` 无法登录的语义由 F0-2 强制执行，本票只存储）。
- **`last_active_at` 区别于 `last_login_at`**：前者由用户活动更新（F0-4 维护），后者仅在登录时更新。两个字段都保留，互不替换。
- **不实现鉴权逻辑（见 F0-2）**：本票只动数据模型 + 迁移，**禁止**新增/修改任何登录路由、密码哈希 helper、`get_current_user`、RBAC 判定；`auth.py` / `routers/auth.py` 的现有写入路径保持原样可运行。
- **不建超管独立账号**（B1 PlatformAdmin 另行处理）；手机号绑定本期 defer（A8），只建可空 `phone` 列。
- **工程纪律**：Agent 不 git commit/push；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。代码标识符加反引号。

## 三、项目现状（基线，2026-07-28 扫描）

### 3.1 现有 `AdminUser` 模型
- 位置：`backend/app/db/models.py`，类定义 `L132-173`。
- 现有列：`id`(PK, String36) / `tenant_id`(FK tenants, 索引) / `wecom_user_id`(String64) / `name`(Text, 可空) / `avatar_url`(Text, 可空) / `last_login_at`(DateTime tz, 可空) / `created_at` / `updated_at`。
- **已有唯一约束 `uq_admin_users_tenant_wecom`**：`UniqueConstraint("tenant_id","wecom_user_id")`，`__table_args__` 内（L136-155）。**本票不要再建同名/同语义唯一约束**——它已满足「wecomuserid/tenant_id 唯一」要求。
- 已有导入：`Enum`（L7）、`Text` / `String` / `DateTime` / `ForeignKey` 均在用，**新增列无需新增 import**。

### 3.2 迁移链
- 当前 head = `0015`（`backend/alembic/versions/0015_sync_state_status.py`，`down_revision="0014"`）。
- **新迁移必须是 `revision="0016"`、`down_revision="0015"`**（不是 0014）。
- 原生枚举范式参考：`0015_sync_state_status.py` 用 `_ENUM = sa.Enum(..., name=...)` + `_ENUM.create(op.get_bind(), checkfirst=True)` + `op.add_column(..., server_default=sa.text("'idle'"))`。

### 3.3 ⚠️ 关键交互：现有登录写入路径不写 role/status
- `backend/app/routers/auth.py:288-295` —— env 单 hash 密码登录，构造 `AdminUser(id, tenant_id, wecom_user_id, name, last_login_at)`，**不含 role/status**。
- `backend/app/routers/auth.py:525-532` —— WeCom OAuth 回调 upsert，构造 `AdminUser(id, tenant_id, wecom_user_id, name, last_login_at)`，**不含 role/status**。
- 这两条路径在 F0-2 落地前**一直在生产运行**。因此 `role`/`status` 必须 `NOT NULL` **且保留 `server_default`**（本票不照搬 0015 的「建完即删 server_default」），否则下次登录会因缺列触发 NOT NULL 违规而崩。保留 server_default 同时保证 `alembic check` 绿（见第五节）。

## 四、目标 Schema（精确落点）

### 4.1 新增原生枚举类型（在迁移中显式创建，模型用同名引用）
- `admin_user_role` = `('owner','admin','compliance','legal','readonlyaudit')`
- `admin_user_status` = `('active','disabled')`

### 4.2 `admin_users` 新增列（全部加在 `updated_at` 之后，`models.py` L173 之后）

| 列名 | 类型 | 可空 | server_default | 说明 |
|---|---|---|---|---|
| `password_hash` | `Text` | 是 | 无 | OAuth 用户为空；F0-2 设置 |
| `role` | `Enum(admin_user_role)` | **否** | `sa.text("'admin'")` | 回填现有行用 `admin`；保留 server_default |
| `status` | `Enum(admin_user_status)` | **否** | `sa.text("'active'")` | 回填现有行用 `active`；保留 server_default |
| `email` | `Text` | 是 | 无 | 不唯一约束（F0-3 可能加） |
| `phone` | `Text` | 是 | 无 | 手机号绑定 defer（A8） |
| `department` | `Text` | 是 | 无 | |
| `last_active_at` | `DateTime(timezone=True)` | 是 | 无 | 区别于 `last_login_at`，F0-4 维护 |
| `invite_token` | `Text` | 是 | 无 | 邀请令牌；加非唯一索引便于 accept 查找 |
| `invited_by` | `String(36)` | 是 | 无 | `ForeignKey("admin_users.id")`，自引用 |
| `invite_status` | `Text` | 是 | 无 | 工作流态，取值 `pending`/`accepted`/`expired`（F0-3 拥有生命周期；用 Text 避免过早锁枚举） |

### 4.3 模型改动示意（`backend/app/db/models.py`，`AdminUser` 类内）

```python
    # —— RND-277 (F0-1) 账号体系字段扩展 ——
    password_hash = Column(Text, nullable=True)
    role = Column(
        Enum("owner", "admin", "compliance", "legal", "readonlyaudit", name="admin_user_role"),
        nullable=False,
        server_default=sa.text("'admin'"),  # 保留！不可删（见 3.3）
    )
    status = Column(
        Enum("active", "disabled", name="admin_user_status"),
        nullable=False,
        server_default=sa.text("'active'"),  # 保留！不可删（见 3.3）
    )
    email = Column(Text, nullable=True)
    phone = Column(Text, nullable=True)
    department = Column(Text, nullable=True)
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    invite_token = Column(Text, nullable=True)
    invited_by = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    invite_status = Column(Text, nullable=True)
```

> 顶部 import 已有 `from sqlalchemy import Enum`（L7）；`Text/String/DateTime/ForeignKey` 已存在，无需新增。

`__table_args__`（L136-155）追加 `invite_token` 非唯一索引，便于 F0-3 按令牌查找：

```python
        Index(
            "ix_admin_users_invite_token",
            "invite_token",
        ),
```

### 4.4 不要做的事
- 不要重建 `uq_admin_users_tenant_wecom`（已存在）。
- 不要把 `email` 设为唯一（跨租户唯一无意义，F0-3 再议）。
- 不要新增任何 router / 鉴权函数 / RBAC 判定（属 F0-2 / F0-5）。
- 不要删 `role`/`status` 的 `server_default`（见 3.3）。

## 五、实现步骤（RED → GREEN）

### 步骤 1 — 改模型（RED 基线先记录，见第六/七节）
编辑 `backend/app/db/models.py`：`AdminUser` 类加 4.2/4.3 的列；`__table_args__` 加 4.3 的 `invite_token` 索引。

### 步骤 2 — autogenerate 生成 0016 迁移
在 `backend/` 目录、设好 `DATABASE_URL`（指向任一 Postgres，建议用测试库）后执行：

```bash
cd backend
alembic revision --autogenerate -m "RND-277 admin_users account fields (F0-1)"
```

### 步骤 3 — 校验并修正生成的 0016 迁移（关键）
打开生成的 `backend/alembic/versions/0016_*.py`，确认/修正：

1. `revision = "0016"`、`down_revision = "0015"`。
2. **原生枚举显式创建**：`upgrade()` 中在 `add_column` 之前有 `_ROLE_ENUM.create(op.get_bind(), checkfirst=True)` 与 `_STATUS_ENUM.create(...)`（若 autogenerate 未生成，手工补，范式见 `0015_sync_state_status.py`）。
3. `add_column` 对 `role`/`status` 必须带 `server_default=sa.text("'admin'")` / `sa.text("'active'")`；**不要**出现 `op.alter_column(..., server_default=None)`（与 0015 不同，本票必须保留 server_default）。
4. `invite_token` 索引通过 `op.create_index("ix_admin_users_invite_token", "admin_users", ["invite_token"])` 创建（autogenerate 应已生成）。
5. `downgrade()`：依次 `op.drop_index` → `op.drop_column`（所有新列，顺序与 upgrade 反序）→ `_ENUM.drop(op.get_bind(), checkfirst=True)` 两个枚举类型。
6. 不要出现对 `uq_admin_users_tenant_wecom` 的改动。

### 步骤 4 — 升级 + `alembic check` 必须绿
```bash
cd backend
alembic upgrade head
alembic check        # Alembic 1.14 支持；预期输出 "Status: Success" / 无 pending change
```
- `alembic check` 绿是本票**硬验收门槛**。若报差异，逐条排查模型与迁移是否一致（尤其枚举 `name`、server_default、索引名）。
- 若环境 Alembic 低于 1.7（本仓为 1.14，正常不会），兜底：`alembic revision --autogenerate --head` 确认生成空迁移体（仅 `pass`）即等价绿。

### 步骤 5 — 新增测试
新建 `backend/tests/test_rnd277_admin_user_extension.py`（参照 `test_tenant_foundation.py` 风格）：
- 模型导入 + 断言 4.2 全部新列存在于 `AdminUser.__table__.columns`。
- 断言 `role` 是 `Enum` 且 `name=="admin_user_role"`、`enums==('owner','admin','compliance','legal','readonlyaudit')`；`status` 同理。
- 断言 `role`/`status` 的 `server_default is not None`（模型侧，保证 check 绿 + 登录兼容）。
- 断言 `uq_admin_users_tenant_wecom` 唯一约束仍在。
- **DB 支撑测试**（用 `DATABASE_URL` 门控，参考 `test_tenant_foundation.py:316` 的 `_DB_AVAILABLE` 模式）：
  - 升级到 head 后，`information_schema.columns` 中 `admin_users` 含全部新列名。
  - `pg_type`/`pg_enum` 中 `admin_user_role` 标签集、`admin_user_status` 标签集正确。
  - **关键回归**：仅用 `(id, tenant_id, wecom_user_id, name, last_login_at)` 插入一条 `AdminUser` 并提交成功（证明现有登录 upsert 路径不被破坏）。

## 六、阶段三验证（RED/GREEN 记录）

RED 基线（改前记录，便于 QA 对比）：
```bash
cd backend
grep -n "class AdminUser" app/db/models.py          # L132
grep -c "uq_admin_users_tenant_wecom" app/db/models.py   # 现有 1 处
ls alembic/versions/ | tail -1                        # 当前最新 0015_*
```

GREEN（改后）：
```bash
cd backend
python -c "from app.db import models; c=models.AdminUser.__table__.columns; print([x.name for x in c])"  # 含全部新列
alembic upgrade head && alembic check                # 必须绿
make verify                                          # 全绿
```

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- 不实现/修改任何鉴权逻辑（无新路由、无密码哈希、无 RBAC）；`auth.py` / `routers/auth.py` 现有写入路径保持不变且仍可运行。
- 不破坏现有登录（role/status 保留 server_default；自建唯一约束只保留既有 `uq_admin_users_tenant_wecom`）。
- 新迁移必为 `0016` / `down_revision="0015"`；`alembic check` 必须绿。
- 不触碰 B 层生产路径（见项目通用纪律：`/srv/apps/wecom-archive-365`、systemd 单元名、deploy.yml、`.env.example`、`backend/scripts` 一个字符不动）。
- 架构边界测试 `backend/tests/test_architecture_boundary.py` 必须仍 PASS（只加模型列/迁移/测试，不引入反向依赖）。

## 八、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应仅含 `app/db/models.py` + 新迁移文件 + 新测试文件）、`alembic check` 绿日志、`make verify` 日志、未提交声明。
