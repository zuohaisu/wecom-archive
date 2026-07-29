[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-328 的 7 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-328 验收提示词（Acceptance / QA Prompt）

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-328「R1-2 audit-log 页面 + 导航新增入口」｜风险等级 R1｜automated 验收
- **这是合规采购客户第一个索要的页面，且审计日志按设计不可篡改 —— AC-4（只读语义）从严判定。**
- 波次：R1（与 RND-327/329/330 并行）。**文件所有权越界必查第 5 项。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 路由可用
- 证据：测试断言 `GET /admin/audit-logs` 返回 200；响应 HTML 无残留 `__TOKEN__`。
- 判定：200 且无残留 token = PASS。

### AC-2 — 真实数据渲染
- 证据：数据来自 `GET /api/admin/audit-logs`（`backend/app/routers/audit.py:52`），非 mock、非硬编码。含操作人 / 时间 / 动作类型。
- 判定：真实端点驱动 = PASS。

### AC-3 — 筛选与分页可用
- 证据：按操作人 / 时间范围 / 动作类型筛选有测试覆盖；分页走**后端分页参数**。
- 反模式检查：确认**不是**前端一次性拉全表再切片（审计表可能极大）。查 JS 中是否有无 limit/offset 的全量请求。
- 判定：三种筛选 + 后端分页 = PASS。前端全量切片 = FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-4 — 只读语义（**重点**）
- 证据：页面**不提供**任何编辑 / 删除 / 修改审计记录的入口；测试显式断言无写操作调用。
- 背景：`AuditLog` 按 RND-274 设计为只读追加、任何人（含超管）不可删改。页面若提供写入口，等于在合规产品上开了个合规漏洞。
- 判定：无写入口 + 有显式断言测试 = PASS。发现任何写操作 UI 或端点调用 → FAIL（`IMPLEMENTATION_DEFECT`, severity: blocker）。

### AC-5 — 导航自动点亮且未碰 sidenav（**关键**）
- 证据：`git diff --stat -- backend/app/web/sidenav.py` **必须无输出**；有测试证明路由注册后导航项渲染为 `<a href="/admin/audit-logs">`。
- 注意：`nav.auditLog` 键与导航项本身由 **RND-326** 提供，本票**不应**新增 `nav.*` 键。若 diff 里出现新增 `nav.*` 键 → FAIL（`SCOPE_VIOLATION`）。
- 判定：sidenav 零改动 + 自动点亮 = PASS。**sidenav.py 被改 → 直接 FAIL（blocker）。**

### AC-6 — i18n 三语齐全且在自己锚点下
- 证据：每个新增 `audit.*` 键 `grep -c` **应为 3**；位于 `/* RND-328 audit-log page keys */` 锚点下方，未侵入他人锚点区间。
- 判定：三语齐全 + 锚点正确 = PASS。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **模板引擎误用**：`grep -rn '{%\|{{' backend/app/web/templates/audit_log.html` —— 无 Jinja，出现即 FAIL。
2. **架构边界**：`grep -n 'from app.main\|import app.main' backend/app/routers/admin_audit_page.py` 应无输出。
3. **架构冻结 D1**：无 React / Vue / 打包器 / SPA 路由。
4. **未改后端 API**：`git diff --stat -- backend/app/routers/audit.py` **必须无输出**。
5. **文件所有权（最危险项）**：`git status --porcelain` 改动文件必须**只有**：
   - `backend/app/web/templates/audit_log.html`
   - `backend/app/routers/admin_audit_page.py`
   - `backend/app/assets/i18n.js`
   - `backend/tests/test_audit_page.py`
   出现清单外文件 → FAIL（`SCOPE_VIOLATION`, blocker）。

## 附加检查（Security）
- 审计数据含操作人身份：确认测试中**无**真实用户名 / 邮箱 / 真实域名，一律固定假数据 → 否则 FAIL（`SECURITY_VIOLATION`）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_audit_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/audit.py   # 必须全无输出
grep -rn '{%\|{{' backend/app/web/templates/audit_log.html   # 必须无输出
git status --porcelain
git log origin/main..HEAD                                     # 必须无输出
```

## 产出
写入 `tasks/RND-328-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- AC-4 若无「页面无写操作入口」的显式断言测试 → 直接 FAIL（`INSUFFICIENT_TEST_COVERAGE`）。
