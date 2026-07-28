# RND-297 开发执行提示词（A8-2 外观/语言偏好持久化）

> 本文件是交给**开发 agent** 的端到端实现 brief。请严格按"落点 → RED→GREEN → 硬约束"执行。
> 工程纪律（全局）：Agent **绝不 `git commit` / `git push`**；实现后交独立 QA agent 验收，由用户决定是否提交。架构冻结 **D1：复用 SSR + 原生 JS，不引 React**（本票以后端为主，前端仅极薄胶水）。代码标识符一律加反引号。

---

## 0. 任务身份与结论

- **Linear**: `RND-297` — `[BLOCKED: F0] A8-2 外观/语言偏好持久化`
- **Parent**: `RND-268` Epic · A8 设置
- **类型 / 优先级**: feature / High (2)
- **现状判定**: **未实现**（代码核查确认，见 §1）。应**创建**本开发 + QA 双提示词。
- **范围**: 后端持久化 `theme` + `locale`（落 `AdminUser` 列）；读端点复用既有 `/api/auth/me`（扩展返回）；新增写端点；偏好在登录后可由客户端应用。Non-goals：**不实现白标 logo 上传**（属 `RND-259` 独立票）。
- **依赖 `F0` 状态**: F0 的数据模型前提已满足 —— `AdminUser` 表结构可安全加列（F0-1 RND-277 已合并，加列走常规 Alembic 迁移）。`[BLOCKED: F0]` 对本票**已非硬阻塞**，可直接开工。

---

## 1. 现状核查（为什么判定未实现）

| 检查项 | 结果 |
|---|---|
| `AdminUser` 的 `theme`/`locale` 列 | **不存在**（grep `app/db/models.py` 零命中） |
| 后端持久化逻辑 | **不存在**；当前 theme/locale 仅存 `localStorage`（`auth.py:73-88` 注释 + `design/ui-v1/pages/settings.html:100` "localStorage: ct-theme"） |
| `/api/auth/me` 端点 | **已存在**（`app/routers/auth.py:944` `auth_me`），返回 `{authenticated, wecom_user_id, display_name, tenant_id, id, role}` —— **不含** theme/locale |
| 有效 theme 值 | `light`（默认）/ `dark`（设计稿 `settings.html:91-97`；CSS `styles.css:70` 仅定义 `[data-theme="dark"]`，light 为默认根变量） |
| 有效 locale 值 | `zh-CN`(默认) / `zh-TW` / `en`（`app/assets/i18n.js` `DEFAULT_LOCALE="zh-CN"` + `availableLocales()`） |
| 迁移 head | `backend/alembic/versions/0019_audit_log.py`（RND-293 已合并）→ 本票新迁移 = **`0020`**，`down_revision="0019"` |
| 路由基线 | `tests/test_http_contract.py:325` 当前 `assert route_count == 46`（含 RND-285 +2）。本票 +1 写路由 → 改为 **N+1**（N=执行时实际值，若其他路由票先合并会 >46；注释追加 `# RND-297: +1 preferences write route.`）。**切勿写死固定数字** |

**结论**：持久化层与写端点缺失；读端点已存在可扩展。底层无阻碍 → 本票 = 加 2 列 + 1 写端点 + 扩展 `/api/auth/me`，无新表（采用 `AdminUser` 列方案，见 §3）。

---

## 2. 精确落点（`文件:行号:函数`）

| 落点 | 动作 |
|---|---|
| `backend/app/db/models.py` `AdminUser`（类起 `:132`） | 新增 `ui_theme` / `ui_locale` 两列（在 `invite_status` 之后） |
| `backend/alembic/versions/0020_admin_user_prefs.py`（**新建**） | `down_revision="0019"`，`add_column` 两列 + server_default |
| `backend/app/routers/auth.py:944` `auth_me` | 扩展返回 `theme` / `locale`（读 `user.ui_theme`/`user.ui_locale`，null 回退默认） |
| `backend/app/routers/auth.py`（同文件，新增） | `PUT /api/auth/me/preferences` 写端点（复用 `auth_me` 的会话解析） |
| `backend/app/schemas/auth.py`（**新建**） | `PreferencesUpdate`（theme?/locale? 可选）+ `PreferencesOut` |
| `backend/app/main.py:94` `app.include_router(auth_router)` | **无需改动**（auth_router 已注册、无 prefix；新端点随其注册） |
| `backend/tests/test_http_contract.py:325` | 读取当前 `route_count` 值 N（执行时可能已因其他路由票合并而 >46），改为 **N+1**（本票新增 1 个路由），注释追加 `# RND-297: +1 preferences write route.`（**不写死固定数字**） |

> 写端点放在 `auth_router` 内（与 `/api/auth/me` 同 router），保持 URL 语义一致（`/api/auth/me/...`）。不要新建独立 router。

---

## 3. 实现方案（RED → GREEN）

### 3.1 模型列（`app/db/models.py`，`AdminUser` 末尾）
```python
    # RND-297 (A8-2): UI 外观/语言偏好持久化
    ui_theme = Column(
        String(16), nullable=False, server_default=text("'light'")
    )
    ui_locale = Column(
        String(16), nullable=False, server_default=text("'zh-CN'")
    )
```
> 放在 `invite_status = Column(...)` 之后、`class AdminSession` 之前。

### 3.2 迁移（`backend/alembic/versions/0020_admin_user_prefs.py`）
```python
"""Add UI theme/locale preferences to admin_users (RND-297 A8-2).

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-29
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("admin_users", sa.Column("ui_theme", sa.String(length=16), nullable=False, server_default="light"))
    op.add_column("admin_users", sa.Column("ui_locale", sa.String(length=16), nullable=False, server_default="zh-CN"))

def downgrade() -> None:
    op.drop_column("admin_users", "ui_locale")
    op.drop_column("admin_users", "ui_locale".replace("ui_locale","ui_theme"))  # 见下注
```
> **downgrade 修正**：分别 `op.drop_column("admin_users", "ui_locale")` 与 `op.drop_column("admin_users", "ui_theme")`（勿照抄上面占位错写）。迁移须 `alembic upgrade head` 在本机跑通、且 `db.downgrade base` 可回滚验证（或至少 `downgrade` 语法正确）。

### 3.3 响应/Pydantic 模型（`app/schemas/auth.py`）
```python
from pydantic import BaseModel
from typing import Optional, Literal

ALLOWED_THEMES = ["light", "dark"]
ALLOWED_LOCALES = ["zh-CN", "zh-TW", "en"]

class PreferencesUpdate(BaseModel):
    theme: Optional[Literal["light", "dark"]] = None
    locale: Optional[Literal["zh-CN", "zh-TW", "en"]] = None

class PreferencesOut(BaseModel):
    theme: str
    locale: str
```
> 后端用 `Literal` 强约束取值；非法值 FastAPI 自动 422。DEFAULT_THEME="light"、DEFAULT_LOCALE="zh-CN"（与 `i18n.js` 及设计稿默认一致）。

### 3.4 会话解析复用（DRY）
`auth_me`（`:944`）现有会话解析（读 `SESSION_COOKIE` → `AdminSession` → `AdminUser`）建议抽为模块内 helper：
```python
def _resolve_session_user(request: Request, db: Session) -> Optional[AdminUser]:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    now = datetime.now(timezone.utc)
    session = (db.query(AdminSession).filter(
        AdminSession.id == session_id,
        AdminSession.expires_at > now,
        AdminSession.is_revoked.is_(False)).first())
    if session is None:
        return None
    return db.query(AdminUser).filter(AdminUser.id == session.admin_user_id).first()
```
`auth_me` 与新增写端点共用之（轻量重构，QA 须确认 `/api/auth/me` 行为不变）。

### 3.5 写端点（`app/routers/auth.py`）
```python
@router.put("/api/auth/me/preferences")
def put_me_preferences(
    payload: PreferencesUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """更新当前登录用户的 theme/locale 偏好（仅本人，fail-closed）。"""
    user = _resolve_session_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    if payload.theme is not None:
        user.ui_theme = payload.theme
    if payload.locale is not None:
        user.ui_locale = payload.locale
    db.commit()
    db.refresh(user)
    return JSONResponse({"theme": user.ui_theme, "locale": user.ui_locale})
```
> 仅更新本人（`user` 来自会话），**不接受**任何 `tenant_id`/`user_id` 请求参数 → 零越权。

### 3.6 扩展 `/api/auth/me`（`auth_me`，约 `:980` 返回字典处）
在返回的 dict 中追加两项（null 回退默认）：
```python
    "theme": user.ui_theme or "light",
    "locale": user.ui_locale or "zh-CN",
```
> 保持既有无 secret 纪律（不返回 token/cookie）。

### 3.7 路由基线（`tests/test_http_contract.py:325`）
读取当前 `route_count` 值 N（执行时可能已因其他路由票合并而 >46），改为 **N+1**（本票新增 1 个写路由）。**切勿写死 `42`/`46` 等固定数字**——以执行时实际值 N 为准，并在注释中追加 `# RND-297: +1 preferences write route.`。示例形如 `assert route_count == N+1  # RND-297: +1 preferences write route.`（其中 N 为当时基线）。

---

## 4. 数据契约（与 `design/ui-v1/pages/settings.html` 对齐）

| 项 | 合法值 | 默认 | 来源 |
|---|---|---|---|
| `theme` | `light` / `dark` | `light` | 设计稿 `settings.html:91-97`；CSS `styles.css:70` |
| `locale` | `zh-CN` / `zh-TW` / `en` | `zh-CN` | `i18n.js` `DEFAULT_LOCALE` + `availableLocales()` |

- 读：`GET /api/auth/me` 响应含 `theme` / `locale`（已落库值；null 回退默认）。
- 写：`PUT /api/auth/me/preferences`，body `{theme?, locale?}`（均可选，partial update），返回更新后 `{theme, locale}`。
- 非法值 → 422（`Literal` 约束）。

---

## 5. 前端「登录后应用」薄胶水（contract + 极小改动）

本票标签为 backend，但验收要求"偏好持久化**并在登录后应用**"。既有客户端已具备主题/语言应用能力（CSS `[data-theme]` + `i18n.js` `I18N.setLocale` / `console-entry.js` `selectLocale`/`applyLocale`），仅缺"从服务端读取并应用"。**极薄胶水**（建议一并实现，约 5 行；若 dev agent 被严格限定 backend-only，则仅交付 API + 记录 apply 契约，apply 作为 `RND-268` 下前端跟随票）：

- 在应用初始化 / `/api/auth/me` 成功后：
  ```js
  I18N.setLocale(me.locale);                 // 复用既有 setLocale
  document.documentElement.setAttribute('data-theme', me.theme);  // 复用既有 [data-theme] 机制
  if (typeof applyLocale === 'function') applyLocale();
  ```
- 不应破坏既有 `localStorage: ct-theme` 的本地兜底（服务端优先、本地兜底）。

---

## 6. 硬约束（不可妥协）

1. **仅本人偏好（fail-closed）**：写端点只改会话解析出的 `user`，**绝不**接受请求体/查询里的 `tenant_id` / `user_id`。零越权、零跨用户。
2. **SF-1 / 数据最小化**：本票不涉及消息内容，无相关风险；但仍须确保响应不含 `password_hash` / `invite_token` / session secret（延续 `auth_me` 纪律）。
3. **不改角色逻辑 / 不改登录语义**：仅加 2 列 + 1 写端点；不触碰 `role`/`status`/鉴权流程。
4. **Non-goals**：**不实现 logo 上传 / 白标**（属 `RND-259`）；**不实现密度(density)** 持久化（设计稿 `settings.html:117` 的紧凑/标准/宽松分段不在本票范围，留作跟随）。
5. **迁移纪律**：必须新增 Alembic 迁移 `0020`（`down_revision="0019"`），本地 `alembic upgrade head` 跑通；不得手工 `ALTER TABLE` 或改 `0019`。
6. **路由基线**：`tests/test_http_contract.py:325` 必须同步 `== 47` 并注释 `RND-297`，否则 CI 失败。
7. **D1 冻结**：仅极薄原生 JS 胶水（§5），不引 React/图表库/新框架。
8. **取值强约束**：`theme` ∈ {light,dark}、`locale` ∈ {zh-CN,zh-TW,en}；非法 → 422。
9. **代码标识符加反引号**（Linear markdown 约定）：如 `AdminUser`、`ui_theme`、`/api/auth/me`、`PreferencesUpdate`。

---

## 7. 验证（GREEN 判定）

- `make test`（或 `pytest backend/tests`）全绿，含 `test_http_contract.py::test_router_count`（== 47）。
- 迁移：`alembic upgrade head` 成功；新列存在且带默认值。
- 新增/并入测试（`tests/test_preferences_api.py` 或并入 auth 测试）：
  - 未登录 `PUT /api/auth/me/preferences` → 401。
  - 登录后 `PUT {theme:"dark",locale:"en"}` → 200 且返回 `{theme:"dark",locale:"en"}`；再 `GET /api/auth/me` 含该值。
  - 非法 `theme:"system"` / `locale:"fr"` → 422。
  - partial：`PUT {theme:"dark"}` 仅改 theme，locale 保持原值。
  - 隔离：用户 A 不能改用户 B 的偏好（本端点无 user_id 入参，结构上保证）。
  - DB 落库确认（`ui_theme`/`ui_locale` 已持久化，重启/其他设备登录后 `GET /api/auth/me` 仍返回）。
- 手动：浏览器登录 → 设置切 dark/en → 刷新/其他设备登录 → 仍应用（验证 §5 胶水，若实现）。

---

## 8. 交付物（交用户）

实现后**不提交**，输出交付报告（见 `agent-dev-qa-brief` 格式）：
- 新增/修改文件清单（`app/db/models.py`、`alembic/versions/0020_*.py`、`app/routers/auth.py`、`app/schemas/auth.py`、`tests/test_http_contract.py` + 新增测试；若含 §5 胶水则列前端文件）。
- 迁移版本号 + 本地 `alembic upgrade head` 结果。
- 测试结果摘要（通过 / 失败）。
- 未决项（如 §5 前端胶水是否实现、density 是否留作跟随）。
- 提示用户：由本人决定是否 `git commit` / 提交 QA。
