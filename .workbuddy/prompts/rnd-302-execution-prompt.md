# RND-302 开发执行提示词 — A8-1 修改密码

> 本文件交给**开发 agent** 执行。QA 见同目录 `rnd-302-qa-prompt.md`。
> 工程纪律：Agent **绝不 git commit/push**；交付 = 开发提示词 + 验收提示词。架构冻结 D1：SSR + 原生 JS，不引 React。代码标识符加反引号。

## 1. 任务身份

- **Linear**：RND-302「[BLOCKED: F0] A8-1 修改密码」｜Epic **RND-268 (A8 设置)**｜Status: Todo｜Priority: 3｜Estimate: 0.5w｜Labels: backend, settings
- **目标**：实现 `POST /api/admin/settings/password`（验证旧密码 + 写 `password_hash`），并提供一个**可用的「修改密码」表单页**（SSR + 原生 JS，遵循 D1 冻结）。
- **验收**：改密可用。

## 2. 现状核对（已核实，执行前勿重复 grep；函数名是锚，行号仅参考）

- `AdminUser.password_hash` **已存在**：`backend/app/db/models.py:177` `password_hash = Column(Text, nullable=True)`（RND-277 迁移 0017 已落）。→ **本票零迁移，严禁新建 Alembic 迁移 / 改 `models.py`**。
- `hash_password` / `verify_password`：`backend/app/auth.py:127` / `:144`。
- `get_current_user`：`backend/app/auth.py:319`，签名 `-> Tuple[AdminUser, str]`（返回 `(当前用户, tenant_id)`）。**F0-2 鉴权依赖已可用**；`[BLOCKED: F0]` 仅是 epic 就绪层面，符号可用，现在即可实现——**不要重建 session/cookie/JWT**。
- `require_html_session`：`backend/app/routers/web.py:8` import，返回 `tenant_id` 或 `None`（SSR 页鉴权用，配合 302 跳 `/admin/login`）。
- 无 `routers/settings.py`、无 `web/templates/settings.html`、无 `/admin/settings` 路由（已 glob 确认）。
- `render_template(name, i18n_script=...)`：`backend/app/web/__init__.py:39`；`name`→`templates/{name}.html`，`__I18N_SCRIPT__` 占位符由 `i18n_script` kwarg 注入（参照 `forgot_password.html` + `auth.py` 的 `render_template("forgot_password", i18n_script=I18N_SCRIPT_TAG, login_body=...)`）。
- 既有密码强度规则：`backend/app/routers/auth.py:302` `len(body.password) < 8 → weak_password`（RND-278）。**保持一致，强制 ≥ 8 位**。`design/ui-v1/pages/settings.html` 里写的「至少 12 位」只是 UI mockup 措辞，不是强制规则，请勿改成 12。
- 契约测试当前基线：`backend/tests/test_http_contract.py:325` `assert route_count == 49  # RND-291: +1 admin media-library route.` → RND-302 新增 **2 条**路由（见 §3），目标 `== 51`。**务必先读文件确认基线未被其他在途票改动，用「当前值 + 2」**。

## 3. 实现落点（精确）

### 3.1 新建 `backend/app/routers/settings.py`（router 层，自动归类，无需改 `_FLAT_SERVICE_MODULES`；不得 import `app.routers.*` / `app.main`）

```python
"""Settings endpoints (RND-302 / A8-1 change password)."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password, verify_password
from app.db.models import AdminUser
from app.db.session import get_db

settings_router = APIRouter()


class _ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


@settings_router.post("/settings/password")
def change_password(
    body: _ChangePasswordBody,
    current: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Verify the old password and set a new PBKDF2 password hash."""
    user, _tenant_id = current
    if user.password_hash is None:
        raise HTTPException(status_code=400, detail="no_password_set")
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid_old_password")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="weak_password")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="same_as_old")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return JSONResponse({"ok": True})
```

- **租户隔离**：操作对象恒为 `current[0]`（鉴权用户自身），**绝不读请求体里的 tenant/user id**。
- **任意角色**（owner/admin/compliance/legal/readonlyaudit）都可改自己的密码 → **不加 `require_role`**。
- 响应**绝不**回显 `password_hash` / 明文密码。

### 3.2 注册路由 `backend/app/main.py`（composition root，允许 import router）

- 顶部 import 区（约 line 9-19）加：`from app.routers.settings import settings_router`
- `app.include_router(...)` 区块（约 line 96-106）加：`app.include_router(settings_router, prefix="/api/admin")`（放在 `users_router` 那行附近）。
- 全路径 → `POST /api/admin/settings/password` ✅（与任务要求精确一致）。

### 3.3 SSR 设置页 `backend/app/routers/web.py`（加一个 GET 路由，镜像 `admin_conversations` line 39-59）

```python
@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(tenant_id: Optional[str] = Depends(require_html_session)):
    """Settings page (RND-302). Requires valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=render_template("settings", i18n_script=I18N_SCRIPT_TAG))
```

### 3.4 模板 `backend/app/web/templates/settings.html`（新建，参照 `forgot_password.html` 头 + `styles.css` 设计系统）

- `<head>`：favicon（带 `?v=__STATIC_VERSION__`）、`<link rel="stylesheet" href="/web/static/styles.css?v=__STATIC_VERSION__">`、`<title>设置</title>`。
- 正文：**仅一个「账号 / 修改密码」卡片**（设计稿里其余 tab——偏好、租户策略、绑定手机、登录设备——全部属后续 A8 子票，**严禁在本票实现**）。视觉对齐 `design/ui-v1/pages/settings.html` 的「修改密码」区块，但**用 SSR + 原生 JS 实现，禁止引入 ui-v1 的 `shell.js` / `data-sidenav` React 壳**（D1 冻结）。
- 3 个字段（当前密码 / 新密码 / 确认新密码）+「更新密码」按钮，使用 `styles.css` 的 `.card` / `.set-row` / `.field` / `.input` / `.btn` 类。
- 表单容器加稳定标记 `id="change-password-form"`（供契约测试断言，参照 `test_rnd278` 的 `id="reset-form"`）。
- 内联原生 JS（`<script>`）：点击「更新密码」→ 读三字段 → 客户端校验（新密码 ≥8 且两次一致）→
  `fetch('/api/admin/settings/password', {method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include', body: JSON.stringify({old_password, new_password})})`
  → 200 显示成功并清空字段；400 `weak_password`/`same_as_old`/`no_password_set`、401 `invalid_old_password` 显示对应中文提示；**绝不回显 token / 密码**。
- 末尾放 `__I18N_SCRIPT__`（与 `forgot_password.html` 一致）；如用到 `data-i18n` 则保留 `applyI18n()` 调用。

### 3.5 测试 `backend/tests/test_rnd302_change_password.py`（新建，镜像 `test_rnd278_password_reset.py`）

- **无 DB 冒烟**（必跑，验证模块可导入、无反向依赖）：import `change_password` + `_ChangePasswordBody`，断言可调用。
- **DB 支撑**（建 `Tenant` + `AdminUser(password_hash=hash_password("old-pass-123"))`，用 `TestClient(app)` 或直调端点）：
  - 正确旧密码 + 合规新密码 → 200 `{"ok":true}`，`verify_password("new-pass-123", user.password_hash)` 为 True，`verify_password("old-pass-123", ...)` 为 False。
  - 旧密码错误 → 401 `invalid_old_password`。
  - 新密码 <8 → 400 `weak_password`。
  - `new_password == old_password` → 400 `same_as_old`。
  - `password_hash is None`（纯 WeCom 用户）→ 400 `no_password_set`。
- **SSR 页**：`TestClient` GET `/admin/settings` 带有效会话 → 200 且含 `id="change-password-form"` 与 `/web/static/styles.css`；无会话 → 302 跳 `/admin/login`。
- **契约更新**（关键，防 RND-323 类返工）：
  - `test_http_contract.py:325` `assert route_count == 49` → 改为 `== 51`（RND-302 新增 `POST /api/admin/settings/password` + `GET /admin/settings` 共 2 条）。**先读文件确认基线是否仍是 49**，若已被其他在途票改过，用「当前值 + 2」。注释改为 `# RND-302: +2 (settings password + settings page).`。
  - `test_routers_are_registered` 的 `expected` 列表（约 line 335-385）追加两条：`"/api/admin/settings/password"`、`"/admin/settings"`（否则该测试会报 extra/missing）。

## 4. 范围守门（严禁）

- ❌ 不加 Alembic 迁移 / 不改 `models.py`（`password_hash` 已存在）。
- ❌ 不引 React / `shell.js` / `data-sidenav`（D1 冻结：SSR + 原生 JS）。
- ❌ 不实现整个设置页（主题切换、语言、时区、租户策略、绑定手机、登录设备等）——属后续 A8 子票。本票只做「修改密码」卡片。
- ❌ 不把密码强度改成 12 位（保持 ≥8，与 RND-278 一致）。
- ❌ 不实现密码历史/复用检查（无 schema，属后续票）。
- ❌ 不改 `auth.py` / 密码重置流程 / F0 鉴权 / 其他 epic 代码。
- ❌ 不 commit / push。

## 5. 验证门槛（硬）

- `make verify` 全绿（含 `test_architecture_boundary.py` 零反向依赖、`test_http_contract.py` 路由数 + 路由注册清单同步更新）。
- `alembic upgrade head` + `alembic check` 零漂移（本票无 schema 变更，应直接绿）。
- `make test` 含新 `test_rnd302_change_password.py` 全绿。
- 端到端手测：登录后访问 `/admin/settings` → 填表单 → 改密成功 → 用新密码可登录、旧密码不可登录。

## 6. 交付报告格式

完成后输出（中文）：
- 改动文件清单（绝对路径）+ 每文件改了什么。
- 路由契约同步：`route_count` 旧→新、`expected` 列表新增两条。
- 验证结果：`make verify` / `alembic check` 输出摘要。
- 范围守门自检：确认未触碰迁移/模型/其他 epic/前端 React。
- 未 commit 声明。
