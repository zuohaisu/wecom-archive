# RND-306 开发执行提示词 · B1-1 PlatformAdmin 实体

> 交付物：本文件（开发提示词）+ `rnd-306-qa-prompt.md`（验收提示词）。
> 你（开发 agent）只改代码、跑测试、写报告，**绝不 git commit / push**（硬规则）。
> 架构冻结 D1：本票纯后端实体，**无前端、无 React**。代码标识符加反引号。

---

## 0. 任务定位（来自 Linear RND-306 + Epic RND-275）

- **RND-306** = `B1-1 PlatformAdmin 实体`，隶属 **Epic RND-275（B1 平台总控台 super-admin）**。
- Epic 原文明确规定：**`PlatformAdmin（独立于 adminusers，隔离更清晰）`** —— 即一个**全新的、与 `admin_users` 无关的表**，不是往 `admin_users` 加列。
- 现有 `AdminUser.tenant_id` 是 `nullable=False`（`backend/app/db/models.py:159-161`），而平台超管是**跨租户、无租户归属**的，因此 PlatformAdmin **必须不包含 `tenant_id`**（tenant-less by design）。这从模型层面印证了「独立表」决策。
- 验收准则「超管可登录平台域」：本票交付**实体 + 持久化 + 一个可测试的认证原语 `verify_platform_admin`**（模型层，复用既有 `hash_password`/`verify_password`）。**HTTP 登录端点、session 签发、租户过滤绕过** 属 **B1-2（RND-305）**，明确 OUT OF SCOPE。
- 状态：`[BLOCKED: F0]` 仅表示 epic 就绪层面依赖 F0；实体本身不依赖任何 F0 符号，现在即可实现（同 RND-281 的「软 BLOCKED」先例）。

---

## 1. 现状核查（已确认，作为事实锚点）

- `backend/app/db/models.py:1-22`：`Enum`、`Text`、`String`、`Column`、`ForeignKey`、`Index`、`UniqueConstraint`、`func`、`text` 均已导入；`JSONB` 来自 `sqlalchemy.dialects.postgresql`。
- `AdminUser`（`models.py:158-201`）：`tenant_id` NOT NULL，`role` 用 `Enum(..., name="admin_user_role")`、`status` 用 `Enum("active","disabled", name="admin_user_status")` —— **这些是本票要避免重名的枚举**（平台角色是独立枚举）。
- `app/auth.py:127` `hash_password(plain)`、`app/auth.py:144` `verify_password(plain, stored_hash)`：通用 PBKDF2-HMAC-SHA256，与租户无关，**可直接复用**，无需新写哈希函数。
- `app/auth.py:319` `get_current_user` 已存在（扁平模块 `app/auth.py`，已通过架构边界测试）。
- **迁移链现状（关键）**：截至本提示词撰写时，**head = `0019`**（`0019_audit_log.py`，RND-293 已合并，commit `1cd781a`，`class AuditLog` 在 `models.py:232`）。因此 **本票新迁移 = `0020`，`down_revision="0019"`**。
- 原生枚举范式见 `backend/alembic/versions/0017_admin_users_account_fields.py`（先 `_ENUM.create(op.get_bind(), checkfirst=True)` 再建列/表）。
- `create_table` 范式见 `backend/alembic/versions/0018_password_reset_tokens.py`。

---

## 2. 实现清单（按序勾选 `[x]`）

- [x] **防御性确认 head**：执行前跑 `git log --oneline -- backend/alembic/versions/`，确认最新 revision 仍是 `0019`、且尚无 `0020`。若 head 已变（例如其他票先占了 `0020`），把本票的 `down_revision` 链到**实际 head**、revision 号顺延，并在报告注明。
- [x] **`models.py` 新增 `PlatformAdmin` 类**（加在 `AuditLog`/`PasswordResetToken` 之后，db 层，无需注册表）：
  ```python
  # —— RND-306 (B1-1) 平台超管实体（独立于 admin_users，tenant-less）——
  class PlatformAdmin(Base):
      """Platform super-admin, isolated from per-tenant admin_users.

      Tenant-less by design: a platform admin operates across all tenants
      (cross-tenant scope is enforced at the auth layer, see B1-2 / RND-305).
      """

      __tablename__ = "platform_admins"
      __table_args__ = (
          UniqueConstraint("email", name="uq_platform_admins_email"),
          Index("ix_platform_admins_status", "status"),
      )

      id = Column(String(36), primary_key=True)
      email = Column(Text, nullable=False)
      password_hash = Column(Text, nullable=False)
      role = Column(
          Enum("superadmin", name="platform_admin_role"),
          nullable=False,
          server_default=text("'superadmin'"),
      )
      status = Column(
          Enum("active", "disabled", name="platform_admin_status"),
          nullable=False,
          server_default=text("'active'"),
      )
      created_at = Column(
          DateTime(timezone=True), nullable=False, server_default=func.now()
      )
      last_active_at = Column(DateTime(timezone=True), nullable=True)
  ```
  > **枚举名/值必须与迁移完全一致**，否则 `alembic check` 红。

- [x] **新增迁移 `backend/alembic/versions/0020_platform_admins.py`**（`revision="0020"`, `down_revision="0019"`），范式严格对齐 `0017`（枚举先 create）＋ `0018`（create_table）：
  ```python
  """Add PlatformAdmin entity (RND-306 B1-1).

  Revision ID: 0020
  Revises: 0019
  Create Date: 2026-07-29
  """

  from typing import Sequence, Union

  import sqlalchemy as sa
  from alembic import op

  revision: str = "0020"
  down_revision: Union[str, None] = "0019"
  branch_labels: Union[str, Sequence[str], None] = None
  depends_on: Union[str, Sequence[str], None] = None

  _ROLE_ENUM = sa.Enum("superadmin", name="platform_admin_role")
  _STATUS_ENUM = sa.Enum("active", "disabled", name="platform_admin_status")


  def upgrade() -> None:
      # create_table() does not create PostgreSQL native enum types itself.
      _ROLE_ENUM.create(op.get_bind(), checkfirst=True)
      _STATUS_ENUM.create(op.get_bind(), checkfirst=True)

      op.create_table(
          "platform_admins",
          sa.Column("id", sa.String(length=36), nullable=False),
          sa.Column("email", sa.Text(), nullable=False),
          sa.Column("password_hash", sa.Text(), nullable=False),
          sa.Column(
              "role",
              _ROLE_ENUM,
              nullable=False,
              server_default=sa.text("'superadmin'"),
          ),
          sa.Column(
              "status",
              _STATUS_ENUM,
              nullable=False,
              server_default=sa.text("'active'"),
          ),
          sa.Column(
              "created_at",
              sa.DateTime(timezone=True),
              server_default=sa.text("now()"),
              nullable=False,
          ),
          sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
          sa.PrimaryKeyConstraint("id"),
          sa.UniqueConstraint("email", name="uq_platform_admins_email"),
      )
      op.create_index(
          "ix_platform_admins_status", "platform_admins", ["status"], unique=False
      )


  def downgrade() -> None:
      op.drop_index("ix_platform_admins_status", table_name="platform_admins")
      op.drop_table("platform_admins")
      _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
      _ROLE_ENUM.drop(op.get_bind(), checkfirst=True)
  ```
  > 注意：建表时 `role`/`status` 带 `server_default`（与 RND-277 的教训一致——任何既有/未来的平台 admin 写入路径都不会因 NOT NULL 缺默认而崩）。**不要**照搬「建完即删 server_default」的写法。

- [x] **`app/auth.py` 新增 `verify_platform_admin` 认证原语**（复用既有 PBKDF2，扁平模块、已通过架构边界）：
  ```python
  # —— RND-306 (B1-1) 平台超管认证原语（HTTP 登录/session 属 B1-2/RND-305）——
  def verify_platform_admin(db, email: str, password: str):
      """Return the active PlatformAdmin for correct credentials, else None.

      Reuses hash_password/verify_password (PBKDF2). Does NOT issue sessions
      or cookies — that is B1-2's job. Disabled admins are rejected.
      """
      from app.db.models import PlatformAdmin  # lazy import to avoid cycles

      admin = (
          db.query(PlatformAdmin)
          .filter(PlatformAdmin.email == email.strip().lower())
          .first()
      )
      if admin is None or admin.status != "active":
          return None
      if not verify_password(password, admin.password_hash):
          return None
      return admin
  ```
  > 签名用 `db` 入参（镜像 `usageservice` 风格），便于单测与调用方注入 Session；**不要**在这里 import `app.routers.*` / `app.main`（架构边界红线）。

- [x] **新增测试 `backend/tests/test_rnd306_platform_admin.py`**：
  - [x] 无 DB 冒烟：导入 `PlatformAdmin`、`verify_platform_admin` 不报错（必跑，验证符号/落点）。
  - [x] DB 支撑（`DATABASE_URL` 门控，无则 `pytest.skip`）：
    - 插入一条 `PlatformAdmin`（`email` 小写归一、`password_hash=hash_password("secret123")`、`role="superadmin"`、`status="active"`），`verify_platform_admin(db, email, "secret123")` 返回该对象。
    - 错误密码 → `None`；未知邮箱 → `None`；`status="disabled"` → `None`。
    - `email` 唯一约束：重复 email 插入触发 `IntegrityError`。
    - 断言表存在且列类型正确（`role`/`status` 为枚举、`email` 有唯一约束、`tenant_id` **不存在**）。
  - [x] 镜像 `backend/tests/test_rnd278_password_reset.py` 的 DB 门控写法。

- [x] **`make verify` 全绿**：含 `test_architecture_boundary.py`（db 层新增类、扁平模块 `auth.py` 新增函数，均不引入反向依赖）。

- [x] **`alembic upgrade head` + `alembic check` 全绿**（Alembic 1.14 支持 `check`）：模型 `PlatformAdmin` 与 `0020` 迁移的表/列/枚举（`platform_admin_role`、`platform_admin_status`、值集合、server_default）完全一致。

- [x] **范围守门自检**：`git diff --stat` 仅含 `models.py` + `0020_*.py` + `auth.py`(仅新增函数) + 新测试文件。**不得**出现任何 `routers/` 改动、不得改 `test_http_contract.py`、不得改 `admin_users`/`AdminUser`。

- [x] **不 commit**：停在这里，交 QA agent 验收；由用户本人决定提交。

---

## 3. 路由判定（防契约雷）

- 本票**不新增任何 router / 端点**（登录端点属 B1-2）。
- 因此 **`backend/tests/test_http_contract.py` 不得出现在 `git diff` 中**，`route_count` 断言值**不变**。若 diff 含该文件或路由数变化 → 范围泄漏，立即回退。

---

## 4. 范围边界（明确 OUT OF SCOPE）

| 项 | 归属 | 本票是否做 |
|---|---|---|
| `platform_admins` 表 + `PlatformAdmin` 模型 + `0020` 迁移 | RND-306 | ✅ |
| `verify_platform_admin` 模型层认证原语 | RND-306 | ✅（复用 `hash_password`/`verify_password`） |
| HTTP 登录端点 / session 签发 / cookie | B1-2 (RND-305) | ❌ |
| 跨租户鉴权作用域（绕过 tenant_id 过滤） | B1-2 (RND-305) | ❌ |
| SSR 平台总控台页面（`design/ui-v1/pages/platform.html`） | B1 后续子票 | ❌（且 D1 冻结禁 React） |
| 内容访问申请 gate / 审计 | B1-5 (RND-309) / A7 | ❌ |
| 改动 `admin_users` / `AdminUser` | — | ❌ |
| 任何前端 / React | D1 冻结 | ❌ |

---

## 5. 交付报告格式（交给 QA + 用户）

完成后输出，至少包含：
- 改动文件清单（`git diff --stat` 节选）。
- 新增 `PlatformAdmin` 列/枚举/约束一览表。
- 迁移 revision（`0020` / `down_revision="0019"`）及执行前 head 确认结果。
- `make verify` + `alembic upgrade head` + `alembic check` 结果（贴关键行）。
- 范围守门核验：`test_http_contract.py` 未被改动、route_count 未变、无 router 改动。
- 未 commit 声明。
- 遗留 / 需用户决策的开放项（如有）。
