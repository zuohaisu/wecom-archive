# RND-293 开发 agent 执行提示词 —— A7-1 AuditLog 表 + Alembic migration（immutable）

> 面向开发 agent（单人端到端实现 RND-293）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

在 `backend/app/db/models.py` 新增 **immutable / 只读追加** 的 `AuditLog` 模型，并配套 Alembic migration（`0018` / `down_revision="0017"`），**不实现任何写入钩子或读取端点**（分别属 A7-2 / A7-3）。最终 `alembic upgrade head` + `alembic check` 必须全绿，且 `make verify` 全绿。

## 二、决策背景（已全部拍板，不要再问）

来源：Epic `RND-274`（A7 审计日志）+ 子任务 `RND-293`（A7-1）+ 子任务 `RND-294`（A7-2 写入钩子，本票不做）/ `RND-295`（A7-3 列表 API，本票不做）。

- **本票只建表 + 迁移 + 测试**，写入钩子（A7-2）与列表/筛选/分页 API（A7-3）是独立 ticket，**严禁**在本票实现。
- **只读追加（immutable）的语义边界**：在**应用层**做到「没有 update / delete 代码路径」——即本票不写任何端点、不写任何 service 写函数、模型不带 `updated_at`、不暴露修改入口。这是 A7-1 对「不可篡改」的落地方式。
- **DB 级防 UPDATE/DELETE 触发器（如 `deny_audit_logs_update_delete`）= 本票显式非目标**，留待 A7-2/A7-3（写入/读取路径成形后）再决定是否加固。QA 不会因缺少触发器判 FAIL。理由：现在加触发器属于孤儿守卫（无消费者），且会复杂化迁移/回滚。
- **`detail` 不含消息内容**（Non-goal：不审计消息内容本身）。`detail` 仅存结构化上下文（如目标标识、被改字段名、来源 IP），**绝不**写消息正文或 `decrypted_payload`（与 SF-1 数据最小化一致）。
- **`action` / `object_type` 用 `Text` 而非枚举**：A7-2 才会定义规范动作词表；本票若过早锁枚举会导致 A7-2 再迁一次。沿用 RND-277 对 `invite_status` 的「Text 避免过早锁枚举」原则。
- **依赖 `F0` 已在 schema 层面满足**：本票 FK 指向 `admin_users.id`，而 F0-1（RND-277，migration `0017`）已 merged，`admin_users` 表与其 `id` 列已存在，故 FK 有效。无需等待 F0-2（鉴权）。
- **工程纪律**：Agent 不 git commit/push；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。代码标识符加反引号。

## 三、项目现状（基线，2026-07-28 扫描）

### 3.1 迁移链（务必基于真实 head，不要猜）
- 当前 head = `0017`（`backend/alembic/versions/0017_admin_users_account_fields.py`，`down_revision="0016"`，即 RND-277 F0-1）。
- **新迁移必须是 `revision="0018"`、`down_revision="0017"`**。
- 原生枚举范式参考 `0017_admin_users_account_fields.py`（本票 `action`/`object_type` 是 Text，**不需要**建原生枚举类型，比 0017 更简单）。
- `alembic check`（Alembic 1.14）可用，作为「模型 ⇄ 迁移一致」的硬门槛。

### 3.2 `AdminUser` / `Tenant` 目标（FK 来源，均已存在）
- `admin_users.id` = `String(36)` PK（`models.py` L158）。本票 `admin_user_id` FK 指向它。
- `tenants.id` = `String(36)` PK（`models.py` L38）。本票 `tenant_id` FK 指向它。
- 注意：仓库中指向 `admin_users.id` 的 FK 列统一命名为 `admin_user_id`（如 `AdminSession.admin_user_id`，`models.py` L218）。**本票沿用 `admin_user_id` 命名**（对应 spec 的 `actoradminuserid`），保持全仓一致。

### 3.3 现有模型文件与导入（无需新增 import）
- `backend/app/db/models.py`：顶部已导入 `Column / String / Text / DateTime / ForeignKey / Index / func / text`（`L1-19`）与 `from sqlalchemy.dialects.postgresql import JSONB`（`L20`），以及 `from app.db.base import Base`（`L22`）。
- `Base` = `DeclarativeBase`（`backend/app/db/base.py` L4，SQLAlchemy 2.0 风格）。
- `AuditLog` 列所需类型**全部已导入**，新模型类**无需新增任何 import**。
- `alembic/env.py` L12 `from app.db import models`（注释明确 "registers all ORM models with Base"），L19 `target_metadata = Base.metadata`。**结论：在 `models.py` 新增 `AuditLog` 类即自动注册到 `Base.metadata`，`alembic check` 能感知该表，无需改 `__init__.py` 或任何注册表。**

### 3.4 `JSONB` 用法范式（本票 `detail` 列）
- 模型侧：`from sqlalchemy.dialects.postgresql import JSONB`，列定义 `detail = Column(JSONB, nullable=True)`（参照 `models.py` L367 `raw_encrypted_payload = Column(JSONB, nullable=True)`）。
- 迁移侧：`from sqlalchemy.dialects import postgresql`，用 `postgresql.JSONB()`。

### 3.5 既有 `audit` 命名是干扰项
- 全仓 `audit` 命中均为 `reachability_audit`（可达性审计，独立功能），**与 A7 审计日志无关**。不要复用其表/模型名，本票独立建 `audit_logs`。

## 四、目标 Schema（精确落点）

### 4.1 表名与列（snake_case 落点，映射 spec 的 camelCase）

表名：`audit_logs`（仓库约定：snake_case 复数，如 `admin_users` / `admin_sessions` / `tenant_wecom_configs`）。

| spec 字段 | DB 列 | 类型 | 可空 | server_default | 说明 / 映射 |
|---|---|---|---|---|---|
| （新增） | `id` | `String(36)` | 否 | 无 | PK；应用/A7-2 用 `uuid4()` 生成（同其他表，无 server_default） |
| `tenantid` | `tenant_id` | `String(36)` | **否** | 无 | FK → `tenants.id`；租户隔离，必须索引 |
| `actoradminuserid` | `admin_user_id` | `String(36)` | 是 | 无 | FK → `admin_users.id`；操作者；系统动作可空。命名沿用 `AdminSession.admin_user_id` |
| `action` | `action` | `Text` | **否** | 无 | 动作类型；**Text 非枚举**（A7-2 定义词表） |
| `objecttype` | `object_type` | `Text` | **否** | 无 | 对象类型；**Text 非枚举** |
| `objectid` | `object_id` | `Text` | 是 | 无 | 对象 ID；无特定对象时为空（如「配置查看」） |
| `detail` | `detail` | `JSONB` | 是 | 无 | 结构化上下文；**绝不存消息正文/decrypted_payload** |
| `created_at` | `created_at` | `DateTime(timezone=True)` | **否** | `func.now()` | 入库时间；**无 `onupdate`**（不可变信号） |

**不可变信号**：模型**不得**含 `updated_at` 列；任何列都不得有 `onupdate`。

### 4.2 索引（沿用仓库「单列索引」风格，如 `AdminSession` 的 `ix_admin_sessions_expires_at`）
- `ix_audit_logs_tenant_id` → `tenant_id`（FK 索引 + 租户隔离查询）
- `ix_audit_logs_admin_user_id` → `admin_user_id`（按操作者查询）
- `ix_audit_logs_created_at` → `created_at`（时间有序分页）

> 不建复合索引（如 `(tenant_id, created_at)`）；单列已覆盖 A7-3 的筛选/分页雏形，复合索引留待 A7-3 据真实查询计划决定，避免过早优化。

## 五、实现落点（精确）

### 5.1 模型：在 `backend/app/db/models.py` 末尾（`AdminSession` 类之后，约 L232 之后）追加

```python
class AuditLog(Base):
    """Immutable, append-only audit trail (RND-293 / A7-1).

    Records admin actions for compliance evidence. There is NO update/delete
    path at the application layer — rows are written once (A7-2) and read
    (A7-3) but never mutated. `detail` holds structured context only; it
    MUST NOT contain message bodies or decrypted payloads (SF-1).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_id", "tenant_id"),
        Index("ix_audit_logs_admin_user_id", "admin_user_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=False
    )
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=True, index=False
    )
    action = Column(Text, nullable=False)
    object_type = Column(Text, nullable=False)
    object_id = Column(Text, nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

> 注意：`tenant_id` / `admin_user_id` 列上 `index=False`，索引改由 `__table_args__` 的 `Index(...)` 统一管理，避免重复建索引。

### 5.2 迁移：新建 `backend/alembic/versions/0018_audit_log.py`

```python
"""Create immutable AuditLog table (RND-293 A7-1).

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_audit_logs_tenant_id"
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["admin_users.id"], name="fk_audit_logs_admin_user_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_admin_user_id", "audit_logs", ["admin_user_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_admin_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_id", table_name="audit_logs")
    op.drop_table("audit_logs")
```

> 关键：`action` / `object_type` 是 `Text`，**不建原生枚举类型**（区别于 0017）；因此 `upgrade` 无 `Enum.create`、`downgrade` 无 `Enum.drop`，回滚干净。
> `created_at` 的 `server_default=sa.func.now()` 必须与模型侧 `func.now()` 渲染一致，否则 `alembic check` 不绿。

### 5.3 测试：新建 `backend/tests/test_rnd293_audit_log.py`

沿用 `test_rnd277_admin_user_extension.py` 的风格（模型断言 + `DATABASE_URL` 门控的 DB 支撑测试）：

- 断言 `AuditLog.__table__.columns` 含全部列：`id` / `tenant_id` / `admin_user_id` / `action` / `object_type` / `object_id` / `detail` / `created_at`。
- 断言 `updated_at` **不在**列集合中（immutable 信号）。
- 断言 `action` / `object_type` 类型为 `Text`（非 `Enum`）；`detail` 类型为 `JSONB`。
- 断言 `created_at` 的 `server_default is not None`（保证 `alembic check` 绿 + DB 回填）。
- 断言 FK：`admin_user_id.type` 指向 `admin_users.id`、`tenant_id.type` 指向 `tenants.id`（用 `ForeignKey` / `columns[].foreign_keys` 验证）。
- 断言三个索引名存在：`ix_audit_logs_tenant_id` / `ix_audit_logs_admin_user_id` / `ix_audit_logs_created_at`。
- DB 支撑测试（`_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))` 门控，无 DB 时 `skip`）：建 `Tenant` + `AdminUser`，`session.add(AuditLog(id=..., tenant_id=..., admin_user_id=..., action="config.view", object_type="tenant_config", object_id=None, detail={"ip": "127.0.0.1"}))` → commit → 读回断言字段；并查 `information_schema.columns` 确认 `audit_logs` 无 `updated_at` 列。finally 中清理（DELETE 测试行 → DELETE 测试 tenant/user）。

## 六、验收门槛（实现后自查）

- [ ] `cd backend && alembic upgrade head` 成功，随后 `alembic check` 输出成功（无 pending change）。
- [ ] `cd backend && make verify` 全绿（含 `test_architecture_boundary.py`：仅新增模型类 + 迁移 + 测试，无反向依赖）。
- [ ] 新测试 `test_rnd293_audit_log.py` 在 `make test` 中执行（无 DB 时按 `_DB_AVAILABLE` 自动 skip，不 FAIL）。

## 七、范围守门（本票只动：模型 + 迁移 + 测试）

- [ ] `git diff --name-only` 仅含：`backend/app/db/models.py` + `backend/alembic/versions/0018_audit_log.py` + `backend/tests/test_rnd293_audit_log.py`。
- [ ] **不**新增/修改任何 router 端点（列表 API 属 A7-3）。
- [ ] **不**新增任何 service / router 写入函数或钩子（写入钩子属 A7-2）。
- [ ] **不**改动 `auth.py` / `routers/auth.py` 等现有文件。
- [ ] 模型**无** `updated_at`、**无** `onupdate`、**无** `update`/`delete` 方法。
- [ ] **不**建 `detail` 内容相关列或任何承载消息正文的字段。
- [ ] **不** git commit / push（由用户本人操作）。
