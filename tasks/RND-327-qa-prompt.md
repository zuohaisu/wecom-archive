[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-327 的 6 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-327 验收提示词（Acceptance / QA Prompt）

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-327「R1-1 users 页面」｜风险等级 R1｜automated 验收
- 波次：R1（与 RND-328/329/330 并行）。**文件所有权越界是本波次最危险的失败模式 —— 必查第 5 项。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令（`make verify`、`pytest`、`grep`、`git diff`、`git status`）。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）
每条必须给出证据（测试名 / `file:line` / exit code）。

### AC-1 — 路由可用
- 证据：测试断言 `GET /admin/users` 返回 200；响应 HTML 中 `grep '__[A-Z0-9_]*__'` 无残留 token。
- 背景：`render_template` 对未提供的 token 抛 `KeyError` → 500。
- 判定：200 且无残留 token = PASS。

### AC-2 — 真实数据渲染
- 证据：页面数据来自 `GET /api/admin/users`，**非 mock、非硬编码假数据**。确认渲染字段含角色 / 状态 / 最后活跃 / 近 30 天消息数。
- 判定：真实端点驱动 = PASS。若模板里写死假用户列表 = FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-3 — 三个操作可用
- 证据：邀请（`POST /api/admin/users/invite`）、启停（`PATCH /api/admin/users/{id}`）、重置密码（`POST /api/admin/users/{id}/reset-password`）三条路径均有测试覆盖并能从页面触发。
- 判定：三个操作各有用例且通过 = PASS。缺任一 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-4 — 导航自动点亮且未碰 sidenav（**关键，决定并行是否被破坏**）
- 证据：`git diff --stat -- backend/app/web/sidenav.py` **必须无输出**；同时有测试证明路由注册后该导航项渲染为 `<a href="/admin/users">`。
- 判定：sidenav 零改动 + 自动点亮生效 = PASS。**若 `sidenav.py` 被修改 → 直接 FAIL（`SCOPE_VIOLATION`, severity: blocker）**，因为这会与其他三张并行票冲突。

### AC-5 — i18n 三语齐全且在自己锚点下
- 证据：对每个新增 `users.*` 键，`grep -c '"<key>"' backend/app/assets/i18n.js` **应为 3**；且这些键位于 `/* RND-327 users page keys */` 锚点下方，**未侵入 RND-328/329/330 的锚点区间**。
- 判定：三语齐全 + 锚点正确 = PASS。少于 3 = FAIL（`I18N_INCOMPLETE`）；写进他人锚点区间 = FAIL（`SCOPE_VIOLATION`）。

### AC-6 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **模板引擎误用**：`grep -rn '{%\|{{' backend/app/web/templates/users.html` —— 本项目**无 Jinja**，出现即 FAIL（`IMPLEMENTATION_DEFECT`）。
2. **架构边界**：`grep -n 'from app.main\|import app.main' backend/app/routers/admin_users_page.py` 应无输出。
3. **架构冻结 D1**：diff 中不得出现 React / Vue / 打包器 / SPA 路由 → 出现即 FAIL（`SCOPE_VIOLATION`）。
4. **未改后端 API**：`git diff --stat -- backend/app/routers/users.py backend/app/routers/auth.py` **必须无输出**（本票明确不改后端）。
5. **文件所有权（最危险项）**：`git status --porcelain` 中改动文件必须**只有**这 4 个：
   - `backend/app/web/templates/users.html`
   - `backend/app/routers/admin_users_page.py`
   - `backend/app/assets/i18n.js`
   - `backend/tests/test_users_page.py`
   出现任何清单外文件（尤其 `sidenav.py` / `main.py` / 其他 `admin_*_page.py`）→ FAIL（`SCOPE_VIOLATION`, blocker）。

## 附加检查（Security）
- 邀请 / 重置密码涉及凭据：确认**无**密码、token、邀请码明文写入模板、JS、日志或测试固定值 → 否则 FAIL（`SECURITY_VIOLATION`）。
- 无真实用户数据 / 真实域名进入测试。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL（`SECURITY_VIOLATION`）。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_users_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/users.py backend/app/routers/auth.py   # 必须全无输出
grep -rn '{%\|{{' backend/app/web/templates/users.html   # 必须无输出
git status --porcelain
git log origin/main..HEAD                                 # 必须无输出
```

## 产出
写入 `tasks/RND-327-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
PASS / FAIL(`FIXING`) / BLOCKED(`BLOCKED_NEEDS_HUMAN`) 三态判定规则见模板。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- 任一 AC 无对应测试用例 → 直接 FAIL（`INSUFFICIENT_TEST_COVERAGE`），不接受「手工验证过了」。
