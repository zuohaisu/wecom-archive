# RND-280 开发 agent 执行提示词 —— F0-5 角色枚举 + 权限依赖 scaffold

> 面向开发 agent（单人端到端实现 RND-280）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

在 **F0-1（RND-277，已合并）** 已落地的 `AdminUser.role` 列之上，落地 RBAC 最小 scaffold：
1. 在 `app/auth.py` 定义角色词汇常量 `ADMIN_ROLES`（`owner`/`admin`/`compliance`/`legal`/`readonlyaudit`）；
2. 提供 `require_role(...)` FastAPI 依赖工厂（最小实现，包裹 `get_current_user`）；
3. 让 `/api/auth/me` 返回真实 `user.role`（替换当前硬编码的 `None`）。

**不新增任何 schema/migration**（F0-1 已建列与枚举）、**不把 `require_role` 接到任何既有路由上**（本票只 scaffold，不锁权限）、**不改动 `get_current_user` / WeCom OAuth / 登录写入路径**。

## 二、决策背景（已全部拍板，不要再问）

来源：Epic `RND-263`（F0 账号体系重构）子任务 **F0-5** + 现有账号体系文档 `docs/research/wecom_employee_login_tenant_saas_foundation.md`。

- **角色枚举值已锁定**（与 F0-1 一致）：`owner` / `admin` / `compliance` / `legal` / `readonlyaudit`。DB 原生枚举名 `admin_user_role`，ORM 模型 `AdminUser.role` 为 `str`（见 `backend/app/db/models.py:178-189`，`Enum("owner","admin","compliance","legal","readonlyaudit", name="admin_user_role")`）。
- **最小实现（scaffold）**：本票只建「词汇常量 + `require_role` 依赖工厂 + `auth_me` 返回 role」。**不要**在 `require_role` 之外写任何权限判断矩阵、不要创建角色管理 UI、不要给既有路由加装饰器（那属于后续 F0-3/F0-4 的权限落地，会改既有端点的可达性）。
- **`/api/auth/me` 返回 role** 是 RND-280 与 F0-2（RND-276）的共有诉求。为避免与 F0-2 的开发 agent 冲突，见第四节「重叠协调」。
- **工程纪律**：Agent 不 git commit/push；交付 = 开发提示词 + QA 提示词。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。代码标识符加反引号。

## 三、项目现状（基线，2026-07-28 扫描）

### 3.1 F0-1 已落地（前置依赖，开工前必须校验）
- `backend/app/db/models.py:178-189`：`AdminUser.role` 列已存在（`Enum(..., name="admin_user_role")`，`nullable=False`，`server_default='admin'`）。
- `backend/alembic/versions/0017_admin_users_account_fields.py`：`admin_user_role` 原生枚举已 `CREATE`（L18-25、L31）。
- 契约测试的 mock schema `backend/tests/test_http_contract.py:82` 已含 `role TEXT NOT NULL DEFAULT 'admin'`。
- **开工第一步必须验证**：`python -c "from app.db import models; assert 'role' in models.AdminUser.__table__.c"` 通过，且 `alembic check` 绿。**若不满足（F0-1 未真正落地），立即停下报告，绝不自行补模型/迁移**——那属于 F0-1（RND-277）。

### 3.2 当前鉴权依赖
- `backend/app/auth.py:238-284`：`get_current_user(session_id: Optional[str] = Cookie(...), db) -> Tuple[AdminUser, str]`，无会话/用户 → `HTTPException(401)`。这是所有受保护路由的依赖，也是 `require_role` 的底层。
- `backend/app/auth.py:287-317`：`require_html_session`（HTML 路由用，返回 `Optional[str]` tenant_id），与 `require_role` 无关，勿改。
- `backend/app/auth.py` 已是架构边界允许的 flat service 模块（`_FLAT_SERVICE_MODULES` 含 `"app.auth"`，见 `test_architecture_boundary.py:70-93`），**`require_role` 放这里即可，不要新建 `app/rbac.py`**（否则需同步改 `test_architecture_boundary.py` 的 allowlist，增大改动面）。

### 3.3 当前 `/api/auth/me` 返回 `role=None`
- `backend/app/routers/auth.py:587-628`：`auth_me(request, db)`。
- L611-613：用 `session.admin_user_id` 查出 `user`（已有 `user` 对象）。
- **L626：`"role": None,`** —— 本票唯一的业务改动落点，改为 `user.role`。

### 3.4 ⚠️ 重叠协调：与 RND-276（F0-2）
RND-276 也要求 `auth_me` 返回 `user.role`，且同样需要 `test_password_auth.py` 的 `_make_admin_user` mock（`L67-74`）补 `role`，否则 `auth_me` 会因 `user.role` 是 `MagicMock` 而 500。
- **若 RND-276 已先合并**（即 `auth.py:626` 已是 `user.role` 且 mock 已含 `role`）：**不要回退**，跳转校验后直接做本票的 `ADMIN_ROLES` + `require_role` 部分。
- **若尚未合并**：按本票 3.3 / 3.5 改 `auth_me` 与 mock。两票改动幂等（同一行、同一 mock 字段），合并时不会语义冲突。

### 3.5 ⚠️ 必须修的 mock（否则既有测试 500）
- `backend/tests/test_password_auth.py:67-74`：`_make_admin_user()` 构造的 `MagicMock` 没有 `role` 属性。`auth_me` 改为 `user.role` 后，`auth_me` 序列化会拿到 `MagicMock` → `TypeError` → 500，导致 `test_authenticated_password_session_auth_me_returns_authenticated`（`L578`）失败。
- 修复：在 `u.name = "testadmin"`（L72）之后加 `u.role = "admin"`。

## 四、实现步骤（GREEN，最小变更）

### 步骤 1 — 校验 F0-1 已落地（RED 前先确认前置）
```bash
cd backend
python -c "from app.db import models; assert 'role' in models.AdminUser.__table__.c, 'F0-1 role column missing'; print('F0-1 OK')"
alembic check        # 预期绿（无 pending change）
```
不绿 → 停下报告，不继续。

### 步骤 2 — 在 `app/auth.py` 加角色常量 + `require_role`（放在 `get_current_user` 之后，约 L284 之后）
```python
# ---------------------------------------------------------------------------
# RND-280 (F0-5) — RBAC role vocabulary + require_role scaffold
# ---------------------------------------------------------------------------
# `AdminUser.role` is a plain `str` (the ORM `Enum` for `admin_user_role`
# stores/returns Python strings), so RBAC checks compare against this tuple.
ADMIN_ROLES: tuple[str, ...] = ("owner", "admin", "compliance", "legal", "readonlyaudit")


def require_role(*allowed_roles: str):
    """FastAPI dependency factory (RND-280 / F0-5 RBAC scaffold).

    Wrap with `Depends(require_role("admin", "owner"))` on a route to gate it
    by role. Unauthenticated callers get `get_current_user`'s 401 first; an
    authenticated caller whose `role` is not in `allowed_roles` gets 403.
    Passing no roles allows any authenticated admin role.

    Returns the same `(AdminUser, tenant_id)` tuple as `get_current_user` so
    downstream route signatures are unchanged.
    """
    allowed = set(allowed_roles) if allowed_roles else set(ADMIN_ROLES)

    def _checker(auth: Tuple[AdminUser, str] = Depends(get_current_user)) -> Tuple[AdminUser, str]:
        user, _tenant_id = auth
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient role for this operation")
        return auth

    return _checker
```
- 顶部 import：本文件已 `from typing import Optional, Tuple`（L32）且 `from fastapi import ... HTTPException`（L35），**无需新增 import**。
- 不要改动 `get_current_user` 任何签名/语义。

### 步骤 3 — `auth_me` 返回 `user.role`
- 仅改 `backend/app/routers/auth.py:626`：`"role": None,` → `"role": user.role,`。
- 其余不变（unauthenticated 分支仍只返回 `{"authenticated": False}`，不含 `role` 键——此分支不受本改动影响，且 `test_http_contract.py:582 test_auth_me_unauthenticated` 的「额外键」断言只针对 unauthenticated 响应，安全）。

### 步骤 4 — 修 mock（见 3.5）
- `backend/tests/test_password_auth.py:72` 之后加 `u.role = "admin"`。

### 步骤 5 — 新增测试 `backend/tests/test_rnd280_rbac_scaffold.py`
（参照 `test_rnd277_admin_user_extension.py` 风格，使用 `client` + `app.dependency_overrides[get_db]` 或契约 DB 门控）：
- **T1**：`from app.auth import ADMIN_ROLES` 断言 `ADMIN_ROLES == ("owner","admin","compliance","legal","readonlyaudit")`。
- **T2**：`require_role` 单元行为（用 `fastapi.testclient` 或直接在测试中调用生成的 checker，注入 mock `get_current_user`）：
  - 注入 `auth=("admin_user", "tenant-x")` → `Depends(require_role("admin","owner"))` 返回该 `auth`；
  - 注入 `auth=("readonlyaudit_user", "tenant-x")` → raise `HTTPException(403)`；
  - `Depends(require_role())`（无参）→ 任意角色（含 `readonlyaudit`）均通过。
- **T3**：`/api/auth/me` 在已认证时返回 `role` 等于该用户的 `role`（用契约 DB 的 `user-001`，其 `role` 走 DB 默认 `'admin'` → 断言 `data["role"] == "admin"`）；未认证仍 `{"authenticated": False}` 且无 `role` 等额外键。
- **T4**：`require_role` 包裹的端点未挂载到任何既有路由（见硬约束 G）——用 grep 证明 `backend/app/routers/**` 内当前 `Depends(require_role` 出现次数为 0（除本测试文件外）。

### 步骤 6 — 验证
```bash
cd backend
# 行为校验（可选，需 DB）：
#   client 登录后用 GET /api/auth/me，确认响应含 "role":"admin"
python -m pytest tests/test_rnd280_rbac_scaffold.py tests/test_password_auth.py tests/test_http_contract.py -q
make verify          # 全绿
alembic check        # 仍绿（无 schema 改动）
```

## 五、阶段三验证（RED/GREEN 记录）

RED 基线（改前）：
```bash
cd backend
grep -n '"role": None' app/routers/auth.py          # L626
grep -n "def require_role" app/auth.py              # 应无输出（新增前）
grep -rn "Depends(require_role" app/routers/        # 应无输出
```

GREEN（改后）：
```bash
cd backend
grep -n "ADMIN_ROLES" app/auth.py                  # 出现
grep -n "def require_role" app/auth.py             # 出现
grep -n '"role": user.role' app/routers/auth.py    # L626 已改
python -m pytest tests/test_rnd280_rbac_scaffold.py -q   # 全过
make verify                                        # 全绿
alembic check                                      # 绿
```

## 六、硬约束（违反即判失败）

- 不 git commit / push。
- **不新增/修改任何 schema 或 Alembic migration**（`alembic check` 必须仍绿；`AdminUser.role` 列由 F0-1 负责）。
- **不把 `require_role` 接到任何既有路由**（保持现有端点可达性不变）；scaffold 仅提供依赖 + 测试。
- 不改动 `get_current_user` / `require_html_session` 语义；不动 WeCom OAuth（`wecom_login`/`wecom_callback`）；不动 `password_login` 等登录写入路径。
- 不新建 `app/rbac.py` 等模块（避免改 `test_architecture_boundary.py` allowlist）；`require_role` / `ADMIN_ROLES` 放 `app/auth.py`。
- 不触碰 B 层生产路径（见项目通用纪律：`/srv/apps/wecom-archive-365`、systemd 单元名、deploy.yml、`.env.example`、`backend/scripts` 一个字符不动）。
- 架构边界测试 `backend/tests/test_architecture_boundary.py` 必须仍 PASS（仅加常量/依赖/测试，不引入反向依赖、不碰 `app.main`）。

## 七、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应仅含 `app/auth.py` + `app/routers/auth.py` + `tests/test_password_auth.py`(mock 一行) + 新测试 `tests/test_rnd280_rbac_scaffold.py`）、`make verify` 日志、`alembic check` 绿日志、未提交声明。
