# RND-276 开发 agent 执行提示词 —— F0-2 per-user 密码鉴权（替换 env 单 hash）

> 面向开发 agent（单人端到端实现 RND-276）。本文件即你的完整 brief。
> 全程不执行 git commit / push（由用户本人操作）。只改工作树，交用户 Review。

## 一、任务（一句话）

将 `backend/app/routers/auth.py` 的密码登录从「全局 env 单 hash（`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH`）」升级为「按 `AdminUser.email` 逐用户校验其 `password_hash`」，复用既有 `verify_password`（PBKDF2）；同时让 `/api/auth/me` 返回真实 `role`。**保留** WeCom OAuth 流程与 env 引导账户的向后兼容；错误响应对「用户名是否存在」零泄露。

## 二、前置依赖（开工前必查）

本任务 **BLOCKED on F0-1（RND-277）**。开工前确认：

- `AdminUser` 已含 `password_hash`（`Text` 可空）与 `role`（`Enum admin_user_role`，NOT NULL，`server_default 'admin'`）。
- migration `0016` 已 `alembic upgrade head` 且 `alembic check` 绿。

校验命令：

```bash
cd backend
python -c "from app.db import models; cols=[c.name for c in models.AdminUser.__table__.columns]; assert 'password_hash' in cols and 'role' in cols, cols; print('F0-1 OK', cols)"
```

若断言失败 → **停下并报告**（依赖未落地，不自行改模型/加迁移）。

## 三、决策背景（已全部拍板，不要再问）

来源：Epic `RND-263`（F0 账号体系重构）子任务 F0-2 + F0-1 已落地的 `AdminUser` 扩展。

- **复用既有 PBKDF2**：`app/auth.py:verify_password`（L123–143，PBKDF2-HMAC-SHA256 / 260000 iters）即逐用户校验函数；**不引入 argon2 等新依赖**（除非用户另行要求）。存储格式沿用 `pbkdf2:sha256:260000:<salt_b64>:<hash_b64>`。
- **登录标识 = `AdminUser.email`**：F0-1 已加 `email` 列；`username` 表单字段现在语义为「邮箱」，按 `email`（trim + lower）在 default 租户内匹配。前端（SSR 登录页，架构冻结 D1）**不改**。
- **env 引导账户向后兼容保留**：`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH` 仍作为首账户引导；当提交的 `username` 不匹配任何 per-user 邮箱时，回退到既有 env 单 hash 校验（保持现有测试与 dev 兜底不变）。
- **Non-goals（不在本票）**：不改 WeCom OAuth 流程；不建用户/改密 UI（属 F0-3）；不做 RBAC 判定（属 F0-5）；不引 React；不触碰 B 层生产路径。
- **工程纪律**：Agent 不 git commit/push；代码标识符加反引号。

## 四、项目现状（精确落点）

### 4.1 现有密码登录（要改写）

`backend/app/routers/auth.py`：

- `password_login` 路由：`L222–326`。
  - 模式守卫：`AUTH_MODE != 'password' → 404`（L237–238，保留）。
  - 读取 env：`auth_settings.admin_username` / `admin_password_hash`（L242–244）；二者为空 → `500` 配置错误（L246–248，保留，保 `test_password_login_missing_config_returns_500` 绿）。
  - 常量时间比较 + PBKDF2 校验：`username_ok = compare_digest(...)` / `password_ok = verify_password(...)`（L250–257）。
  - default 租户解析 fail-closed：`Tenant.slug=='default' and is_active`（L263–270，保留）。
  - 旧 upsert：`AdminUser(id, tenant_id, wecom_user_id=__pwd__<admin>__, name, last_login_at)`（L278–298）。
  - 建会话 + 设 cookie：`L300–326`（复用，cookie 标志 `session_id` / `httponly` / `samesite=lax` / `path=/` 不变）。
- `auth_me` 路由：`L587–628`，返回 `"role": None`（L627，**改成 `user.role`**）。

### 4.2 既有 helper（复用，不改）

- `backend/app/auth.py`：`verify_password`（L123–143）、`hash_password`（L106–120）、`PASSWORD_MODE_WECOM_PREFIX`（L325）、常量（L83–86）。
- `backend/app/settings.py`：`AuthSettings.admin_username` / `admin_password_hash`（L44–52）。

### 4.3 必须保持不变的路由（硬约束）

- `wecom_login`（`L334–368`）与 `wecom_callback`（`L376–579`）**逐字节行为不变**。per-user 改造不得触碰。

### 4.4 现有测试契约（必须继续全绿）

`backend/tests/test_password_auth.py` 钉死了以下行为，RND-276 必须保持：

- `AUTH_MODE=wecom` 时 `/api/auth/password/login` → 404（L327）。
- 错误凭据 / 错误用户名 → 401，且不建会话（L341–410）。
- 正确凭据 → 200 + `{"logged_in": True}` + HttpOnly cookie（L418–487）。
- 响应不泄露 hash/密码（L468–487）。
- 会话绑定 default 租户（L495–549）。
- 缺 env 配置 → 500（L712–732）。
- default 租户缺失/停用 → 500 fail-closed，无会话无 cookie（L821–982）。

此外 `test_http_contract.py:622`（真实 DB）只断言 `authenticated is True`，加 `role` 不影响；`test_auth.py:245` 未认证 `auth_me` 只断言 `authenticated is False` 与无 secret 键，亦不受影响。

## 五、实现步骤（GREEN，最小变更）

### 步骤 1 — 导入补充

`backend/app/routers/auth.py` 顶部 `from sqlalchemy.orm import Session` 旁新增 `from sqlalchemy import func`（per-user 查询用 `func.lower`）。

### 步骤 2 — 改写 `password_login`（L222–326）

保持 `L237–248`（模式守卫 + 缺配置 500）与 `L263–270`（default 租户 fail-closed）**原样**。在拿到 `tenant` 之后、建会话之前，替换「常量时间比较 + env 校验」（L250–257）及其后的 upsert 逻辑为：

```python
    submitted = (body.username or "").strip()
    normalized_email = submitted.lower()

    # F0-2 per-user 路径：按 email 在 default 租户内匹配 active 用户
    candidate = (
        db.query(AdminUser)
        .filter(
            AdminUser.tenant_id == tenant.id,
            func.lower(AdminUser.email) == normalized_email,
            AdminUser.status == "active",  # 注：status 为 Enum 列，比较字符串 'active'
        )
        .first()
    )

    password_ok = False
    resolved_user = None
    if candidate is not None and candidate.password_hash:
        password_ok = verify_password(body.password, candidate.password_hash)
        if password_ok:
            resolved_user = candidate

    # 向后兼容：未命中 per-user 时回退既有 env 单 hash 引导账户
    if not password_ok:
        username_ok = _hmac.compare_digest(submitted, admin_username)
        env_ok = verify_password(body.password, admin_hash)
        if username_ok and env_ok:
            password_ok = True
            # 复用既有 sentinel upsert（逻辑不变；RND-277 后 role/status 有 server_default）
            resolved_user = _upsert_env_admin_user(db, tenant, admin_username, now)

    if not password_ok or resolved_user is None:
        logger.warning("password_login: failed (credentials not logged)")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # —— 以下建会话 + 设 cookie 复用既有代码（L300–326），
    #     将 user 替换为 resolved_user，tenant_id 用 resolved_user.tenant_id ——
```

> `status` 过滤的写法：模型里 `status` 是 `Enum("active","disabled", name="admin_user_status")`（基于原生字符串枚举），直接 `AdminUser.status == "active"` 即可；若 linter/typecheck 报类型，可改为 `AdminUser.status == "active"` 保持不变（SQLAlchemy 会按枚举底层值比较）。保持与既有 `is_active.is_(True)` 风格一致即可。

要点：

- `_upsert_env_admin_user(...)`：把现有 `L278–298` 的 upsert 抽成同名 helper（或直接内联，逻辑不变；注意 RND-277 后 `role`/`status` 有 `server_default`，upsert 不传也能落库）。
- **零泄露**：上面「未命中 / 密码错 / 用户停用」三态最终都走同一个 401 + 同一文案 `"Invalid credentials"`，前端映射到 `login.invalidCredentials`（不变）。**不得**按「用户是否存在」分叉返回。
- **计时均衡**：env 回退分支无条件执行 `compare_digest` + `verify_password(admin_hash)`，每请求至少跑一次 `verify_password`，使「用户不存在」与「密码错」的墙钟时间可比（同既有实现）。
- 成功路径：更新 `resolved_user.last_login_at = now`，建 `AdminSession`（tenant_id=resolved_user.tenant_id）、设 cookie（标志同既有），返回 `{"logged_in": True}`。

### 步骤 3 — 改 `auth_me`（L587–628）

仅改 `L627`：`"role": None` → `"role": user.role`。其余字段（`wecom_user_id` sentinel 处理、`display_name`、`tenant_id`）**原样**。

### 步骤 4 — 修测试 mock（必须）

`backend/tests/test_password_auth.py` 的 `_make_admin_user`（L67–74）与 `_mock_db_for_authenticated_session`（L123–160）构造的 `AdminUser` MagicMock **没有 `role`**。改 `auth_me` 返回 `user.role` 后该 mock 会序列化失败（MagicMock 不可 JSON 序列化）。补：

```python
    u.role = "admin"
    u.email = "admin@example.com"
    u.password_hash = None
    u.status = "active"
```

（其余既有 env 单 hash 测试不受影响，保持全绿。）

### 步骤 5 — 新增 per-user 测试

新建 `backend/tests/test_rnd276_per_user_auth.py`（DB 支撑测试用 `DATABASE_URL` 门控，参照 `test_password_auth.py` 的 mock 工厂风格）：

- P1 成功：插入 `AdminUser(email="alice@example.com", password_hash=hash_password("secret"), role="compliance", status="active", tenant_id=default)`，POST `{username:"alice@example.com", password:"secret"}` → 200 + cookie + `logged_in`。
- P2 错误密码 → 401；**且响应体 == P3**。
- P3 不存在邮箱 → 401；**响应体（status + detail）与 P2 逐字节相同**（零泄露核心证据）。
- P4 `/api/auth/me` 登录后返回 `"role": "compliance"`（非 None）；不泄露 `pbkdf2`/`__pwd__`。
- P5 `status='disabled'` 用户 → 401（等同不存在，不泄露）。
- P6 env 引导账户仍可用（回归）：env `ADMIN_USERNAME/ADMIN_PASSWORD_HASH` 配置下，`username=admin` 仍 200（验证向后兼容）。
- P7 计时提示：代码评审确认每请求至少一次 `verify_password`（可加注释/断言 helper 被调用）。
- 复用 `test_password_auth.py` 既有的 wecom 公开性 / 路由可达性等断言精神，但**不要改动** `test_password_auth.py` 里的 env 契约测试本身（除步骤 4 的 mock 补字段）。

## 六、阶段三验证（RED/GREEN 记录）

RED 基线（改前，确认现状）：

```bash
cd backend
grep -n '"role": None' app/routers/auth.py          # L627
grep -n "compare_digest(body.username, admin_username)" app/routers/auth.py  # 现有 env 单 hash 点
python -c "from app.auth import verify_password; print(verify_password('x','pbkdf2:sha256:260000:xx:yy'))"
```

GREEN（改后）：

```bash
cd backend
python -c "from app.db import models; 'password_hash' in [c.name for c in models.AdminUser.__table__.columns]"  # F0-1 已落地
make verify        # lint-diff typecheck build test 全绿（含新增 test_rnd276_per_user_auth.py）
# 手测（有 DB）：per-user 邮箱登录 200；错密/不存在 401 同体；/api/auth/me 返 role
```

## 七、硬约束（违反即判失败）

- 不 git commit / push。
- 不改 `wecom_login` / `wecom_callback`（L334–579）任何行为。
- 不新增 migration / 不改 `models.py`（schema 由 RND-277 负责；本票无模型变更 → `alembic check` 必须仍绿）。
- 不改 `/api/auth/password/login` 的 URL/方法/成功体 `{"logged_in": True}`/失败体 `"Invalid credentials"`/404 守卫/500 守卫；cookie 标志不变。
- `/api/auth/me` 仅把 `role` 由 None 改为 `user.role`，其余字段不变。
- 错误响应对「用户名是否存在」零泄露（同体 401 + 同文案；计时均衡）。
- 不改前端（SSR 登录页）、不引 React（D1 冻结）。
- 不碰 B 层生产路径（`/srv/apps/wecom-archive-365`、systemd、deploy.yml、`.env.example`、`backend/scripts`）。
- `backend/tests/test_architecture_boundary.py` 必须仍 PASS（仅改 `routers/auth.py` + 测试，不引入反向依赖）。

## 八、收尾（交付物）

向用户交付：RED/GREEN 记录、`git diff --stat`（应仅含 `backend/app/routers/auth.py` + `backend/tests/test_password_auth.py`(mock 补字段) + 新 `backend/tests/test_rnd276_per_user_auth.py`）、`make verify` 日志（全绿）、未提交声明。
