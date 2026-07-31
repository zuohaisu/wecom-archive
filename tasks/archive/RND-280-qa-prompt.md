# RND-280 QA / 验收 agent 提示词 —— F0-5 角色枚举 + 权限依赖 scaffold

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-280-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认在已落地的 F0-1 `AdminUser.role` 之上，RND-280 完成了 RBAC 最小 scaffold：
1. `app/auth.py` 新增 `ADMIN_ROLES` 常量（5 值）；
2. `app/auth.py` 新增 `require_role(...)` 依赖工厂（包裹 `get_current_user`，401/403/200 三态正确）；
3. `/api/auth/me` 返回真实 `user.role`（替换硬编码 `None`）；
4. 配套修复 `test_password_auth.py` 的 `_make_admin_user` mock，并新增 `test_rnd280_rbac_scaffold.py`。

**零回归**：不改 schema（`alembic check` 仍绿）、不改 `get_current_user`/WeCom/登录路径、**不把 `require_role` 接到既有路由**（现有端点可达性不变）、`make verify` 全绿。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 前置依赖（F0-1 已合并）
- [ ] P1 `AdminUser.role` 列存在 —— 证据：`python -c "from app.db import models; assert 'role' in models.AdminUser.__table__.c"`
- [ ] P2 `alembic check` 绿（开发 agent 未动 schema；若红，先判其越界改了模型/迁移）

### 角色常量（文件 `backend/app/auth.py`）
- [ ] B1 存在 `ADMIN_ROLES: tuple[str, ...]`，值恰为 `("owner","admin","compliance","legal","readonlyaudit")` —— 证据：grep + 断言
- [ ] B2 `ADMIN_ROLES` 与模型枚举一致：`app/db/models.py:178-189` 的 `Enum(...,name="admin_user_role")` 标签集 == B1 —— 证据：读两端

### `require_role` 依赖工厂（文件 `backend/app/auth.py`）
- [ ] B3 `def require_role(*allowed_roles: str)` 存在，内部定义 checker 以 `Depends(get_current_user)` 注入 `Tuple[AdminUser, str]` —— 证据：读 `app/auth.py`
- [ ] B4 无参 `require_role()` → `allowed` 为全部 `ADMIN_ROLES`（任意角色通过）
- [ ] B5 行为矩阵（用单测或 testclient 注入 mock `get_current_user` 验证）：
  - 未认证（mock `get_current_user` raise 401）→ `require_role("admin")` 调用 → **401**
  - 认证且 `role="admin"` → `require_role("admin","owner")` → **返回 (user, tenant)**
  - 认证且 `role="readonlyaudit"` → `require_role("admin","owner")` → **403**
  - 认证且 `role="readonlyaudit"` → `require_role()`（无参）→ **通过**

### `/api/auth/me` 返回 role（文件 `backend/app/routers/auth.py:626`）
- [ ] A1 `auth_me` 已改为 `"role": user.role`（不再为 `None`）—— 证据：grep L626
- [ ] A2 契约 DB 已认证 GET `/api/auth/me` 返回 `role` 等于该用户 `role`（如 `user-001` 走默认 `'admin'` → `data["role"] == "admin"`）—— 证据：`tests/test_rnd280_rbac_scaffold.py::T3` 或 curl
- [ ] A3 unauthenticated GET `/api/auth/me` 仍 `{"authenticated": False}`、**不含 `role` 键** —— 证据：`tests/test_http_contract.py:582 test_auth_me_unauthenticated`（额外键断言）仍 PASS

### Mock 修复（文件 `backend/tests/test_password_auth.py`）
- [ ] M1 `_make_admin_user`（`L67-74`）已设 `u.role = "admin"` —— 证据：grep
- [ ] M2 `tests/test_password_auth.py::test_authenticated_password_session_auth_me_returns_authenticated`（L578）PASS（不再因 `user.role` 为 MagicMock 而 500）—— 证据：pytest

### 新增测试
- [ ] T1 `backend/tests/test_rnd280_rbac_scaffold.py` 存在且覆盖 B1/B5/A2/A3/T4 —— 证据：文件存在 + pytest 全过

### 范围守门（scaffold，不得溢出）
- [ ] G1 `git diff --name-only` 仅含：`backend/app/auth.py` + `backend/app/routers/auth.py` + `backend/tests/test_password_auth.py`（mock 一行）+ `backend/tests/test_rnd280_rbac_scaffold.py`
- [ ] G2 `git diff` **不含**任何 Alembic migration 文件、**不含** `app/db/models.py` 改动（无 schema 变更）
- [ ] G3 `git grep "Depends(require_role"` 在 `app/routers/**` 中出现次数为 **0**（scaffold 不接既有路由；允许仅出现在 `tests/test_rnd280_rbac_scaffold.py`）—— 证据：grep
- [ ] G4 未改动 `get_current_user` / `require_html_session` / WeCom OAuth / `password_login` 路径 —— 证据：`git diff` 比对

### 全局契约
- [ ] C1 `make verify` 全绿（含 `test_architecture_boundary.py`：`require_role`/`ADMIN_ROLES` 在 `app/auth.py`，无新模块、无 `service→router`、无 `router→main`）
- [ ] C2 `alembic check` 绿（无 pending change）
- [ ] C3 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）

## 三、回归套件

1. `cd backend && make verify`（全量；应含 `test_rnd280_rbac_scaffold.py` + `test_password_auth.py` + `test_http_contract.py`）。
2. 若环境有 `DATABASE_URL`：额外确认 `alembic check` 绿、契约 DB 下 `auth_me` 返回正确 `role`。
3. 若 `DATABASE_URL` 缺失：DB 支撑类测试按门控自动 skip；此类项标记「需有 DB 环境复测」，不得判 FAIL。

## 四、智能路由判定（每轮必给）

- `ADMIN_ROLES` 缺值/错值、`require_role` 三态不对（如该 403 却 200、该 401 却绕过）→ 反馈开发 agent 修复，附 `app/auth.py` 具体行号 + 期望。
- `auth_me` 仍是 `role: None` 或 unauthenticated 多了 `role` 键（破坏 `test_http_contract` 额外键断言）→ 反馈修复，附 `app/routers/auth.py:626`。
- `_make_admin_user` 未补 `role` 导致 `test_password_auth` 500 → 反馈补 mock（L72 后加 `u.role="admin"`）。
- 溢出本票范围（动了 migration/models、把 `require_role` 接到既有路由、改了登录/WeCom）→ 判 FAIL 并明确标注范围溢出。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式

```
RND-280 验收结论：PASS / FAIL
前置：F0-1 role 列 __（在/缺）；alembic check __（绿/红）
角色常量：B1/B2 PASS/FAIL
require_role：B3/B4/B5 PASS/FAIL（401/403/200 矩阵）
auth_me：A1/A2/A3 PASS/FAIL（返回 user.role；unauth 无 role 键）
Mock 修复：M1/M2 PASS/FAIL
新增测试：T1 PASS/FAIL
范围守门：diff 文件清单 __；无 migration/model 改动 __；G3 接路由=0 __；登录/WeCom 零改动 __
回归：make verify __（绿/红）；alembic check __（绿/红）
契约：未 commit __
遗留：__
```
