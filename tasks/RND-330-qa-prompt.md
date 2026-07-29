[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-330 的 7 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-330 验收提示词（Acceptance / QA Prompt）

---

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始下面的验收步骤，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发文档规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：确认工单号，然后直接进入验收核对步骤。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-330「R1-4 contacts 页面」｜风险等级 R1｜automated 验收
- 波次：R1（与 RND-327/328/329 并行）。**文件所有权越界必查第 6 项。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 路由可用
- 证据：测试断言 `GET /admin/contacts` 返回 200；响应 HTML 无残留 `__TOKEN__`。
- 判定：200 且无残留 token = PASS。

### AC-2 — 真实数据渲染（**先查语义**）
- 证据：数据来自 `GET /api/admin/external-contacts`（RND-288 交付，`backend/app/routers/external_contacts.py`），非 mock，含企业/标签/归属员工字段。
- **语义核查（本票特有陷阱，2026-07-29 已在 RND-288 层解决，但仍需复查前端有没有走错端点）**：仓库里 `Contact` 是**内部员工**，`ExternalContact` 才是**外部联系人**；`GET /api/contacts` 是归档参与者，两者都**不是**本票该用的数据源。确认页面调用的是 `GET /api/admin/external-contacts`，不是 `GET /api/contacts` 或 `GET /api/search/contacts`。
- 判定：真实调用 RND-288 端点 **且** 展示语义与「外部联系人」一致 = PASS。若仍在用 `GET /api/contacts`/`GET /api/search/contacts`（说明没读 2026-07-29 更正）→ FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-3 — 筛选可用
- 证据：企业/标签/归属员工筛选走 RND-288 端点的筛选参数（**不是**前端全量拉取后再过滤），有测试覆盖。
- 判定：筛选走后端参数且有用例 = PASS。前端全量过滤 = FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-4 — 不悬挂详情链接（**重点**）
- 证据：列表项**不得**链接到 `/admin/contacts/{id}` 等未注册路由；测试显式断言页面无指向未注册路由的链接。
- 背景：A4-3 详情时间线（RND-289）仍在 Backlog，详情页属 R3。若列表项链到详情，用户点击必然 404。
- 判定：无悬挂链接 + 有显式断言 = PASS。存在指向未实现路由的链接 → FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-5 — 导航自动点亮且未碰 sidenav（**关键**）
- 证据：`git diff --stat -- backend/app/web/sidenav.py` **必须无输出**；有测试证明路由注册后导航项渲染为 `<a href="/admin/contacts">`。
- 判定：sidenav 零改动 + 自动点亮 = PASS。**sidenav.py 被改 → 直接 FAIL（blocker）。**

### AC-6 — i18n 三语齐全且在自己锚点下
- 证据：每个新增 `contacts.*` 键 `grep -c` **应为 3**；位于 `/* RND-330 contacts page keys */` 锚点下方，未侵入他人锚点区间。
- 判定：三语齐全 + 锚点正确 = PASS。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **模板引擎误用**：`grep -rn '{%\|{{' backend/app/web/templates/contacts.html` —— 无 Jinja，出现即 FAIL。
2. **架构边界**：`grep -n 'from app.main\|import app.main' backend/app/routers/admin_contacts_page.py` 应无输出。
3. **架构冻结 D1**：无 React / Vue / 打包器 / SPA 路由。
4. **未改后端 API**：`git diff --stat -- backend/app/routers/external_contacts.py backend/app/routers/conversations.py backend/app/routers/search.py backend/app/db/models.py` **必须全无输出**（RND-288 已交付的端点不应被本票改动）。
5. **未越界做 A4-3**：本票明确只做列表。若 diff 里出现详情时间线实现 → FAIL（`SCOPE_VIOLATION`）。
6. **文件所有权（最危险项）**：`git status --porcelain` 改动文件必须**只有**：
   - `backend/app/web/templates/contacts.html`
   - `backend/app/routers/admin_contacts_page.py`
   - `backend/app/assets/i18n.js`
   - `backend/tests/test_contacts_page.py`
   出现清单外文件 → FAIL（`SCOPE_VIOLATION`, blocker）。

## 附加检查（Security）
- 外部联系人是**真实客户 PII**：确认测试中**无**真实姓名 / 手机号 / 邮箱 / 企业名 / 真实域名，一律固定假数据 → 否则 FAIL（`SECURITY_VIOLATION`, severity: blocker）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_contacts_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/external_contacts.py backend/app/routers/conversations.py backend/app/routers/search.py   # 必须全无输出
grep -rn '{%\|{{' backend/app/web/templates/contacts.html   # 必须无输出
git status --porcelain
git log origin/main..HEAD                                    # 必须无输出
```

## 产出
写入 `tasks/RND-330-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
若开发 agent 因 API 未就绪而未实现设计稿中的某些筛选维度，且已在 QA Summary 中记录 —— 这是**允许的降级**，不判 FAIL，但须在 `notes` 中列出未实现清单。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- AC-4 若无「无悬挂详情链接」的显式断言测试 → 直接 FAIL（`INSUFFICIENT_TEST_COVERAGE`）。
