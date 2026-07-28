# RND-285 验收提示词（独立 QA）— A3-2 邀请流程（令牌 + 邮件）

> 对应开发提示词：`rnd-285-execution-prompt.md`
> 线性任务：`RND-285` `[BLOCKED: F0] A3-2 邀请流程（令牌 + 邮件）`，父 Epic `RND-273`
> 工程纪律：Agent **绝不 git commit/push**；只验收、出报告，提交由用户决定。架构冻结 D1：SSR + 原生 JS。代码标识符加反引号。

---

## 0. 验收目标

独立验证 RND-285 的开发产出是否满足：邀请端点建 pending `AdminUser` + 发邮件、接受端点凭令牌激活账号、鉴权与租户隔离正确、邮件不泄露令牌、**且无任何不必要的 schema 变更**。逐条对照下方清单，给出 PASS / FAIL + 证据。

---

## 1. 验收清单（逐条打勾 `[x]` 并附证据）

### A. 范围守门（红线，先查）
- [ ] **无新增 Alembic migration**：`git status` / `ls backend/alembic/versions/` 无新文件；`grep -rn "revision = " backend/alembic/versions/0019*` 应为空。若开发擅自加了 `0019`/新表 → **FAIL（违反 §1.1）**。
- [ ] **未改 `models.py`**：`git diff -- backend/app/db/models.py` 应为空（`invite_token`/`invite_status`/`invited_by` 列是 RND-277 既有，不允许再动）。
- [ ] **未新增 router 文件 / 未改 `main.py`**：`git diff --stat` 不应出现新 `routers/*.py` 或 `main.py` 变更（端点应加在既有 `routers/auth.py`）。
- [ ] **未实现/修改鉴权逻辑**：`routers/auth.py` 不得新增 session/cookie/JWT 签发；只应「使用」`get_current_user`，不得自造。
- [ ] **无前端 HTML/SSR 页面新增**：本票后端票，不应出现 `onboarding-invite.html` 等前端产出（允许仅返回 accept 链接字符串）。

### B. 端点与签名
- [ ] `POST /api/admin/users/invite` 存在，签名含 `Depends(get_current_user)`，返回 `{"ok": True}`。
- [ ] `POST /api/admin/users/accept` 存在，**不挂** `get_current_user`（公开端点），返回 `{"ok": True}`。
- [ ] `invite` 请求体含 `role`（必填）与可选 `wecom_user_id`/`email`/`name`；`accept` 含 `token`/`password`/`name?`。

### C. 邀请逻辑正确性
- [ ] 调 `invite_user` 后，DB 新增一条 `AdminUser`：`status == "disabled"`、`invite_status == "pending"`、`invite_token` 非空、`invited_by == 发起人.id`、`tenant_id == 发起人.tenant_id`。
- [ ] `role` 不在 `{"owner","admin","compliance","legal","readonlyaudit"}` → `400 invalid_role`。
- [ ] `wecom_user_id` 缺且 `email` 缺 → `400 wecom_user_id_or_email_required`；`wecom_user_id` 缺但有 `email` → 落 `wecom_user_id = f"invited:{email.lower()}"`。
- [ ] 同 `tenant_id + wecom_user_id` 已有 pending → 不发新行、复用既有 `invite_token`（重发语义）。
- [ ] 邮件函数被调用，链接含 token 且路径为 `/admin/accept-invite?token=...`。

### D. 接受逻辑正确性
- [ ] 有效 token + 密码≥8 → `password_hash` 被 `hash_password` 写入、`status == "active"`、`invite_status == "accepted"`；响应体**不含** token 原文。
- [ ] 无效/不存在 token → `400 invalid_or_expired_token`。
- [ ] 密码 <8 → `400 weak_password`。
- [ ] 接受后该行 `invite_token` 仍保留（用于审计/幂等），但同 token 二次接受应失败（pending 已变 accepted，查不到 → `400`）。

### E. 鉴权与租户隔离
- [ ] `invite_user` 无 session（mock `get_current_user` 抛 `401`）→ `401`。
- [ ] 被邀请人 `tenant_id` 等于 `get_current_user` 返回的 `tenant_id`；端点**不得**读取请求体里的 tenant（grep 确认无 `body.tenant_id` 之类用法）。
- [ ] `get_current_user` 来源为 `app/auth.py:319`（既有依赖），非自造。

### F. 邮件与设置
- [ ] `email.py` 新增 `send_invite_email(to_email, accept_link, locale="zh-CN")`，严格镜像 `send_password_reset_email`（无 SMTP → console 回退）。
- [ ] console 回退**不在日志泄露 token/accept_link**：`caplog` 断言 token 子串不出现（参照 `test_rnd278_password_reset.py:100`）。
- [ ] `settings.py` `EmailSettings` 新增 `invite_base_url`（alias `INVITE_BASE_URL`）；`monkeypatch.setenv("INVITE_BASE_URL", ...)` 后 `get_email_settings().invite_base_url` 正确。

### G. 回归与质量门
- [ ] 既有 `password_forgot`/`password_reset` 端点未被破坏（可跑 `test_rnd278_password_reset.py` 全绿）。
- [ ] `make verify` 全绿——**含 `test_architecture_boundary.py`**：`routers/auth.py` 未 import `app.main`、无反向依赖、未破坏既有 allowlist。
- [ ] `alembic check`（Alembic 1.14）零漂移：本票无 schema 变更，应直接 PASS；若报 drift → FAIL（说明误改了模型/迁移）。
- [ ] 测试文件 `tests/test_rnd285_invite_flow.py` 存在且覆盖 §1 的 C/D/E/F 场景。
- [ ] **未 commit**：`git status` 显示改动为未提交（unstaged/staged 均可，但无 commit 由 agent 产生）。

---

## 2. 路由判定（拿不准如何处理）

- 若开发**加了新迁移/新表** → 直接 FAIL，并在报告明确指出「违反 RND-285 无迁移约束（列由 RND-277 提供）」，不要尝试「帮它改对」，退回开发。
- 若 `get_current_user` 实际签名与 `app/auth.py:319` 不符 → 以仓库实际代码为准，记录差异。
- 若测试因环境（无 DATABASE_URL）跳过 DB 支撑测试 → 允许，但必须有无 DB 冒烟测试（端点可导入、邮件函数可调用）已跑绿；在报告标注跳过的部分。

---

## 3. 交付报告格式（贴回对话）

```
## RND-285 QA 报告
- 结论：PASS / FAIL（N 项 FAIL）
- 范围守门：无迁移 / 未改 models / 未加 router / 未改鉴权 / 无前端 → [PASS/FAIL + 证据]
- 端点：/invite（Depends get_current_user）PASS；/accept（公开）PASS
- 邀请逻辑：pending 行字段正确 / 角色校验 / 占位 wecom_user_id / 防重复 → [逐项]
- 接受逻辑：激活 / 拒无效 token / 拒弱密码 / 幂等 → [逐项]
- 鉴权+租户隔离：401 无 session / tenant 取自 get_current_user / 不读请求体 tenant → [逐项]
- 邮件：send_invite_email + INVITE_BASE_URL / 不泄露 token → [逐项]
- 质量门：make verify 全绿 / alembic check 零漂移 / test_architecture_boundary 通过 / 未 commit → [逐项]
- 阻塞/风险：……
- 证据：关键 git diff 行号、测试输出片段
```
