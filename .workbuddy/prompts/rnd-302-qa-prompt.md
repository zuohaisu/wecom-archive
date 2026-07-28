# RND-302 验收提示词 — A8-1 修改密码

> 本文件交给**独立 QA agent** 验收。请**自行 grep 核对真实落点**，不要盲信开发提示词的行号假设（函数名才是锚）。
> 工程纪律：Agent **绝不 git commit/push**。架构冻结 D1：SSR + 原生 JS，不引 React。

## 1. 任务身份

- **Linear**：RND-302「[BLOCKED: F0] A8-1 修改密码」｜Epic RND-268 (A8 设置)。
- **验收**：`POST /api/admin/settings/password` 验证旧密码 + 写 `password_hash`，且提供可用的「修改密码」SSR 表单页。

## 2. 验收清单（逐条，[x] 勾选）

### A. 后端端点
- [ ] `POST /api/admin/settings/password` 存在且路径精确（grep `settings_router` / `"/settings/password"`，确认 `main.py` 以 `prefix="/api/admin"` 注册 → 全路径 `/api/admin/settings/password`）。
- [ ] 请求体为 `{old_password, new_password}`（无需 `confirm`，confirm 由前端做）。
- [ ] 旧密码正确 + 新密码合规 → 200 `{"ok":true}`，`password_hash` 更新且新密码可验、旧密码不可验。
- [ ] 旧密码错误 → **401** `invalid_old_password`（非 400，便于前端区分）。
- [ ] 新密码 <8 → 400 `weak_password`。
- [ ] `new_password == old_password` → 400 `same_as_old`。
- [ ] `password_hash is None`（纯 WeCom 用户未设密码）→ 400 `no_password_set`。
- [ ] 响应体**绝不**含 `password_hash` / 明文密码。
- [ ] 操作对象恒为 `get_current_user` 返回的当前用户；无 `tenant_id`/`user_id` 来自请求体。

### B. 鉴权与隔离
- [ ] 端点用 `Depends(get_current_user)`（非 `require_html_session`）；未登录 → 401。
- [ ] 不加 `require_role`（任意角色可改自己密码）。
- [ ] 不重建 session/cookie/JWT（复用 `app/auth.py` 现有 `get_current_user`）。

### C. SSR 设置页
- [ ] `GET /admin/settings` 在 `web.py` 存在，用 `require_html_session`，无会话 → 302 `/admin/login`。
- [ ] 模板 `web/templates/settings.html` 存在，链接 `/web/static/styles.css?v=__STATIC_VERSION__`，含 `__I18N_SCRIPT__` 占位符被正确替换。
- [ ] 页面含「修改密码」卡片，`id="change-password-form"` 标记存在，3 字段 + 按钮。
- [ ] 内联原生 JS：`credentials:'include'` POST 到 `/api/admin/settings/password`，处理 200/400/401，显示中文提示，不回显密码/token。
- [ ] **无 React / `shell.js` / `data-sidenav`**（D1 冻结）。

### D. 范围守门
- [ ] **无新 Alembic 迁移**（`backend/alembic/versions/` 无 RND-302 文件）；`models.py` 未改 `password_hash` 定义。
- [ ] 未实现主题/语言/时区/租户策略/手机绑定/登录设备等（仅修改密码卡片）。
- [ ] 未改 `auth.py`、密码重置流程、F0 鉴权。
- [ ] 密码强度保持 ≥8（grep 确认非 12）。

### E. 契约与测试
- [ ] 新建 `test_rnd302_change_password.py`，含：无 DB 冒烟 + DB 支撑（5 个成功/错误分支）+ SSR 页 200/302 + 路由契约更新。
- [ ] `test_http_contract.py`：`route_count == 46` → `== 48`（或「当前值 + 2」，先读文件确认基线）；`test_routers_are_registered` 的 `expected` 列表新增 `"/api/admin/settings/password"` 与 `"/admin/settings"`。
- [ ] `make verify` 全绿（含 `test_architecture_boundary.py`、`test_http_contract.py`）。
- [ ] `alembic upgrade head` + `alembic check` 零漂移（绿）。
- [ ] 架构边界：新 `routers/settings.py` 未 import `app.routers.*`/`app.main`；`main.py` 仅新增 import + `include_router`（composition root 合规）。

### F. 流程纪律
- [ ] 未 commit / 未 push（Agent 绝不 git commit）。
- [ ] 改动文件集合与开发提示词一致（diff 仅：settings.py、main.py、web.py、settings.html、test_rnd302_*.py、test_http_contract.py 契约两处）。

## 3. 硬门槛（任一失败 = 驳回）

- `make verify` 非全绿。
- `alembic check` 非绿（即便本票无 schema 变更，若报漂移说明误改了模型）。
- 出现新迁移文件（违背零迁移结论）。
- 范围溢出（改了主题/租户策略/React 等）。
- 未更新 `test_http_contract.py` 导致 CI 红。

## 4. 交付报告格式

- 验收结论：**PASS / FAIL**（附失败项）。
- 逐条清单勾选结果。
- 契约同步核对：`route_count` 旧→新、`expected` 新增两条。
- `make verify` + `alembic check` 输出摘要（绿/红）。
- 端到端手测结论：改密成功、新旧密码登录验证。
- 范围守门结论。
- 未 commit 确认。
