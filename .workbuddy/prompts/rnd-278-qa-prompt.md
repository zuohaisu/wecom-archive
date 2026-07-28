# RND-278 QA / 验收 agent 提示词 —— F0-3 password_reset_tokens 表 + 邮箱找回流程

> 面向独立测试/QA agent。只读言、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-278-execution-prompt.md` 产出的改动。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS。

## 一、验收目标

确认 RND-278（F0-3）达成 Linear 验收标准且零回归：
- **申请发邮件**：提交邮箱后系统生成令牌并向该邮箱发出含重置链接的邮件。
- **链接重置成功**：持有效令牌 + 新密码可成功重置，随后可用新密码登录。
- **过期/已用令牌被拒绝**：过期令牌、已使用令牌、伪造令牌一律被拒（400）。
- **零枚举 / 安全**：令牌哈希存储、原始令牌不落库/不回传；`/forgot` 不泄露邮箱是否存在。
- **契约不变**：既有登录/WeCom/OAuth/me/logout 行为、URL、OpenAPI、租户隔离、i18n 既有键、架构边界全部不变。

前置：F0-1（RND-277 + RND-276）已落地（否则开发 agent 应已停下报告，本票无可验收内容）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 后端 — 模型与迁移
- [ ] M1 `PasswordResetToken` 存在于 `app/db/models.py`，列含 `id`(PK String36)/`admin_user_id`(FK,索引)/`tenant_id`(FK,索引)/`token`(unique,索引)/`expires_at`(DateTime tz)/`used`(Boolean)/`created_at` —— 证据：`python -c "from app.db import models; print([c.name for c in models.PasswordResetToken.__table__.columns])"`。
- [ ] M2 `token` 列为 **SHA-256 哈希**（长度 64 hex），**非**明文令牌 —— 证据：读 `app/auth.py:create_password_reset_token` 确认 `hashlib.sha256(raw).hexdigest()`；grep 确认无代码把 raw token 写入 `PasswordResetToken.token`。
- [ ] M3 新 Alembic 迁移 `down_revision` == 运行时 `alembic heads` 输出（**不是**硬编码 0016/0017），`alembic upgrade head && alembic check` 绿 —— 证据：命令输出 `Status: Success`。
- [ ] M4 迁移体只含 `password_reset_tokens` 建表，未改动 `uq_admin_users_tenant_wecom` 等既有对象 —— 证据：读迁移文件 + `git diff alembic/versions/`。

### 后端 — 端点行为（核心验收）
- [ ] B1 `POST /api/auth/password/forgot` 提交**已注册 active 且含 password_hash 的 per-user 用户邮箱** → 200 `{"ok": true}`；DB 新增一条 `PasswordResetToken`（used=False、expires_at 在未来）；邮件模块被调用（console 传输下 `logger.warning("[email-console] ...")` 可见链接）。 —— 证据：pytest + 捕获日志/DB 断言。
- [ ] B2 提交**不存在的邮箱** → 200 且响应体（status+body）与 B1 **逐字节相同**；DB **无**新令牌行、**无**发信。 —— 证据：对比两次响应 + DB 行数断言（零枚举核心证据）。
- [ ] B3 提交**禁用(status=disabled)用户**或**无 password_hash（仅 env 引导）用户**邮箱 → 200 同体；无令牌/无发信。 —— 证据：同 B2。
- [ ] B4 `POST /api/auth/password/reset` 持 **B1 生成的 raw token** + 新密码 → 200 `{"ok": true}`；`user.password_hash` 已更新（= `hash_password(new)`）；该令牌行 `used=True`。 —— 证据：pytest + DB 断言。
- [ ] B5 持 **已 used 的令牌**（B4 之后）再次 reset → 400 `invalid_or_expired_token`，**不**改密码。 —— 证据：pytest。
- [ ] B6 持 **过期令牌**（构造 `expires_at` 在过去） → 400，且该行被置 `used=True`。 —— 证据：pytest。
- [ ] B7 持 **伪造/随机令牌** → 400，无密码变更、无用户泄露。 —— 证据：pytest。
- [ ] B8 重置成功后用**新密码**走 `POST /api/auth/password/login`（per-user 路径，RND-276）可登录 200；旧密码失败 401。 —— 证据：pytest 串联。
- [ ] B9 **重申请失效旧令牌**：同一用户再 `/forgot` 一次，原令牌即便未过期也被置 `used=True` → B9 旧令牌 reset 返回 400。 —— 证据：pytest。
- [ ] B10 新密码长度 < 8 → 400 `weak_password`；合法长度 → 通过。 —— 证据：pytest。

### 后端 — 安全与契约
- [ ] S1 raw token **不出现**于任何 API 响应体、不写日志（`grep -rn "token" app/routers/auth.py` 确认响应只回 `{"ok": true}`，reset 响应不含令牌）。 —— 证据：读源码 + 测试捕获响应。
- [ ] S2 零枚举：`/forgot` 三种结果（存在/不存在/禁用）响应体逐字节一致（B1-B3 已证）。 —— 证据：pytest 断言 `resp1.content == resp2.content == resp3.content`。
- [ ] S3 SMTP 缺失 fail-closed：无 `SMTP_HOST` 时 `/forgot` 仍 200、邮件走 console 传输不崩溃，请求方无差异。 —— 证据：pytest（mock `get_email_settings` 返回空 host）。
- [ ] S4 不改现有路由：`password_login`/`wecom_login`/`wecom_callback`/`auth_me`/`logout` 行为与 URL 不变；`/api/auth/password/login` 404/500 守卫不变；cookie 标志不变。 —— 证据：`git diff` 仅含新增、无对既有路由体的删除/修改；`make verify` 中 auth 既有测试全绿。

### 前端（SSR + 原生 JS，D1 冻结）
- [ ] F1 `GET /admin/forgot-password` 返回 200 HTML，含邮箱输入框、`styles.css` 引用、i18n 引导脚本、`applyI18n()` 调用；提交走 `fetch('/api/auth/password/forgot')` 并在成功后显示「邮件已发送」提示。 —— 证据：curl 页面 + 读模板。
- [ ] F2 `GET /admin/reset-password?token=xxx` 返回 200 HTML，含隐藏 token 域、新密码/确认密码输入、`fetch('/api/auth/password/reset')`；`invalid_or_expired_token` 显示「链接无效或已过期」。 —— 证据：curl + 读模板。
- [ ] F3 登录页（`/admin/login`，password 模式）的「忘记密码？（即将上线）」惰性 `<span>` 已替换为指向 `/admin/forgot-password` 的链接（开发提示词步骤 6）。 —— 证据：curl 登录页 grep `href="/admin/forgot-password"`。
- [ ] F4 不引 React；新页面仅用原生 JS + `styles.css`（grep 确认无 React/JSX 依赖）。 —— 证据：grep。

### 全局契约
- [ ] C1 架构边界：`backend/tests/test_architecture_boundary.py` PASS；`app.email` 已加入 `_FLAT_SERVICE_MODULES`；`app/main.py` 无新增路由；`email.py` 未 `import app.routers.*`/`app.main`。 —— 证据：`make verify` + 读测试列表 + grep。
- [ ] C2 租户隔离：`/forgot` 仅匹配 default 租户（`Tenant.slug=='default' and is_active`），跨租户同名邮箱不串。 —— 证据：pytest（建第二个租户同名邮箱，确认只给 default 租户用户发信）。
- [ ] C3 i18n：`login.forgot*`/`login.reset*` 新键已加入**每个** locale 块（zh-CN/zh-TW/en），格式对齐既有 `login.*`；既有键未被删改（`login.forgotPasswordDisabled` 如清理需确认 `test_i18n_foundation.py` 仍绿）。 —— 证据：grep 三块 + `make verify`。
- [ ] C4 `test_http_contract.py` 仍 PASS（新增公开路由为增量，不破坏既有契约断言）。 —— 证据：`make verify`。
- [ ] C5 `make verify` 全绿（lint-diff / typecheck / build / test，含新增 `test_rnd278_password_reset.py`）。 —— 证据：命令输出。
- [ ] C6 无新第三方依赖：`backend/requirements.txt` 未新增邮件库（stdlib `smtplib`/`email`）。 —— 证据：`git diff requirements.txt`。
- [ ] C7 `.env.example` 仅**追加** SMTP 段、未改既有条目（除非按执行提示词第六·三节改走 docs 说明）；B 层生产路径（`/srv/apps/...`、systemd、`deploy.yml`、`backend/scripts`）未触碰。 —— 证据：`git diff`。

## 三、回归套件（必须全绿）

`make verify` 全绿，重点确认：
- `backend/tests/test_architecture_boundary.py`
- `backend/tests/test_password_auth.py`（RND-276 契约）
- `backend/tests/test_auth.py`、`test_rnd225_auth_fail_closed.py`
- `backend/tests/test_verify_alembic_head.py`、`test_makefile_lint_diff.py`
- `backend/tests/test_i18n_foundation.py`
- 新增 `backend/tests/test_rnd278_password_reset.py`（见第二节 B1-B10、C1-C3）

DB 支撑测试用 `DATABASE_URL` 门控；无 DB 时相关用例自动 skip，但 SQLAlchemy mock 路径（B1-B10）必须可无 DB 运行（参照 `test_password_auth.py` 的 `dependency_overrides[get_db]` 工厂模式）。

## 四、智能路由判定（每轮必给）

- **源码有 Bug**（如令牌未置 used、零枚举被打破、架构边界违规）→ 反馈开发 agent 修复，附：失败用例/错误栈 + 期望行为 + 相关文件:行号；不自行改实现。
- **测试代码有 Bug**（如断言旧路径、mock 缺字段）→ 可自行修正测试，仅当断言已过期路径时须显式标注「修正测试而非实现」。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 对比。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留并交回开发 agent。

## 五、RED→GREEN 记录要求

- RED（改前基线）：`PasswordResetToken` 不存在；`ls alembic/versions/ | tail -1` = `0016_media_download_attempts.py`；`/forgot`/`/reset` 路由不存在（404）。
- GREEN（改后）：M1-M4、B1-B10、S1-S4、F1-F4、C1-C7 全 PASS；`alembic check` 绿；`make verify` 绿。
- 量化：无需性能数字，但 B1/B2/B3 响应体需做**逐字节相等**断言（零枚举硬证据）。

## 六、交付报告格式

```
RND-278 验收结论：PASS / FAIL
F0-1 前置：已落地 / 未落地（未落地则无验收内容）
迁移 head：<alembic heads 输出>  alembic check：绿/红
核心验收：
  - 申请发邮件：PASS/FAIL（证据）
  - 链接重置成功：PASS/FAIL（证据 B4/B8）
  - 过期令牌拒绝：PASS/FAIL（证据 B6）
  - 已用令牌拒绝：PASS/FAIL（证据 B5）
  - 零枚举：PASS/FAIL（证据 B1-B3 逐字节相等）
安全：token 哈希存储/原始不回传：PASS/FAIL（S1/S2）
契约：http_contract 不变 / i18n 不变 / 架构边界 PASS / 无新依赖：PASS/FAIL
回归：make verify ___（绿/红）
遗留：___
```
