# RND-306 验收提示词 · B1-1 PlatformAdmin 实体

> 你（独立 QA agent）对开发 agent 的交付做**独立验收**。只验证、跑测试、写报告，**绝不 git commit / push**。
> 对照 `rnd-306-execution-prompt.md` 逐条核验。所有「证据」须来自真实命令输出，不得凭空断言。

---

## 0. 验收口径

- 任务：RND-306（B1-1 PlatformAdmin 实体），Epic RND-275。
- 核心交付：**独立 `platform_admins` 表 + `PlatformAdmin` 模型 + `0020` 迁移 + `verify_platform_admin` 认证原语**。
- 关键事实锚点：head = `0019`（RND-293 已合并 `1cd781a`）；本票迁移须为 `0020 / down_revision="0019"`。
- 硬门槛：`make verify` 全绿 + `alembic upgrade head` + `alembic check` 绿 + 范围守门通过 + 未 commit。

---

## 1. 验收清单（逐条勾选 `[x]`，附证据）

### 1.1 模型层（`backend/app/db/models.py`）
- [x] 存在 `class PlatformAdmin(Base)`，`__tablename__ = "platform_admins"`。
- [x] 列齐备且类型正确：`id`(String(36) PK)、`email`(Text NOT NULL 隐含唯一约束)、`password_hash`(Text NOT NULL)、`role`(Enum, NOT NULL, server_default `'superadmin'`)、`status`(Enum, NOT NULL, server_default `'active'`)、`created_at`(DateTime, server_default now)、`last_active_at`(DateTime, nullable)。
- [x] **无 `tenant_id`**（tenant-less by design；对照 `AdminUser.tenant_id` 为 NOT NULL）。
- [x] 枚举名/值精确：`role` → `Enum("superadmin", name="platform_admin_role")`；`status` → `Enum("active","disabled", name="platform_admin_status")`。**不得**与既有 `admin_user_role`/`admin_user_status` 重名。
- [x] `email` 有唯一约束（`UniqueConstraint("email", name="uq_platform_admins_email")`）。
- [x] 类加在 db 层（models.py），无新增注册表/import 副作用。

### 1.2 迁移（`backend/alembic/versions/0020_platform_admins.py`）
- [x] `revision="0020"`、`down_revision="0019"`（执行前已 `git log` 确认 head 仍为 `0019`；若 head 变化，报告须说明已顺延并链到实际 head）。
- [x] `upgrade()` 先 `_ROLE_ENUM.create(op.get_bind(), checkfirst=True)` + `_STATUS_ENUM.create(...)`，再 `op.create_table("platform_admins", ...)`，含 `role`/`status` 枚举列、`email` 唯一约束、PK。
- [x] `role`/`status` 带 `server_default`（非「建完即删」写法，避免未来写入缺默认 NOT NULL 报错）。
- [x] `downgrade()` 顺序正确：`drop_index` → `drop_table` → `drop` 两个枚举（逆序）。
- [x] 与 `0017`（枚举先 create）+ `0018`（create_table）范式一致。

### 1.3 认证原语（`backend/app/auth.py`）
- [x] 新增 `verify_platform_admin(db, email, password)`，签名含 `db` 入参。
- [x] **复用** `hash_password`/`verify_password`（`app/auth.py`），无自建哈希函数。
- [x] 语义：邮箱 `strip().lower()` 归一后查；未知邮箱 → `None`；`status != "active"`（disabled）→ `None`；密码错 → `None`；全对 → 返回 `PlatformAdmin` 对象。
- [x] 不签发 session/cookie（那是 B1-2）；不 import `app.routers.*` / `app.main`（架构边界红线）。

### 1.4 测试（`backend/tests/test_rnd306_platform_admin.py`）
- [x] 无 DB 冒烟：导入 `PlatformAdmin`、`verify_platform_admin` 通过（必跑）。
- [x] DB 门控（`DATABASE_URL` 缺失则 skip）覆盖：正确凭据返回对象、错密→None、未知邮箱→None、disabled→None、email 唯一约束触发 `IntegrityError`、`tenant_id` 列不存在。
- [x] 写法镜像 `test_rnd278_password_reset.py`。

### 1.5 质量门禁（硬门槛）
- [x] `make verify` 全绿（含 `test_architecture_boundary.py`）。
- [x] `alembic upgrade head` 成功；`alembic check` 绿（模型与迁移枚举/列/server_default 完全一致）。
- [x] 架构边界：`app/db/models.py` 新增 db 层类、`app/auth.py` 扁平模块新增函数，均未引入对 `app.routers.*`/`app.main` 的反向依赖。

### 1.6 范围守门（防泄漏）
- [x] **`test_http_contract.py` 不得出现在 `git diff`**；`route_count` 断言值**不变**（本票零 router）。
- [x] diff 仅含：`models.py`(新增类) + `0020_*.py` + `auth.py`(仅新增函数) + 新测试文件。
- [x] 无 `routers/` 改动、无 `admin_users`/`AdminUser` 改动、无前端/HTML/React、无登录端点/session。
- [x] **未 commit**（`git status` 显示改动未提交；不出现 commit 记录）。

---

## 2. 失败即判 FAIL 的红线
- `alembic check` 红（模型/迁移枚举不一致、缺 server_default）。
- `make verify` 任一用例失败（尤其 `test_architecture_boundary.py`）。
- diff 含 `routers/` 或 `test_http_contract.py`（范围泄漏）。
- `PlatformAdmin` 误含 `tenant_id`，或枚举与 `admin_user_role`/`admin_user_status` 重名。
- 自建了与 `hash_password` 重复的哈希函数。
- 出现了登录端点/session/cookie 代码（属 B1-2）。
- 已 commit。

---

## 3. 交付报告格式
完成后输出：
- **结论**：PASS / FAIL（含失败项定位）。
- **证据**：`make verify` 关键输出、`alembic upgrade head` + `alembic check` 关键输出、`git diff --stat`、`git status --short`。
- **逐条清单**结果（1.1–1.6 勾选态）。
- **范围守门核验**：`test_http_contract.py` 未被改、route_count 不变、无 router 改动、未 commit。
- **开放项 / 需用户决策**（如有）。
