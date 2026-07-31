[Goal check] This work advances 开发（Development） by 在已上线的 settings 页面基础上扩出配置中心的分组导航 + 表单骨架，为 T9 的交互完善和 T5 的后端联调预留结构。

# RND-251 开发提示词（Developer Prompt）— 配置中心 T8

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-251 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 关键前提：`settings.html` 已经存在且已上线，本票是扩建不是新建

`backend/app/web/templates/settings.html`（108 行）**已经是一个真实、生产可用的页面**——`GET /admin/settings`（`app/routers/web.py:65` 的 `admin_settings_page`）渲染它，里面有一个完整的"账号 → 修改密码"卡片（<issue>RND-302</issue> 交付，接 `POST /api/admin/settings/password`，**已在生产使用**）。左侧主导航（`app/web/sidenav.py:41`）已经有"设置"这一项指向 `/admin/settings`，**不需要新增导航入口**。

**本票要做的是在这个既有页面内部，再加一层"分组导航"**（RND-244 描述的通用/第三方/存储/企微/高级五组，对应 <issue>RND-247</issue> 的 `CONFIG_REGISTRY` 分组），把现有的"账号"卡片变成分组导航下的其中一个条目（或保留在顶部，视觉上不冲突即可），**不是推倒重做整个页面**。

## 任务身份
- 工单：RND-251「配置中心 T8：前端 Settings 页面框架 + 分组导航 + 表单」｜父 Epic RND-244
- 优先级：Medium｜风险等级：**R1**（改动已上线页面，需保证既有"修改密码"功能零回归）｜milestone：R2 · 开源发布闭环

## 背景与项目现状（已实地核实）

**技术栈：SSR + vanilla JS（D1，冻结）**——复用 `settings.html` 现有结构（`page`/`page-narrow`/`card`/`card-hd`/`set-row`/`field`/`btn` 等既有 CSS class，见文件现有的"账号"卡片作为范例），**不引入 React/Vite/MUI/Tailwind**。

**模板引擎铁律**：`render_template` 只做 `__TOKEN__` 单遍字符串替换（`app/web/__init__.py`），**没有 Jinja**，不要写 `{% for %}`/`{% include %}`。五个分组的表单骨架如果字段数量差异大，用 Python 侧拼 HTML 字符串传入 `__TOKEN__`，或者直接在模板里手写五段静态骨架（**本票允许字段先留空骨架，不要求这一步就把 <issue>RND-247</issue> 的每个字段都硬编码进模板**——T5 的后端 API 落地后，实际渲染哪些字段由 T9 的前端 JS 用 `fetch` 拉取组装，本票只搭"分组导航 + 每组一个空表单容器"的骨架）。

**i18n**：`{i18n_script}` token 已经在现有模板里（`__I18N_SCRIPT__` 占位符），新增的分组标题文案（"通用"/"第三方"/"存储"/"企微"/"高级"）**必须**加进 `backend/app/assets/i18n.js` 的 `LocaleRegistry`，且 **`zh-CN`/`zh-TW`/`en` 三个 locale 都要加**，缺一个就是 FAIL。

**❗ 本项目高频踩坑：**
- **绝不能破坏既有"修改密码"功能**：`change-password-form` 的 DOM 结构、`id`、事件绑定、请求路径 `POST /api/admin/settings/password` 一律不动。
- 架构冻结 D1。
- 本票**可静态开发，不依赖 T5 后端联调**（RND-244 原文说明）——表单骨架先搭出来，字段的真实值展示/保存交互是 T9（<issue>RND-253</issue>）的事。

## 目标（Goal）
在已上线的 settings 页面里加一层左侧分组导航（通用/账号/第三方/存储/企微/高级），每组对应右侧一个表单骨架容器，不破坏现有的"账号→修改密码"功能。

## 范围边界

**In scope：**
1. `backend/app/web/templates/settings.html`（**修改**，不是新建）：
   - 加一层左侧分组导航（vanilla JS 点击切换右侧显示的 section，仿 review_console 或其他多分区页面的现有交互模式——**先读一遍 review_console.html 的分区切换写法**，照抄结构，不要自创一套新的 tab 切换机制）。
   - 现有"账号"卡片内容**原样保留**，归入分组导航的"账号"组（或独立于分组导航之外放在顶部——两种布局都可以，选一种并在 QA Summary 说明）。
   - 新增四个空表单骨架容器：`通用`（域名等）/`第三方`（SMTP 等）/`存储`（七牛云）/`企微`（密钥四件套）/`高级`（缩略图/语音转码等杂项）——每个容器先放一个占位说明文字（如"配置项加载中…"），不要求这一步就有真实表单字段。
2. `backend/app/web/static/settings.js`（**新建**，独立 JS 文件，不要把所有逻辑塞进模板内联 `<script>`）：分组导航的点击切换逻辑（骨架级别，不含 fetch 后端数据——那是 T9 的事）。
3. `backend/app/assets/i18n.js`：新增五个分组标题的 i18n key，**三个 locale 都要有**。
4. 测试：`backend/tests/test_rnd251_settings_page.py`（用现有的 HTTP 行为特征测试模式，验证页面可访问、既有密码表单 DOM 仍存在、新增分组导航容器存在）。

**Out of scope（显式非目标）：**
- 不做真实字段的渲染/保存（属 T9/<issue>RND-253</issue>，需要 T5 的 API 才能联调）。
- 不做敏感字段掩码/显隐（属 T9）。
- 不改 `sidenav.py`（"设置"主导航入口已存在，不需要新增）。
- 不改 `POST /api/admin/settings/password` 的后端逻辑。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/settings.html` —— **修改**：新增分组导航结构，**保留**现有账号卡片
- `backend/app/web/static/settings.js`（新）
- `backend/app/assets/i18n.js` —— 仅新增本票需要的 key（三 locale 齐全）
- `backend/tests/test_rnd251_settings_page.py`（新）

**只读、绝不可写：** `app/routers/web.py`（`admin_settings_page` 函数不需要改，`render_template` 调用方式不变）、`app/routers/settings.py`（T5 拥有，本票不碰后端路由）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 页面可访问**：`GET /admin/settings`（合法会话）200，HTML 中含分组导航结构 + 五个分组容器。
- **AC-2 既有密码修改零回归（关键）**：`change-password-form` 的 DOM（`id="change-password-form"` 等既有 id）与既有 `<script>` 逻辑**逐字未变**；`git diff` 中该部分应体现为"周围新增内容，原有段落不变"，不是整体重写。
- **AC-3 分组导航可切换**：`settings.js` 实现点击左侧分组项切换右侧显示的表单容器（骨架级别，纯前端 DOM 切换，无需后端数据）。
- **AC-4 无 Jinja 语法**：`grep -rn '{%\|{{' backend/app/web/templates/settings.html` 除已有的 `__TOKEN__` 风格占位符外应无 Jinja 标签命中。
- **AC-5 i18n 三 locale 齐全**：新增的分组标题 key 在 `zh-CN`/`zh-TW`/`en` 三个 locale 里都存在。
- **AC-6 架构冻结 D1**：diff 中无 React/Vue/打包器引入的痕迹（无 `package.json` 变化、无 `import React` 之类）。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；既有 8+ 个已上线页面无破版。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd251_settings_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -rn '{%\|{{' backend/app/web/templates/settings.html   # AC-4：应无 Jinja 命中（__TOKEN__ 风格除外）
for k in <新增的分组标题key列表>; do echo "== $k"; grep -c "\"$k\"" backend/app/assets/i18n.js; done   # AC-5：每个应为 3
git diff -- backend/app/web/templates/settings.html    # AC-2：人工核对既有账号卡片段落未变
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
需要 T3（<issue>RND-247</issue>）的分组定义作为参照（哪五组、叫什么名字），但**不需要等 T3 代码落地**——分组名称已在 RND-244 的设计文档里冻结（通用/第三方/存储/企微/高级），可直接按文档开工。不依赖 T4/T5（本票纯前端骨架，不联调后端）。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明分组导航的 DOM 结构（供 T9 直接在此基础上加交互）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：破坏既有"修改密码"功能——由 AC-2 防守，这是唯一一个已经在生产使用的部分。
- 风险 2：自创一套新的 tab 切换机制，与项目里其他多分区页面（如 review_console）的现有模式不一致——照抄现有模式，不要另起炉灶。
- 回滚：`git checkout -- backend/app/web/templates/settings.html backend/app/web/static/settings.js backend/app/assets/i18n.js` 即可；纯前端改动，无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：R1（改动已上线页面），建议看一眼截图/DOM 结构确认没有视觉破版，非强制人工审阅。
- **Escalation**：若发现 review_console 的分区切换模式与本票的"设置分组导航"场景差异太大、照抄会很别扭 → `BLOCKED_NEEDS_HUMAN`，说明具体差异，不要自创一套完全不同的交互模式。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/web/templates/settings.html`（**逐行**，现有结构）、`backend/app/web/templates/review_console.html`（分区切换模式参考）、`backend/app/assets/i18n.js`（现有 key 结构）。
2. 在 `settings.html` 内新增分组导航 + 五个分组容器骨架，保留现有账号卡片。
3. 新建 `settings.js`，实现分组切换。
4. `i18n.js` 加分组标题 key（三 locale）。
5. 写测试覆盖 AC-1~AC-5。
6. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥。
- 不扩大 Scope：不做真实字段渲染/保存、不改后端。
- 复用优先：分区切换模式仿 review_console，CSS class 复用现有 `card`/`field`/`btn` 系列。
- 证据优先，以 exit 0 / 测试通过为证。
