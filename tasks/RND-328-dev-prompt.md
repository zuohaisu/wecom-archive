[Goal check] This work advances 开发（Development） by 交付 /admin/audit-logs 页面模板与路由，对接已就绪的审计日志 API，并在侧栏新增审计日志入口。

# RND-328 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## 任务身份
- 工单：RND-328「R1-2 audit-log 页面：审计日志前端实现 + 导航新增入口」｜Linear team `Builder`
- 优先级：**Urgent**｜风险等级：**R1**｜波次：R1 · 前端快赢四页
- **本票是 4 张页面票里优先级最高的**：合规采购客户第一个索要的就是审计日志页，而当前它连导航入口都不存在。
- **可与 RND-327/329/330 并行**（前提：严守下方文件所有权）

## 背景与项目现状
后端 **100% 就绪，本票不需要改后端一行代码**（已实地核实）：

| 端点 | 位置 | 用途 |
|---|---|---|
| `GET /api/admin/audit-logs` | `backend/app/routers/audit.py:52` | 列表 / 筛选 / 分页（`AuditLogListOut`） |

`AuditLog` 表与写入钩子已由 RND-293/294 完成，只读追加、不可篡改。
设计稿：`design/Crowntime WeCom Archive Design System/pages/audit-log.html`。

**RND-326 已为你准备好（不要重做，也不要修改）：**
- `design-system.css`、`sidenav.py` 的 `render_sidenav(active_id, registered_paths)`（`active_id="audit-log"`）。
- `backend/app/routers/admin_audit_page.py` —— 空 stub router，已在 `main.py` 注册。
- **导航项本身已由 RND-326 建好**（`audit-log` → `/admin/audit-logs`，含 `nav.auditLog` 三语）。你注册路由后它会**自动**从灰变亮 —— 所以你**不需要**碰 `sidenav.py` 或加 `nav.*` 键。
- i18n 锚点：`/* RND-328 audit-log page keys — insert below */`（3 个 locale 各一处）。

**❗ 本项目高频踩坑（必读）：**
- **没有模板引擎。** `render_template` 只做 `__TOKEN__` 单遍替换，**不存在 Jinja**。禁止 `{% %}` / `{{ }}`。未提供的 token 会抛 `KeyError` → 500。
- **i18n 三语**：`zh-CN` / `zh-TW` / `en`，缺一即 FAIL。
- **架构边界硬闸**：router 不得 import `app.main`；不要动 `main.py`（已注册）。
- **架构冻结 D1**：SSR + 原生 JS，禁 React / Vue / 构建步骤。

## 目标（Goal）
让合规管理员在生产环境用 `/admin/audit-logs` 页面查看、筛选、分页浏览审计事件，并让审计日志首次出现在侧栏导航中。

## 范围边界

**In scope：**
1. `backend/app/web/templates/audit_log.html` —— 列表页：审计事件、操作人、时间、动作类型；筛选（操作人 / 时间 / 动作类型）；分页。
2. `backend/app/routers/admin_audit_page.py` —— 注册 `GET /admin/audit-logs`，渲染模板，传 `sidenav=render_sidenav("audit-log", <已注册路径集合>)`。
3. i18n：在**自己的锚点正下方**新增 `audit.*` 键（3 个 locale）。
4. `backend/tests/test_audit_page.py`。

**Out of scope（显式非目标）：**
- 不改任何后端 API（`GET /api/admin/audit-logs` 已就绪）。
- **不实现审计记录的编辑 / 删除 / 导出** —— 审计日志按设计是只读追加、不可篡改（RND-274）。页面上不得出现任何写操作入口。
- 不碰 `sidenav.py`、不加 `nav.*` 键（RND-326 已建好，会自动点亮）。
- 不改 `main.py`。
- 不碰其他三张页面票的文件或 i18n 锚点区间。

**本工单拥有的文件（只许写这些）：**
- `backend/app/web/templates/audit_log.html`（新）
- `backend/app/routers/admin_audit_page.py`
- `backend/app/assets/i18n.js` —— **仅限 `/* RND-328 audit-log page keys */` 锚点正下方**
- `backend/tests/test_audit_page.py`（新）

**只读、绝不可写：** `sidenav.py`、`main.py`、`design-system.css`、其他 `admin_*_page.py`、其他锚点区间、所有后端 API 文件。

> 若发现必须改他人文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）
- **AC-1 路由可用**：`GET /admin/audit-logs` 返回 200，HTML 中无残留 `__TOKEN__`。
- **AC-2 真实数据渲染**：列表由 `GET /api/admin/audit-logs` 真实响应填充（非 mock），含操作人 / 时间 / 动作类型字段。
- **AC-3 筛选与分页可用**：按操作人、时间范围、动作类型筛选生效；分页可翻页，页码与后端返回的总数一致。
- **AC-4 只读语义**：页面**不提供**任何编辑 / 删除 / 修改审计记录的入口（审计不可篡改）。测试须断言页面无写操作端点调用。
- **AC-5 导航自动点亮**：`/admin/audit-logs` 注册后，侧栏「审计日志」渲染为 `<a href>`，**且 `sidenav.py` 未被修改**（`git diff --stat -- backend/app/web/sidenav.py` 无输出）。
- **AC-6 i18n 三语齐全**：新增每个 `audit.*` 键在三个 locale 中均存在，且均位于 RND-328 锚点下方。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。

## 验证方式
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_audit_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py   # 必须无输出
grep -rn '{%\|{{' backend/app/web/templates/audit_log.html          # 必须无输出
```

## 依赖（Dependencies）
**阻塞于 RND-326**。其产物缺失 → `BLOCKED_NEEDS_HUMAN`，不要自建替代品。

## 完成定义
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票 4 个拥有文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：审计日志含操作人身份与动作元数据，**不得**在页面或测试里落入真实用户数据 / 真实域名；测试用固定假数据。
- 风险：审计表可能数据量大 —— 分页必须走后端分页参数，**不得**一次性拉全表到前端再切片。
- 回滚：新增文件为主，`git checkout -- <files>`。

## 人工点位
- **Trigger**：RND-326 完成后置 In Progress。
- **Gate**：Haisu 审阅后批准 commit。
- **Escalation**：若 API 返回字段不足以支撑设计稿筛选维度 → `BLOCKED_NEEDS_HUMAN`，**不要改后端补字段**。

## 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`audit.py`、`sidenav.py`（只读）、设计稿 `pages/audit-log.html`。
2. 在 `admin_audit_page.py` 注册 `GET /admin/audit-logs`。
3. 写 `audit_log.html`，`__TOKEN__` 占位，原生 JS 调 API，分页走后端参数。
4. i18n 锚点下加 `audit.*` 键 ×3 locale。
5. 写 `tests/test_audit_page.py` 覆盖 AC-1~AC-6（AC-4 要显式断言无写操作入口）。
6. `make verify` → QA Summary + `git status`，**不 commit**。

## 硬性约束
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；凭证只从环境变量读。
- 不扩大 Scope；最小正确改动优先。
- 证据优先，以 exit 0 / 测试通过为证。
