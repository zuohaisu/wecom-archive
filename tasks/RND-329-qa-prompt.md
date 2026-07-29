[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-329 的 7 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-329 验收提示词（Acceptance / QA Prompt）

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
- 工单：RND-329「R1-3 media 页面」｜风险等级 R1｜automated 验收
- 波次：R1（与 RND-327/328/330 并行）。**文件所有权越界必查第 5 项。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 路由可用
- 证据：测试断言 `GET /admin/media` 返回 200；响应 HTML 无残留 `__TOKEN__`。
- 判定：200 且无残留 token = PASS。

### AC-2 — 真实数据渲染
- 证据：数据来自 `GET /api/admin/media`（`backend/app/routers/media_library.py:42`），非 mock、非硬编码。含类型 / 大小 / 上传时间 / 所属会话。
- 判定：真实端点驱动 = PASS。

### AC-3 — 筛选走后端参数
- 证据：类型与时间范围筛选有测试覆盖，且请求携带后端筛选参数。
- 反模式检查：**不得**前端全量拉取后再过滤（媒体表可能极大）。
- 判定：后端筛选 = PASS；前端全量过滤 = FAIL（`IMPLEMENTATION_DEFECT`）。

### AC-4 — 缩略图经既有媒体路由，无七牛直连（**重点**）
- 证据：`grep -rniE 'qiniu|QINIU_|\.clouddn\.com' backend/app/web/templates/media.html` **必须无输出**；预览 / 缩略图 URL 指向既有媒体访问路由（`backend/app/routers/media.py` 提供）。
- 背景：媒体存储是双 provider（`local` / `qiniu_kodo`）。前端直连七牛意味着要么暴露签名逻辑，要么绕过访问控制 —— 两者都不可接受。
- 判定：无直连痕迹 + 走既有路由 = PASS。发现直连或任何签名密钥出现在前端 → FAIL（`SECURITY_VIOLATION`, severity: blocker）。

### AC-5 — 导航自动点亮且未碰 sidenav（**关键**）
- 证据：`git diff --stat -- backend/app/web/sidenav.py` **必须无输出**；有测试证明路由注册后导航项渲染为 `<a href="/admin/media">`。
- 判定：sidenav 零改动 + 自动点亮 = PASS。**sidenav.py 被改 → 直接 FAIL（blocker）。**

### AC-6 — i18n 三语齐全且在自己锚点下
- 证据：每个新增 `media.*` 键 `grep -c` **应为 3**；位于 `/* RND-329 media page keys */` 锚点下方，未侵入他人锚点区间。
- 判定：三语齐全 + 锚点正确 = PASS。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；8 个已上线页面无破版。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **模板引擎误用**：`grep -rn '{%\|{{' backend/app/web/templates/media.html` —— 无 Jinja，出现即 FAIL。
2. **架构边界**：`grep -n 'from app.main\|import app.main' backend/app/routers/admin_media_page.py` 应无输出。
3. **架构冻结 D1**：无 React / Vue / 打包器 / SPA 路由。
4. **未改后端与存储层**：`git diff --stat -- backend/app/routers/media.py backend/app/routers/media_library.py backend/app/media_storage.py backend/app/qiniu_storage.py` **必须全无输出**。
5. **未越界做 RND-292**：本票明确**不实现下载端点与下载审计钩子**（那是 RND-292）。若 diff 里出现新增下载端点或 `AuditLog` 写入 → FAIL（`SCOPE_VIOLATION`）。
6. **文件所有权（最危险项）**：`git status --porcelain` 改动文件必须**只有**：
   - `backend/app/web/templates/media.html`
   - `backend/app/routers/admin_media_page.py`
   - `backend/app/assets/i18n.js`
   - `backend/tests/test_media_page.py`
   出现清单外文件 → FAIL（`SCOPE_VIOLATION`, blocker）。

## 附加检查（Security）
- 媒体是真实归档内容：确认测试中**无**真实聊天媒体、真实用户数据、生产 URL → 否则 FAIL（`SECURITY_VIOLATION`）。
- 确认无 `QINIU_ACCESS_KEY` / `QINIU_SECRET_KEY` 等凭证出现在模板、JS 或测试中 → 出现即 FAIL（`SECURITY_VIOLATION`, blocker）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_page.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff --stat -- backend/app/web/sidenav.py backend/app/main.py backend/app/routers/media.py backend/app/routers/media_library.py   # 必须全无输出
grep -rn '{%\|{{' backend/app/web/templates/media.html            # 必须无输出
grep -rniE 'qiniu|QINIU_|clouddn' backend/app/web/templates/media.html   # 必须无输出（AC-4）
git status --porcelain
git log origin/main..HEAD                                          # 必须无输出
```

## 产出
写入 `tasks/RND-329-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- AC-4 若无「无七牛直连」的显式检查证据 → 直接 FAIL（`INSUFFICIENT_TEST_COVERAGE`）。
