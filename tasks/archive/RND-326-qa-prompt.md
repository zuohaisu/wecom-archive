[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-326 的 7 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-326 验收提示词（Acceptance / QA Prompt）

---

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始下面的验收步骤，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发文档规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：确认工单号，然后直接进入验收核对步骤。

---

> 交给**独立验收 agent**（默认 Codex）。你独立于开发 agent，只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-326「R1-0 设计系统落地」｜风险等级 R1｜automated 验收
- 本票是 R1 波次的前置阻塞项：**它若被误判为 PASS，后续 4 张页面票会在错误地基上并行开发，代价成倍放大。从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令（`make verify`、`pytest`、`grep`、`git diff`、`git status`）。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → 输出 FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）
每条必须给出证据（测试名 / `file:line` / exit code）。

### AC-1 — 设计系统 CSS 落地且未覆盖现存文件（**最高风险项，重点查**）
- 证据：`backend/app/web/static/design-system.css` 存在且内容源自设计系统；**且** `git diff --stat -- backend/app/web/static/styles.css` **无输出**。
- 背景：现存 `static/styles.css` 服务 `login`/`forgot_password`/`reset_password`/`settings` 四个已上线页面。若被设计系统同名文件覆盖 = 四页破版。
- 判定：新文件存在 + 现存 `styles.css` 零改动 = PASS。**若 `styles.css` 有任何改动 → 直接 FAIL（`type: REGRESSION`, severity: blocker）。**

### AC-2 — 单一导航配置
- 证据：`sidenav.py` 中导航项只定义一处；`render_sidenav()` 返回 str；`grep -n 'side-nav-item' backend/app/web/templates/review_console.html` 应查不到手写导航项列表（应已被 `__SIDENAV__` 取代）。
- 判定：配置单点 + 模板改用 token = PASS。若导航项仍在模板里硬编码 = FAIL。

### AC-3 — 审计日志入口存在
- 证据：`sidenav.py` 含 `audit-log` 项且指向 `/admin/audit-logs`；`grep -c '"nav.auditLog"' backend/app/assets/i18n.js` **应为 3**。
- 判定：配置项存在 + 三语齐全 = PASS。少于 3 = FAIL（`type: I18N_INCOMPLETE`）。

### AC-4 — 按路由自动点亮（**决定并行能否成立，重点查**）
- 证据：必须存在**正反两组**测试 —— 同一份导航配置，`registered_paths` 不含某路径时渲染为 disabled 占位；含该路径时渲染为 `<a href>`，**且中间未修改 `sidenav.py`**。
- 为什么重点：这条成立，RND-327~330 才不需要碰 `sidenav.py`；不成立则 4 张票会争抢同一文件，整个并行方案失效。
- 判定：正反用例齐全且通过 = PASS。只有单向用例 = FAIL（`type: INSUFFICIENT_TEST_COVERAGE`）。

### AC-5 — i18n 锚点就位
- 证据：`grep -c 'RND-327 users page keys\|RND-328 audit-log page keys\|RND-329 media page keys\|RND-330 contacts page keys' backend/app/assets/i18n.js` **应为 12**（4 个锚点 × 3 个 locale）。
- 另需确认现有 i18n 键无丢失：`git diff backend/app/assets/i18n.js` 应只见新增，不见删除（`-` 行只应出现在无关的格式调整上，且不得删除任何 `"key":` 行）。
- 判定：12 行锚点 + 零键丢失 = PASS。

### AC-6 — 无破版回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；8 个已上线页面路由返回 200 且响应 HTML 中**无残留 `__TOKEN__` 字面量**（`render_template` 对未提供 token 会抛 `KeyError` → 500，务必确认 `review_console` 所有渲染入口都传了 `sidenav=`）。
- 判定：全部 exit 0 = PASS。

### AC-7（2026-07-29 追加）— 生产路由与测试 shim 均已接入 sidenav，且改动范围最小
- 证据：`GET /admin/conversations`（经 `backend/app/routers/web.py` 真实路由，不是直接调用 `render_template`）返回 200；`backend/tests/_rnd216_web_shims.py` 相关既有测试全绿。
- **改动范围核查（本条重点）**：`git diff -- backend/app/routers/web.py backend/tests/_rnd216_web_shims.py` 逐行看，**每个文件应只新增一个 `sidenav=render_sidenav(...)` 关键字参数**，不得有其他改动（`web.py` 里其他路由函数、`_rnd216_web_shims.py` 里 `settings`/`diagnostics` 等其他函数必须逐字节未变）。
- 判定：两处均已接入 + diff 范围仅限一行新增参数 = PASS。若 diff 超出这一行（哪怕是重构、加注释）→ FAIL（`type: SCOPE_VIOLATION`）。若两处任一未接入（仍会 `KeyError`）→ FAIL（`type: IMPLEMENTATION_DEFECT`, severity: blocker）。

## 本项目专属检查（必查）
1. **模板引擎误用**：`grep -rn '{%\|{{' backend/app/web/templates/` —— 本项目**没有** Jinja。出现即 FAIL（`type: IMPLEMENTATION_DEFECT`），Jinja 标签会被原样吐给浏览器。
2. **架构边界**：`grep -n 'import.*app\.main\|from app\.main' backend/app/web/sidenav.py` **应无输出**。`sidenav.py` 若 import `app.main` 会触发架构硬闸。
3. **架构冻结 D1**：diff 中不得出现 React / Vue / 打包器 / SPA 路由假设 → 出现即 FAIL（`type: SCOPE_VIOLATION`）。
4. **Out of scope 越界**：本票**明确不迁移** `review_console` / `search` 的内联 `<style>`。若 diff 里大规模重写了这两个页面的内联样式 → FAIL（`type: SCOPE_VIOLATION`）。同样，`base.css` 不得被改。
5. **文件所有权**：`git status --porcelain` 中的改动文件必须全部落在本票拥有清单内：`design-system.css`、`sidenav.py`、`review_console.html`、4 个 `admin_*_page.py`、`main.py`、`i18n.js`、`test_sidenav.py`、**`web.py`、`_rnd216_web_shims.py`（2026-07-29 追加授权，见 AC-7，仅限一行 `sidenav=` 新增）**。出现清单外文件 → FAIL（`type: SCOPE_VIOLATION`），这会破坏并行波次。

## 附加检查（Security）
- 无凭证 / 密钥 / 真实域名 / 真实用户数据写入代码或测试 → 否则 FAIL（`SECURITY_VIOLATION`）。
- `git log origin/main..HEAD` **应为空**（agent 不得 commit）→ 有输出即 FAIL（`SECURITY_VIOLATION`）。
- 未改 CI/CD、`.gitignore`、部署配置。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_sidenav.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/static/styles.css          # 必须无输出
grep -c '"nav.auditLog"' backend/app/assets/i18n.js            # 必须为 3
grep -c 'RND-32[7-9].*page keys\|RND-330.*page keys' backend/app/assets/i18n.js   # 必须为 12
grep -rn '{%\|{{' backend/app/web/templates/                   # 必须无输出
git status --porcelain
git log origin/main..HEAD                                      # 必须无输出
```

## 产出
写入 `tasks/RND-326-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
- 全 PASS 无 blocker/major → `verdict: PASS`
- 任一 FAIL 或有 blocker/major → `verdict: FAIL`，`recommended_next_state: FIXING`，开发 agent **只修 findings**
- 歧义 / 2 轮仍 FAIL → `verdict: BLOCKED`，`recommended_next_state: BLOCKED_NEEDS_HUMAN`

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- AC-4 若缺正反双向用例，直接 FAIL —— 不接受「手工看过了」。
