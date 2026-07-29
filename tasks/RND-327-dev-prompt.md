[Goal check] This work advances 开发（Development） by 交付 /admin/users 页面模板与路由，对接已就绪的用户管理 API，并使侧栏「员工与坐席」自动点亮。

# RND-327 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-327「R1-1 users 页面：员工与坐席前端实现」｜Linear team `Builder`
- 优先级：High｜风险等级：**R1**｜波次：R1 · 前端快赢四页
- **可与 RND-328/329/330 并行**（前提：严守下方文件所有权）

## 背景与项目现状
后端 **100% 就绪，本票不需要改后端一行代码**。可直接对接（已实地核实）：

| 端点 | 位置 | 用途 |
|---|---|---|
| `GET /api/admin/users` | `backend/app/routers/users.py:23` | 列表 + 筛选 |
| `PATCH /api/admin/users/{user_id}` | `users.py:134` | 启用 / 停用 |
| `POST /api/admin/users/{user_id}/reset-password` | `users.py:161` | 管理员重置密码 |
| `POST /api/admin/users/invite` | `backend/app/routers/auth.py:312` | 邀请 |
| `POST /api/admin/users/accept` | `auth.py:368` | 接受邀请 |

设计稿：`design/Crowntime WeCom Archive Design System/pages/users.html`。

**RND-326 已为你准备好（不要重做，也不要修改）：**
- `backend/app/web/static/design-system.css` —— 直接 `<link>` 引用。
- `backend/app/web/sidenav.py` 的 `render_sidenav(active_id, registered_paths)` —— 直接调用，`active_id="users"`。
- `backend/app/routers/admin_users_page.py` —— **空 stub router，已在 `main.py` 注册好**。你只需在这个文件里加路由。
- `backend/app/assets/i18n.js` 中你的锚点：`/* RND-327 users page keys — insert below */`（3 个 locale 各一处）。

**❗ 本项目高频踩坑（必读）：**
- **没有模板引擎。** `render_template(name, **ctx)` 只做 `__TOKEN__` 单遍替换，**不存在 Jinja**。禁止写 `{% %}` / `{{ }}`。模板里出现未提供的 token 会抛 `KeyError` → 页面 500。
- **i18n 三语**：`zh-CN` / `zh-TW` / `en`，新增文案键三处都要加，缺一即 FAIL。
- **架构边界硬闸**：router 不得 import `app.main`；`app/main.py` 不得新增业务路由（你的路由放自己的 router 文件里，`main.py` 已经注册过了，**不要再动 `main.py`**）。
- **架构冻结 D1**：SSR + 原生 JS，禁 React / Vue / 构建步骤 / SPA 路由。

## 目标（Goal）
让管理员在生产环境用 `/admin/users` 页面完成用户查看、邀请、启停、重置密码，并使侧栏「员工与坐席」从灰色占位变为可点击。

## 范围边界

**In scope：**
1. `backend/app/web/templates/users.html` —— 列表页：用户、角色、状态、最后活跃时间、近 30 天消息数；邀请弹窗；启停与重置密码操作。
2. `backend/app/routers/admin_users_page.py` —— 注册 `GET /admin/users`，渲染模板，传入 `sidenav=render_sidenav("users", <已注册路径集合>)`。
3. i18n：在**自己的锚点正下方**新增 `users.*` 键（3 个 locale）。
4. `backend/tests/test_users_page.py`。

**Out of scope（显式非目标）：**
- 不改任何后端 API（全部已就绪）。
- 不实现离职员工会话继承、不实现绑定手机（A3 epic 已明确 defer）。
- 不碰 `sidenav.py`（导航会因路由注册而**自动**点亮，这是 RND-326 的 AC-4）。
- 不改 `main.py`（stub router 已注册）。
- 不碰其他三张页面票的文件或 i18n 锚点区间。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/users.html`（新）
- `backend/app/routers/admin_users_page.py`
- `backend/app/assets/i18n.js` —— **仅限 `/* RND-327 users page keys */` 锚点正下方**
- `backend/tests/test_users_page.py`（新）

**只读、绝不可写：** `sidenav.py`、`main.py`、`design-system.css`、其他 `admin_*_page.py`、其他锚点区间、所有后端 API 文件。

> 若发现必须改他人文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。不要「顺手改一下」——这会打断整个波次的并行。

## 验收标准（Acceptance Criteria）
- **AC-1 路由可用**：`GET /admin/users` 返回 200，HTML 中无残留 `__TOKEN__` 字面量。
- **AC-2 真实数据渲染**：列表由 `GET /api/admin/users` 的真实响应填充（非 mock、非硬编码假数据），含角色 / 状态 / 最后活跃 / 近 30 天消息数字段。
- **AC-3 三个操作可用**：邀请（`POST /users/invite`）、启用停用（`PATCH /users/{id}`）、重置密码（`POST /users/{id}/reset-password`）均可从页面触发并生效。
- **AC-4 导航自动点亮**：`/admin/users` 注册后，侧栏「员工与坐席」渲染为 `<a href="/admin/users">` 而非灰色占位，**且 `sidenav.py` 未被修改**（`git diff --stat -- backend/app/web/sidenav.py` 无输出）。
- **AC-5 i18n 三语齐全**：新增的每个 `users.*` 键在 `zh-CN`/`zh-TW`/`en` 中均存在；且均位于 RND-327 锚点下方。
- **AC-6 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_users_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py   # 必须无输出（AC-4 / 所有权）
grep -rn '{%\|{{' backend/app/web/templates/users.html              # 必须无输出（无 Jinja）
```

## 依赖（Dependencies）
**阻塞于 RND-326**（需要 `design-system.css` + `sidenav.py` + stub router + i18n 锚点）。RND-326 未完成前不得开始；若其产物缺失 → `BLOCKED_NEEDS_HUMAN`，不要自建替代品。

## 完成定义
- [ ] AC-1 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票 4 个拥有文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：模板漏传 token → `KeyError` → 页面 500。渲染前核对模板中每个 `__TOKEN__` 都有对应 ctx。
- 风险：邀请/重置密码涉及凭据流转，**不得**把任何密码、token 明文写进日志、模板或测试固定值。
- 回滚：新增文件为主，`git checkout -- <files>` 即可。

## 人工点位
- **Trigger**：RND-326 完成后由 Haisu/PM 置 In Progress。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：需改他人文件、或 API 返回字段不足以支撑设计稿 → `BLOCKED_NEEDS_HUMAN`，不要改后端补字段。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`users.py`、`auth.py:312-395`、`sidenav.py`（**只读**，理解如何调用）、设计稿 `pages/users.html`。
2. 在 `admin_users_page.py` 注册 `GET /admin/users`。
3. 写 `users.html`，用 `__TOKEN__` 占位（至少 `__SIDENAV__`），原生 JS 调用 API。
4. i18n 锚点下加 `users.*` 键 ×3 locale。
5. 写 `tests/test_users_page.py` 覆盖 AC-1~AC-5。
6. `make verify` → QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；凭证只从环境变量读。
- 不扩大 Scope；最小正确改动优先。
- 证据优先，以 exit 0 / 测试通过为证。
