[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-292 的 6 条 AC 并产出带证据的 PASS/FAIL 判定。

# RND-292 验收提示词（Acceptance / QA Prompt）

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-292「A6-2 下载端点 + 审计钩子」｜风险等级 R1｜automated 验收
- **本票同时触及访问控制与审计留痕，是合规承诺的落点。AC-2/3/4/5 从严判定。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、改工单、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 下载可用
- 证据：测试断言下载端点返回字节流，含正确 `Content-Type` 与 `Content-Disposition: attachment`；`local` 与 `qiniu_kodo` 两种 provider 均有用例（七牛可 mock SDK 边界，参照既有媒体测试做法）。
- 判定：两种 provider 均有用例且通过 = PASS。只覆盖一种 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-2 — 每次下载写审计（**重点**）
- 证据：测试断言一次成功下载后 `audit_logs` **恰好新增一条**记录，且字段含操作人 / 时间 / 动作类型 / 目标媒体标识 / 租户。
- 代码审阅：确认审计写入在**成功路径上必然执行**，不是可选分支、不是 best-effort 吞异常（`grep` 是否有 `try: audit... except: pass`）。
- 判定：恰好一条 + 字段齐全 + 必然执行 = PASS。审计写入被 try/except 静默吞掉 → FAIL（`IMPLEMENTATION_DEFECT`, blocker）。

### AC-3 — 失败不误记（**重点**）
- 证据：构造下载失败（媒体不存在 / 无权限）用例，断言**未产生「下载成功」审计记录**。
- 背景：把失败记成成功，比没有审计更糟 —— 审计记录一旦不可信，整条合规证据链失效。
- 判定：有失败路径用例且断言无成功记录 = PASS。缺此用例 = FAIL（`INSUFFICIENT_TEST_COVERAGE`）。

### AC-4 — 鉴权与租户隔离（**重点**）
- 证据：未认证请求被拒（有用例）；**A 租户下载 B 租户媒体被拒**（有用例）。
- 判定：两个用例齐全且通过 = PASS。缺跨租户用例 → FAIL（`INSUFFICIENT_TEST_COVERAGE`, blocker）—— 这是数据泄露防线。

### AC-5 — 无凭证泄露
- 证据：`grep -rniE 'QINIU_ACCESS_KEY|QINIU_SECRET_KEY|clouddn' backend/app/routers/media.py` 及响应构造逻辑，确认无长期凭证进入响应体 / 响应头 / 日志；确认未返回可绕过访问控制的对象存储直链。
- 判定：无泄露 = PASS。发现凭证或绕过直链 → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-6 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过；既有媒体测试全绿（`pytest -k media`）。
- 判定：全部 exit 0 = PASS。既有媒体端点行为被改变 = FAIL（`REGRESSION`）。

## 本项目专属检查（必查）
1. **架构边界**：`grep -n 'from app.main\|import app.main' backend/app/routers/media.py` 应无输出；service 层未 import `app.routers.*`。
2. **未改审计基础设施**：`git diff --stat -- backend/app/db/models.py backend/app/routers/audit.py` **必须无输出**。`AuditLog` 已就绪，本票只调用不修改。若改了 `models.py` 且无对应迁移 → `alembic check` 会红 = FAIL。
3. **未改存储层**：`git diff --stat -- backend/app/media_storage.py backend/app/qiniu_storage.py` **必须无输出**。
4. **未越界做 UI**：本票**不含**媒体库页面（那是 RND-329）。若 diff 出现 `templates/media.html` 或 `admin_media_page.py` → FAIL（`SCOPE_VIOLATION`）。
5. **未碰 R1 页面票文件**：`git diff --stat -- backend/app/web/ backend/app/assets/i18n.js` **必须无输出**。
6. **文件所有权**：`git status --porcelain` 改动文件应限于 `routers/media.py`、可选的新 service、`tests/test_media_download_audit.py`。出现清单外文件 → FAIL（`SCOPE_VIOLATION`）。

## 附加检查（Security）
- 测试中**无**真实归档媒体、真实用户数据、生产 URL → 否则 FAIL（`SECURITY_VIOLATION`）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_media_download_audit.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
.venv/bin/python -m pytest backend/tests/ -k "media" -q       # 既有媒体测试无回归
git diff --stat -- backend/app/db/models.py backend/app/media_storage.py backend/app/qiniu_storage.py backend/app/web/ backend/app/assets/i18n.js   # 必须全无输出
grep -rniE 'QINIU_ACCESS_KEY|QINIU_SECRET_KEY|clouddn' backend/app/routers/media.py   # 必须无输出
git status --porcelain
git log origin/main..HEAD                                      # 必须无输出
```

## 产出
写入 `tasks/RND-292-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- AC-3（失败不误记）与 AC-4（跨租户拒绝）若无对应用例 → 直接 FAIL。这两条分别是审计可信度与数据隔离的唯一防线，不接受「手工验证过了」。
