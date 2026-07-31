[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-251 的 7 条 AC（重点验证既有修改密码功能零回归、无 Jinja、i18n 三 locale 齐全）并产出带证据的 PASS/FAIL 判定。

# RND-251 验收提示词（Acceptance / QA Prompt）— 配置中心 T8

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-251「配置中心 T8：前端 Settings 页面框架」｜风险等级 **R1**（改动已上线页面）
- **`settings.html` 承载着已在生产使用的"修改密码"功能——AC-2 从严判定，这不是一张空白页面。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 页面可访问
- 证据：`GET /admin/settings`（合法会话）200，响应体含分组导航结构 + 五个分组容器标记。
- 判定：PASS/FAIL 按是否符合。

### AC-2 — 既有密码修改零回归（关键）
- 证据：`git diff -- backend/app/web/templates/settings.html` 中，`id="change-password-form"`/`id="old-password"`/`id="new-password"`/`id="confirm-new-password"`/`id="change-password-message"`/`id="update-password-button"` 及其 `<script>` 逻辑逐字未变；`POST /api/admin/settings/password` 的调用路径字符串未变。
- 判定：既有段落零改动 = PASS。**任何一处既有 id/逻辑被改动 → 直接 FAIL（`REGRESSION`, severity: blocker）**——这是生产在用功能。

### AC-3 — 分组导航可切换
- 证据：审阅 `settings.js`，确认点击左侧分组项能切换右侧显示的容器（DOM 级别即可，不要求真实数据）。
- 判定：符合 = PASS。

### AC-4 — 无 Jinja 语法
- 证据：`grep -rn '{%\|{{' backend/app/web/templates/settings.html`，命中项应全部是既有的 `__TOKEN__` 风格占位符，不是 Jinja 标签。
- 判定：无 Jinja 命中 = PASS。**出现 `{% %}`/`{{ }}` 风格的 Jinja 标签 → 直接 FAIL（`IMPLEMENTATION_DEFECT`）**——`render_template` 只做单遍字符串替换，Jinja 标签会被原样输出到浏览器。

### AC-5 — i18n 三 locale 齐全
- 证据：本票新增的每个分组标题 key，在 `zh-CN`/`zh-TW`/`en` 三个 locale 块里都能 `grep` 到。
- 判定：全部三个都有 = PASS。**任一 locale 缺失 → 直接 FAIL（`IMPLEMENTATION_DEFECT`）**。

### AC-6 — 架构冻结 D1
- 证据：diff 中无 `package.json`/`webpack.config`/`vite.config` 等构建工具痕迹，无 `import React`/`Vue` 类字符串。
- 判定：无痕迹 = PASS。出现 → FAIL（`SCOPE_VIOLATION`）。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；抽查其他已上线页面（如 `/admin/conversations`）响应仍 200。
- 判定：全部符合 = PASS。

## 本项目专属检查（必查）
1. **未改 `sidenav.py`**：`git diff --stat -- backend/app/web/sidenav.py` 应无输出——"设置"主导航入口已存在，本票不需要新增。
2. **未改后端路由**：`git diff --stat -- backend/app/routers/web.py backend/app/routers/settings.py` 应无输出。
3. **文件所有权**：`git status --porcelain` 中改动应限于 `web/templates/settings.html`（修改）、`web/static/settings.js`（新）、`assets/i18n.js`（仅新增 key）、`tests/test_rnd251_settings_page.py`（新）。

## 附加检查（Security）
- 无真实凭据/密钥出现在模板或 JS 文件中。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd251_settings_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -rn '{%\|{{' backend/app/web/templates/settings.html
git diff -- backend/app/web/templates/settings.html    # AC-2：人工核对既有账号卡片段落是否逐字未变
git diff --stat -- backend/app/web/sidenav.py backend/app/routers/web.py backend/app/routers/settings.py   # 应全无输出
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 产出
写入 `tasks/RND-251-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录分组导航的 DOM 结构说明（供 T9 对接）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-2 若既有密码修改功能有任何回归 → 直接 FAIL（blocker）**，这是本票最不能碰的部分。
