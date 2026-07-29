[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-330 的 7 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-330 验收提示词（Acceptance / QA Prompt）

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
- 证据：数据来自 `GET /api/contacts`（`backend/app/routers/conversations.py:325`），非 mock。
- **语义核查（本票特有陷阱）**：仓库里 `Contact` 是**内部员工**（`wecom_userid/name/tenant_id`），`ExternalContact` 才是**外部联系人**。本页面语义是「外部联系人」。请核实页面实际展示的是哪一种。
- 判定：真实端点驱动 **且** 展示语义与「外部联系人」一致 = PASS。若做成了员工列表 → FAIL（`IMPLEMENTATION_DEFECT`），或若开发 agent 已按要求上报 `BLOCKED_NEEDS_HUMAN` 则判 BLOCKED（这是正确行为，不算失败）。

### AC-3 — 搜索可用
- 证据：搜索走 `GET /api/search/contacts`（`backend/app/routers/search.py:177`），有测试覆盖关键词过滤。
- 判定：搜索端点驱动且有用例 = PASS。

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
4. **未改后端 API**：`git diff --stat -- backend/app/routers/conversations.py backend/app/routers/search.py backend/app/db/models.py` **必须全无输出**。
5. **未越界做 A4-2 / A4-3**：本票明确只做列表。若 diff 里出现新增 `/api/admin/external-contacts` 端点或详情时间线实现 → FAIL（`SCOPE_VIOLATION`）。
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
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/conversations.py backend/app/routers/search.py   # 必须全无输出
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
