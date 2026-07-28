# RND-286 QA / 验收 agent 提示词 —— A3-3 启用/停用 + 管理员重置密码

> 面向独立测试/QA agent。只读言、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-286-execution-prompt.md` 产出的改动。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS。

## 一、验收目标

确认 RND-286（A3-3）达成 Linear 验收标准且零回归：
- **启用/停用**：管理员可翻转同租户用户的 `status`；停用后该用户无法登录（401）。
- **管理员重置**：管理员可触发重置，系统向目标邮箱发出含重置链接的邮件（复用 F0-3 基础设施）。
- **安全与隔离**：租户隔离（跨租户 404）、RBAC（非 admin/owner 403）、零枚举（目标缺失一律 404）、原始令牌不回传。
- **契约不变**：无 schema 变更（`alembic check` 绿）、既有登录/WeCom/me/logout 行为不变、`test_http_contract.py` 仅增量更新、架构边界 PASS、无新依赖。

前置：F0 已落地（否则开发 agent 应已停下报告，本票无可验收内容）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 后端 — 端点行为（核心验收）
- [ ] B1 `PATCH /api/admin/users/{id}`（admin/owner、同租户 active 用户，body `{"status":"disabled"}`）→ 200，响应 DTO 含 `status:"disabled"`；DB 中该用户 `status` 已置 `disabled`。 —— 证据：pytest + DB 断言。
- [ ] B2 **AC 核心**：B1 停用后，该用户走 `POST /api/auth/password/login`（per-user 路径，F0-2）→ **401** `Invalid credentials`（`password_login` 的 `AdminUser.status == "active"` 守卫，`app/routers/auth.py:459` 生效）。重新 `{"status":"active"}` 后登录恢复 200。 —— 证据：串联 pytest。
- [ ] B3 `PATCH` 置 `status:"active"`（从 disabled 恢复）→ 200，DTO `status:"active"`。 —— 证据：pytest。
- [ ] B4 `PATCH` 非法 `status` 值（如 `"foo"`）→ 400 `invalid_status`；合法枚举外任何值均拒绝。 —— 证据：pytest。
- [ ] B5 `PATCH` 跨租户用户 id → **404** `User not found`（零枚举，不泄露是否存在）；同租户不存在 id 也 404，二者响应一致。 —— 证据：建第二租户用户 id 调用，断言 404。
- [ ] B6 `PATCH` 自己为 `disabled` → 400 `cannot_disable_self`；自己置 `active` → 200（允许）。 —— 证据：pytest。
- [ ] B7 `POST /api/admin/users/{id}/reset-password`（admin/owner、同租户 active 且有 `email` 用户）→ 200 `{"ok": true}`；`PasswordResetToken` 新增一行（used=False、expires_at 在未来、`tenant_id`==当前租户）；邮件模块被调用（console 传输下 `logger.warning("[email-console] ...")` 可见链接，链接含 `token=`）。 —— 证据：pytest + 捕获日志/DB 断言。
- [ ] B8 `POST reset-password` 对 `disabled` 用户 → 409 `user_not_active`。 —— 证据：pytest。
- [ ] B9 `POST reset-password` 对无 `email` 用户 → 400 `user_has_no_email`。 —— 证据：pytest。
- [ ] B10 `POST reset-password` 跨租户 id → 404（零枚举）。 —— 证据：pytest。
- [ ] B11 **令牌可用**：B7 生成的 raw token 走 `POST /api/auth/password/reset`（F0-3）+ 新密码 → 200，密码已更新，令牌 `used=True`（验证管理员重置链路端到端可用）。 —— 证据：pytest 串联。
- [ ] B12 **SMTP 缺失 fail-closed**：无 `SMTP_HOST` 时 `POST reset-password` 仍 200、走 console 传输不崩溃（参照 F0-3 S3）。 —— 证据：mock `get_email_settings` 返回空 host。

### 后端 — 安全与隔离
- [ ] S1 **租户隔离**：目标查询必须 `AdminUser.tenant_id == 当前会话租户`（读 `app/routers/users.py:_resolve_target` 确认 filter 含 `tenant_id`）；构造跨租户 id 一律 404（B5/B10 已证）。 —— 证据：读源码 + 跨租户用例。
- [ ] S2 **RBAC**：以 `readonlyaudit`/`compliance`/`legal` 角色调用两端点 → 403 `Insufficient role for this operation`；未认证 → 401（先 401 后 403 顺序由 `require_role` 保证）。 —— 证据：pytest。
- [ ] S3 **零枚举**：跨租户 id 与不存在 id 均 404，响应体一致（不泄露账号是否存在）。 —— 证据：pytest 断言两次响应体等价。
- [ ] S4 **令牌不回传**：`POST reset-password` 响应仅 `{"ok": true}`，不含 token；grep 确认 `app/routers/users.py` 未把 `raw_token` 写入任何响应体/日志。 —— 证据：读源码 + 测试捕获响应。
- [ ] S5 原始重置令牌只出现在邮件链接（`create_password_reset_token` 返回 + `send_password_reset_email`），`PasswordResetToken.token` 列为 SHA-256 哈希（F0-3 既有约束，本票未改）。 —— 证据：读 `app/auth.py:create_password_reset_token`。

### 全局契约
- [ ] C1 **无 schema 变更**：`alembic check` 绿（Status: Success）；`git diff` 不含 `app/db/models.py`/`alembic/versions/` 改动。 —— 证据：命令输出 + `git diff`。
- [ ] C2 **架构边界**：`backend/tests/test_architecture_boundary.py` PASS；`app/routers/users.py` 未 `import app.routers.*`/`app.main`（仅 `app.auth`/`app.email`/`app.db.models`/`app.db.session`/`app.settings`）；`app/main.py` 仅新增 `include_router`，无内联路由。 —— 证据：`make verify` + grep。
- [ ] C3 **HTTP 契约同步**：`test_http_contract.py` 三处已更新且 `make verify` 绿 —— `route_count == 44`（L325）、path 集合含两条新路由（L334-377）、snapshot 列表含两条（`response_model="None"`、`response_class="None"`，L403-492）。 —— 证据：`make verify` + 读测试。
- [ ] C4 **既有路由不变**：`password_login`/`wecom_login`/`wecom_callback`/`auth_me`/`logout` URL 与行为不变；`/api/auth/me` 不变；`/api/auth/password/login` 404/500 守卫不变。 —— 证据：`git diff` 仅含新增 + 契约测试更新，无既有路由体改动；既有 auth 测试全绿。
- [ ] C5 **登录契约**：`test_password_auth.py`（RND-276 契约）全绿，env 回退路径不受影响。 —— 证据：`make verify`。
- [ ] C6 **无新第三方依赖 / 不碰 B 层**：`requirements.txt`、`.env.example`、systemd、`deploy.yml`、`backend/scripts` 未改动。 —— 证据：`git diff`。
- [ ] C7 `make verify` 全绿（lint-diff / typecheck / build / test，含新增 `test_rnd286_user_admin.py`）。 —— 证据：命令输出。

## 三、回归套件（必须全绿）

`make verify` 全绿，重点确认：
- `backend/tests/test_architecture_boundary.py`
- `backend/tests/test_http_contract.py`（本票已同步更新）
- `backend/tests/test_password_auth.py`（RND-276 契约 / F0-2）
- `backend/tests/test_auth.py`、`test_rnd225_auth_fail_closed.py`
- `backend/tests/test_verify_alembic_head.py`、`test_makefile_lint_diff.py`
- `backend/tests/test_i18n_foundation.py`（本票无 i18n 改动，应仍绿）
- 新增 `backend/tests/test_rnd286_user_admin.py`（见第二节 B1-B12、S1-S5）

DB 支撑测试用 `DATABASE_URL` 门控；无 DB 时相关用例自动 skip，但 SQLAlchemy mock 路径（B1-B12 中可无 DB 运行的部分）必须可无 DB 运行（参照 `test_password_auth.py` 的 `dependency_overrides[get_db]` 工厂模式）。

## 四、智能路由判定（每轮必给）

- **源码有 Bug**（如跨租户未 404、RBAC 缺失、停用后仍可登录、令牌回传、契约测试未更新导致 route_count 不符）→ 反馈开发 agent 修复，附：失败用例/错误栈 + 期望行为 + 相关文件:行号；不自行改实现。
- **测试代码有 Bug**（如断言旧路径、mock 缺字段）→ 可自行修正测试，仅当断言已过期路径时须显式标注「修正测试而非实现」。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 对比。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留并交回开发 agent。

## 五、RED→GREEN 记录要求

- RED（改前基线）：`app/routers/users.py` 不存在；`app/main.py` 无 `api/admin/users`；`route_count` = 42；reset-password 端点 404。
- GREEN（改后）：B1-B12、S1-S5、C1-C7 全 PASS；`alembic check` 绿；`make verify` 绿。
- 量化：B2 需断言停用后 `password_login` 返回 401（AC 硬证据）；其余为布尔 PASS。

## 六、交付报告格式

```
RND-286 验收结论：PASS / FAIL
F0 前置：已落地 / 未落地（未落地则无验收内容）
迁移 head：<alembic heads 输出>  alembic check：绿/红
核心验收：
  - 启用/停用翻转：PASS/FAIL（证据 B1/B3）
  - 停用用户无法登录：PASS/FAIL（证据 B2）
  - 管理员重置发信：PASS/FAIL（证据 B7/B11）
  - 跨租户隔离：PASS/FAIL（证据 B5/B10/S1）
  - RBAC 门禁：PASS/FAIL（证据 S2）
  - 零枚举：PASS/FAIL（证据 B5/S3）
安全：令牌不回传/哈希存储：PASS/FAIL（S4/S5）
契约：alembic 无变更 / http_contract 增量更新 / 架构边界 PASS / 无新依赖：PASS/FAIL
回归：make verify ___（绿/红）
遗留：___
```
