[Goal check] This work advances 开发（Development） by 交付 design-system.css + sidenav 渲染模块 + 4 个 stub router + i18n 锚点，使 RND-327~330 四张页面工单可在 main 上零文件冲突地并行开发。

# RND-326 开发提示词（Developer Prompt）

> 开发 agent 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 项目：Crowntime WeCom Archive / 365 企微会话存档（Linear team `Builder`）
- 工单：RND-326「R1-0 设计系统落地：styles.css + _sidenav.html 接入生产模板」
- Linear：https://linear.app/xyzhs1897/issue/RND-326
- 优先级：Urgent｜风险等级：**R1**
- 波次：R1 · 前端快赢四页 —— **本票是整个波次的前置阻塞项，4 张页面票都等它**

## 背景与项目现状（先对齐，避免跑偏）

**⚠️ 工单标题与旧路线图里的三处说法是错的，以本节为准（已实地核实）：**

1. **不存在 Jinja，也不会引入。** `backend/app/web/__init__.py` 开头明确写着「No template engine is introduced on purpose」。`render_template(name, **ctx)` 只做 `__TOKEN__` 单遍正则替换（`_TOKEN_RE = re.compile(r"__([A-Z0-9_]+)__")`），且**模板里出现未提供的 token 会抛 `KeyError`**。所以「`_sidenav.html` Jinja include」**做不到**，必须改用 `__SIDENAV__` token + Python 侧生成 HTML。
2. **`styles.css` 这个名字已被占用。** `backend/app/web/static/styles.css` **已存在**，服务于 `login.html` / `forgot_password.html` / `reset_password.html` / `settings.html`。设计系统那份同名 `styles.css` 若直接覆盖，会**打烂这 4 个已上线页面**。必须换名落地（本票用 `design-system.css`）。
3. **现有 8 个页面的样式来源有三种，不是一种：**
   | 页面 | 样式来源 |
   |---|---|
   | `messages` / `message_detail` / `message_detail_404` / `diagnostics` | 外链 `base.css` |
   | `login` / `forgot_password` / `reset_password` / `settings` | 外链 `styles.css`（现存那份） |
   | `review_console` / `search` | **页面内联 `<style>` 块**（review_console 约 330 行） |

   因此「把 `base.css` 换成 `styles.css`」这句话在本仓库是不成立的操作。

**当前已有：**
- `render_template` + `STATIC_VERSION`（`app/web/__init__.py`）：`?v=__STATIC_VERSION__` 缓存失效机制已就绪，静态资源改动会自动换 hash。
- 侧栏导航目前**只存在于 `review_console.html`**（约 350–382 行），是手写 `<nav class="side-nav">` markup，12 项里 7 项是灰色 `<span class="side-nav-item side-nav-disabled">…<em data-i18n="nav.comingSoon">即将推出</em></span>` 占位。
- i18n：`backend/app/assets/i18n.js` 的 `LocaleRegistry`，**3 个 locale**：`zh-CN`（约 line 20）、`zh-TW`（约 line 399）、`en`（约 line 778，键名未加引号）。现有 `nav.*` 键见 zh-CN 块 line 294–306。
- 设计系统源文件：`design/Crowntime WeCom Archive Design System/styles.css`（372 行，含 light/dark + 组件层）与 `pages/shell.js`（91 行，`NAV` 配置 + `navHTML()` 渲染函数，注释里写明「In production this becomes a Jinja include」—— 意图对，手段在本仓库不适用）。

**当前缺口（即本票要补的）：**
- ❌ 设计系统 CSS 零引用，未进生产。
- ❌ 导航 markup 硬编码在单个模板里，新增页面无法复用；4 张页面票若各自去改它必然冲突。
- ❌ `audit-log` 在导航结构里**完全不存在**（不是灰色占位，是压根没有这一项），`nav.auditLog` 键也不存在。
- ❌ 无 stub router，4 张页面票会同时改 `app/main.py` 造成冲突。
- ❌ i18n 无分区锚点，4 个 agent 会在同一区域插入键造成冲突。

## 目标（Goal）
把设计系统落地为生产可用的样式与导航基础设施，并**一次性预建好 4 张页面票所需的全部共享骨架**，使 RND-327/328/329/330 可以在 `main` 上零共享文件写入地并行开发。

## 范围边界

**In scope（交付物）：**
1. `backend/app/web/static/design-system.css` —— 从 `design/Crowntime WeCom Archive Design System/styles.css` 落地，**新文件名，不得覆盖现存 `styles.css`**。
2. `backend/app/web/sidenav.py` —— 单一导航配置 + `render_sidenav()`，产出 HTML 字符串；含全部 12 个导航项（含新增「审计日志」）。
3. 导航项**按路由是否已注册自动点亮**（见 AC-4）—— 这是 4 张页面票不需要碰导航的关键。
4. `review_console.html` 的手写 `<nav>` 换成 `__SIDENAV__` token，由 `render_sidenav()` 注入。
5. 4 个 stub router 模块 + 在 `app/main.py` 注册：
   `admin_users_page.py` / `admin_audit_page.py` / `admin_media_page.py` / `admin_contacts_page.py`。
   **stub 阶段不注册任何路由**（保持空 `APIRouter()`），页面票再各自填。
6. i18n：新增 `nav.auditLog`（3 个 locale），并在 3 个 locale 块各插入 4 行锚点注释：
   ```js
   /* RND-327 users page keys — insert below */
   /* RND-328 audit-log page keys — insert below */
   /* RND-329 media page keys — insert below */
   /* RND-330 contacts page keys — insert below */
   ```
7. 测试 `backend/tests/test_sidenav.py`，覆盖 AC-1~AC-5。

**Out of scope（显式非目标）：**
- **不迁移 `review_console` / `search` 的内联 `<style>` 到设计系统**（review_console 约 330 行内联 CSS，属独立高风险工作，另开票）。本票只保证它们**不破版**。
- 不改 `base.css`、不改现存 `styles.css`（避免打烂 8 个已上线页面）。
- 不实现 users / audit-log / media / contacts 四个页面的任何业务内容（那是 RND-327~330）。
- 不引入 Jinja2 或任何模板引擎、不引入 React/Vue/构建步骤（架构冻结 D1）。
- 不改后端 API、不改数据模型、不写 migration。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/static/design-system.css`（新）
- `backend/app/web/sidenav.py`（新）
- `backend/app/web/templates/review_console.html`
- `backend/app/routers/admin_{users,audit,media,contacts}_page.py`（新，4 个 stub）
- `backend/app/main.py`（仅 `include_router` 4 行）
- `backend/app/assets/i18n.js`（仅 `nav.*` 键 + 4 个锚点注释）
- `backend/tests/test_sidenav.py`（新）
- **`backend/app/routers/web.py`（2026-07-29 追加授权，仅限第 47 行 `render_template("review_console", ...)` 调用新增 `sidenav=render_sidenav(...)` 一个关键字参数；`admin_conversations` 函数体其余逻辑、文件里其他路由一律不动）**
- **`backend/tests/_rnd216_web_shims.py`（2026-07-29 追加授权，仅限第 99 行 `review_console_html()` 的 `render_template(...)` 调用同样新增 `sidenav=` 参数；`settings`/`diagnostics` 等其他函数一律不动）**

> 这两个文件是 `render_template("review_console", ...)` 的实际调用点（模板文件本身不调用自己）。加了 `__SIDENAV__` token 后不补这两处会导致：生产 `/admin/conversations` 500（`KeyError`）+ 大量既有测试在 collection 阶段失败。**若你在开工前就读到这份文件，直接把它们当作本票范围的一部分，不需要再走一次 BLOCKED 流程。**

**只读、绝不可写：** 其余全部文件，尤其 `base.css`、现存 `static/styles.css`、其他 7 个模板、`web.py`/`_rnd216_web_shims.py` 里上述两行以外的任何内容。

## 验收标准（Acceptance Criteria）

- **AC-1 设计系统 CSS 落地且不覆盖现存文件**：`backend/app/web/static/design-system.css` 存在且内容源自设计系统 `styles.css`；`backend/app/web/static/styles.css` 的内容**逐字节未改变**（`git diff --stat` 中不出现该文件）。
- **AC-2 单一导航配置**：导航项集合在 `sidenav.py` 中**只定义一处**；`render_sidenav()` 返回 HTML 字符串；`review_console.html` 中不再有手写 `<nav class="side-nav">` 的导航项列表，改由 `__SIDENAV__` 注入。
- **AC-3 审计日志入口存在**：`sidenav.py` 的配置中包含 `audit-log` 项（`/admin/audit-logs`），且 `nav.auditLog` 在 `zh-CN`/`zh-TW`/`en` 三个 locale 中均存在。
- **AC-4 按路由自动点亮（关键）**：给定一组已注册路由路径，`render_sidenav()` 对**路径已注册**的项渲染为可点击 `<a href>`，对**未注册**的项渲染为灰色占位（带「即将推出」）。
  Given `registered_paths` 不含 `/admin/users` → users 项渲染为 disabled；When 传入含 `/admin/users` 的集合 → Then 同一项渲染为 `<a href="/admin/users">`，**无需修改 `sidenav.py`**。
- **AC-5 i18n 锚点就位**：3 个 locale 块中各存在 4 行 RND-327/328/329/330 锚点注释（共 12 行），且现有 i18n 键无一丢失。
- **AC-6 无破版回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；8 个已上线页面路由均返回 200 且 HTML 中无残留 `__TOKEN__` 字面量。
- **AC-7（2026-07-29 追加）生产路由与测试 shim 均已接入 sidenav**：`GET /admin/conversations` 返回 200（真实经 `web.py` 路由，非直接调用 `render_template`）；`backend/tests/_rnd216_web_shims.py` 相关的既有测试全绿；`git diff -- backend/app/routers/web.py backend/tests/_rnd216_web_shims.py` 人工核对每个文件只新增了一行 `sidenav=render_sidenav(...)`，无其他改动。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_sidenav.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/static/styles.css   # 必须无输出（AC-1）
grep -c "RND-32[7-9] \|RND-330 " backend/app/assets/i18n.js   # 应为 12（AC-5）
git diff -- backend/app/routers/web.py backend/tests/_rnd216_web_shims.py   # 人工核对：各只多一行 sidenav=（AC-7）
```
通过 = 7 条 AC 全满足且上述命令 Exit Code 均为 0。

## 依赖（Dependencies）
无前置阻塞。**本票阻塞 RND-327 / 328 / 329 / 330**，应最优先完成。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-6 全满足，每条有对应测试
- [ ] `make verify` 全绿
- [ ] `nav.auditLog` 三语齐全
- [ ] 12 行 i18n 锚点就位
- [ ] `git status` 只显示本票拥有的文件
- [ ] 产出 QA Summary
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险 1：误覆盖现存 `styles.css` → 打烂 login/forgot/reset/settings 四页。**由 AC-1 显式防守。**
- 风险 2：`render_template` 对未提供的 token 抛 `KeyError` → 若模板加了 `__SIDENAV__` 而某个路由没传，该页面直接 500。务必确认 `review_console` 的所有渲染入口都传了 `sidenav=`。
- 风险 3：`sidenav.py` 若 import `app.main` 会触发架构边界硬闸。**不要 import**；已注册路径由调用方传入（FastAPI 处理器里可用 `request.app.routes` 取得）。
- 回滚：纯新增文件 + 一处模板替换，`git checkout -- <files>` 即可，无数据/生产影响。

## 人工点位
- **Trigger**：Haisu 将 RND-326 置 In Progress（已置）。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：若发现「按路由自动点亮」与现有 `web.py` 渲染方式无法调和，或需要改他人拥有的文件 → `BLOCKED_NEEDS_HUMAN`，不要猜。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/web/__init__.py`（务必理解 `render_template` 的 token 机制与 `KeyError` 行为）、`review_console.html` 的 350–382 行、`design/Crowntime WeCom Archive Design System/pages/shell.js`（借鉴其 `NAV` 结构，但用 Python 重写）。
2. 落地 `design-system.css`（新文件名）。
3. 写 `sidenav.py`：一份 `NAV` 配置（分组 + 项 + `path`），`render_sidenav(active_id, registered_paths) -> str`，HTML 转义必须做（参考 `shell.js` 的 `esc()`）。
4. 改 `review_console.html`：删手写导航项列表，插入 `__SIDENAV__`；改对应路由传 `sidenav=render_sidenav("review", paths)`。
5. 建 4 个空 stub router，`main.py` 里 `include_router`。
6. 改 `i18n.js`：加 `nav.auditLog` ×3，加 12 行锚点注释。
7. 写 `tests/test_sidenav.py` 覆盖 AC-1~AC-5（AC-4 要有「同一配置、不同 registered_paths → 渲染态不同」的正反用例）。
8. 跑 `make verify`，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit、不 push、不建分支、不改 git 历史。
- 不改 CI/CD、部署设置、`.gitignore`。
- 不碰生产数据/密钥；凭证只从环境变量读。
- 不扩大 Scope：内联样式迁移、页面业务实现一律 Out。
- 证据优先：以命令 exit 0 / 测试通过为证，不自证。
