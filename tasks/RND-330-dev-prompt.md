[Goal check] This work advances 开发（Development） by 交付 /admin/contacts 页面模板与路由，对接 RND-288 交付的外部联系人列表 API，并使侧栏「外部联系人」自动点亮。

# RND-330 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-330「R1-4 contacts 页面：外部联系人前端实现」｜Linear team `Builder`
- 优先级：High｜风险等级：**R1**｜波次：R1 · 前端快赢四页
- **可与 RND-327/328/329 并行**（前提：严守下方文件所有权）

## ⚠️ 2026-07-29 更正：原设计的两个端点都不是 ExternalContact 数据

上一轮开发 agent 正确执行到 `BLOCKED_NEEDS_HUMAN`：核实后发现 `GET /api/contacts`（`listing_service.list_contacts()`）返回的是**归档参与者**（从消息里出现过的 wecom_userid 推导，只有 `contact_id`/`display_name`/`raw_id` 三个字段），`GET /api/search/contacts` 查询的是内部 `Contact` + `AdminUser`（员工）——**两者都不触碰 `ExternalContact` 表**，企业/标签/归属员工这些字段无从获得。

**RND-288（A4-2 外部联系人列表/筛选 API）已交付 `GET /api/admin/external-contacts`**，专门服务本页面。**本票现在对接这个端点，不再是 `/api/contacts` + `/api/search/contacts`。** 开工前先读 RND-288 的 QA Summary，确认其最终响应字段名（可能与本提示词假设的名字有细微差异）。

## 背景与项目现状

| 端点 | 位置 | 用途 |
|---|---|---|
| `GET /api/admin/external-contacts` | `backend/app/routers/external_contacts.py`（RND-288 交付） | 外部联系人列表 + 筛选（企业/标签/归属员工）+ 分页 |

`ExternalContact` 实体 + WeCom 同步由 RND-287（A4-1）完成；RND-288 在此基础上补齐了列表/筛选 API。
设计稿：`design/Crowntime WeCom Archive Design System/pages/contacts.html`。

**⚠️ 后端能力边界（务必先看，避免做不出来）：**
- A4-3「详情时间线」（RND-289）**仍在 Backlog，尚未实现**。
- 因此本票**只做列表页**。**`contact-detail` 详情页不在本票范围**（属 R3）。
- 若设计稿里的某个筛选维度当前 RND-288 端点不支持 → 降级为不实现该维度并在 QA Summary 中记录，**不要改后端补 API**（那还是 RND-288 或新票的范围）。

**RND-326 已为你准备好（不要重做，也不要修改）：**
- `design-system.css`、`sidenav.py` 的 `render_sidenav(active_id, registered_paths)`（`active_id="contacts"`）。
- `backend/app/routers/admin_contacts_page.py` —— 空 stub router，已在 `main.py` 注册。
- i18n 锚点：`/* RND-330 contacts page keys — insert below */`（3 个 locale 各一处）。

**❗ 本项目高频踩坑（必读）：**
- **没有模板引擎。** `render_template` 只做 `__TOKEN__` 单遍替换，**不存在 Jinja**。禁止 `{% %}` / `{{ }}`。未提供的 token 抛 `KeyError` → 500。
- **i18n 三语**：`zh-CN` / `zh-TW` / `en`，缺一即 FAIL。
- **架构边界硬闸**：router 不得 import `app.main`；不要动 `main.py`。
- **架构冻结 D1**：SSR + 原生 JS，禁 React / Vue / 构建步骤。
- **注意 `contacts` 的语义歧义**：仓库里既有 `Contact`（**内部员工**）又有 `ExternalContact`（**外部联系人**）。本页面面向**外部联系人**，对接的是 `GET /api/admin/external-contacts`（RND-288），**不是** `GET /api/contacts`（那是归档参与者，语义完全不同，已被上一轮排除）。

## 目标（Goal）
让管理员在生产环境用 `/admin/contacts` 页面浏览与搜索外部联系人，并使侧栏「外部联系人」从灰色占位变为可点击。

## 范围边界

**In scope：**
1. `backend/app/web/templates/contacts.html` —— 列表页：昵称、所属企业、标签、归属员工；搜索 / 筛选。
2. `backend/app/routers/admin_contacts_page.py` —— 注册 `GET /admin/contacts`，渲染模板，传 `sidenav=render_sidenav("contacts", <已注册路径集合>)`。
3. i18n：在**自己的锚点正下方**新增 `contacts.*` 键（3 个 locale）。
4. `backend/tests/test_contacts_page.py`。

**Out of scope（显式非目标）：**
- **不实现 `contact-detail` 详情页 / 时间线**（依赖 RND-289，属 R3）。列表项不得链接到不存在的详情路由。
- 不改任何后端 API（含 RND-288 交付的端点）；不实现 A4-3。
- 不改现有内部 `Contact` 表或 `ExternalContact` 模型。
- 不碰 `sidenav.py`、不改 `main.py`。
- 不碰其他三张页面票的文件或 i18n 锚点区间。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/contacts.html`（新）
- `backend/app/routers/admin_contacts_page.py`
- `backend/app/assets/i18n.js` —— **仅限 `/* RND-330 contacts page keys */` 锚点正下方**
- `backend/tests/test_contacts_page.py`（新）

**⚠️ 强制随附改动（2026-07-30 追加授权）：本票新增 1 个路由，因此 `backend/tests/test_http_contract.py` 也属于本票拥有清单** —— 必须同步 ① 第 326 行 `route_count`（**先跑 `make verify` 读当前真实基线 N，改成 N+1，禁止写死数字**）② expected path 集合追加本票新路由 ③ snapshot 列表追加对应条目。**不得**改他票条目。
> 本票的页面路由用 `require_html_session`（不是 `require_role`），因此**不触发** `test_rnd280_rbac_scaffold.py` 的 RBAC 白名单，那个文件不用改。
> 契约测试同步不是独立工单——拆开会让 `main` 在两票之间红灯。详见 `docs/ticket-autopilot-workflow.md` §3.3。

**只读、绝不可写：** `sidenav.py`、`main.py`、`design-system.css`、`routers/external_contacts.py`、`routers/conversations.py`、`routers/search.py`、其他 `admin_*_page.py`、其他锚点区间。

> 若发现必须改他人文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）
- **AC-1 路由可用**：`GET /admin/contacts` 返回 200，HTML 中无残留 `__TOKEN__`。
- **AC-2 真实数据渲染**：列表由 `GET /api/admin/external-contacts`（RND-288）真实响应填充（非 mock），含昵称/企业/标签/归属员工。
- **AC-3 筛选可用**：按企业/标签/归属员工筛选，走 RND-288 提供的筛选参数（**不得**前端全量拉取后再过滤）。
- **AC-4 不悬挂详情链接**：列表项**不得**链接到 `/admin/contacts/{id}` 等尚未实现的详情路由（会 404）。测试须断言页面无指向未注册路由的链接。
- **AC-5 导航自动点亮**：`/admin/contacts` 注册后侧栏「外部联系人」渲染为 `<a href>`，**且 `sidenav.py` 未被修改**。
- **AC-6 i18n 三语齐全**：新增每个 `contacts.*` 键在三个 locale 中均存在，且位于 RND-330 锚点下方。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_contacts_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/external_contacts.py   # 必须无输出
grep -rn '{%\|{{' backend/app/web/templates/contacts.html           # 必须无输出
```

## 依赖（Dependencies）
**阻塞于 RND-326（设计系统落地）与 RND-288（外部联系人列表 API）。** 任一产物缺失 → `BLOCKED_NEEDS_HUMAN`，不要自己拼凑数据源或直接查 DB。
RND-289（A4-3 详情时间线）**未完成且不阻塞本票**，但限定了本票只能做列表，不能做详情跳转。

## 完成定义
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票 4 个拥有文件
- [ ] QA Summary 已产出（含「设计稿中因 API 未就绪而未实现的筛选维度」清单，若有）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：外部联系人是真实客户 PII —— 测试**不得**引入真实姓名 / 手机号 / 企业名，一律固定假数据。
- 风险：RND-288 最终响应字段名与本提示词假设的不完全一致 —— 开工前先读其 QA Summary 确认真实字段名，不要凭本提示词的名字硬编码后再报错。
- 回滚：新增文件为主，`git checkout -- <files>`。

## 人工点位
- **Trigger**：RND-326 与 RND-288 均完成后置 In Progress。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：RND-288 端点字段仍不足以支撑设计稿所需字段 → `BLOCKED_NEEDS_HUMAN`，**不要改后端**（那还是 RND-288 或新票的范围）。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`routers/external_contacts.py`（RND-288 交付，确认真实响应字段）、`sidenav.py`（只读）、设计稿 `pages/contacts.html`。
2. 在 `admin_contacts_page.py` 注册 `GET /admin/contacts`。
3. 写 `contacts.html`，`__TOKEN__` 占位，原生 JS 调 `GET /api/admin/external-contacts`。
4. i18n 锚点下加 `contacts.*` 键 ×3 locale。
5. 写 `tests/test_contacts_page.py` 覆盖 AC-1~AC-6（AC-4 要显式断言无悬挂详情链接）。
6. `make verify` → QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试数据必须是假数据。
- 不扩大 Scope；最小正确改动优先。
- 证据优先，以 exit 0 / 测试通过为证。
