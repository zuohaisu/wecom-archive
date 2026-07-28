# RND-276 QA / 验收 agent 提示词 —— F0-2 per-user 密码鉴权（替换 env 单 hash）

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-276-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认密码登录已从 env 单 hash 升级为按 `AdminUser.email` 逐用户校验 `password_hash`（复用 `verify_password`），`/api/auth/me` 返回真实 `role`；**WeCom OAuth 行为不变**；**错误响应对用户名是否存在零泄露**；既有 env 引导账户与所有 `test_password_auth.py` 契约回归全绿；无 schema/迁移变更（`alembic check` 仍绿）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 依赖与范围

- [ ] D1 前置 F0-1（RND-277）已落地：`AdminUser` 含 `password_hash`/`role`，`alembic check` 绿 —— 证据：执行提示词第二节校验命令
- [ ] S1 改动文件仅 `backend/app/routers/auth.py` + 测试（`test_password_auth.py` mock 补字段 + 新 `test_rnd276_per_user_auth.py`）—— 证据：`git diff --name-only`
- [ ] S2 无 `models.py` / 无新 migration / 无 `auth.py` 的 `verify_password`/`hash_password` 逻辑改动 —— 证据：`git diff` 比对

### 后端 per-user 鉴权（根因）

- [ ] B1 `password_login` 按 `AdminUser.email`（lower+trim）在 default 租户内匹配 active 用户并 `verify_password(password_hash)` —— 证据：读 `routers/auth.py` + 新测试 P1 通过
- [ ] B2 成功登录建立会话 + HttpOnly `session_id` cookie（标志同前）—— 证据：测试 P1 / `set-cookie` 断言
- [ ] B3 错误密码 → 401 —— 证据：测试 P2
- [ ] B4 不存在邮箱 → 401，且**响应体（status + detail）与 B3 逐字节相同** —— 证据：测试 P3 + 直接 diff 两次响应文本
- [ ] B5 `status='disabled'` 用户 → 401（等同不存在，不泄露）—— 证据：测试 P5
- [ ] B6 `/api/auth/me` 登录后返回 `"role"` == 用户真实 role（非 None），不泄露 `pbkdf2`/`__pwd__` —— 证据：测试 P4
- [ ] B7 env 引导账户仍可用（向后兼容）：env 配置下 `username=admin` 仍 200 —— 证据：测试 P6 + 现有 `test_password_login_valid_credentials_returns_200`

### 零泄露强化

- [ ] L1 后端代码中「用户不存在」与「密码错误」走同一 401 + 同一 `"Invalid credentials"` 文案，无分叉 —— 证据：读 `password_login` 失败分支
- [ ] L2 每请求至少一次 `verify_password`（计时均衡）—— 证据：代码评审 `password_login`，确认 env 回退分支无条件执行 `compare_digest`+`verify_password`
- [ ] L3 响应体不含提交的用户名/邮箱/密码/hash —— 证据：测试 P2/P3 断言

### WeCom 不变（硬契约）

- [ ] W1 `wecom_login`（`L334–368`）/`wecom_callback`（`L376–579`）无任何改动 —— 证据：`git diff` 不含这两段；`test_auth.py` 的 callback 测试仍绿
- [ ] W2 `/api/wecom/archive/events` 仍公开（不受 password 模式影响）—— 证据：现有测试 `test_wecom_archive_events_remains_public_in_password_mode` 绿

### 全局契约

- [ ] C1 `make verify` 全绿（lint-diff / typecheck / build / test，含 `test_architecture_boundary.py`）
- [ ] C2 `alembic check` 仍绿（本票无模型/迁移变更）
- [ ] C3 路由数 / URL / 成功体 / 失败体 / 404 / 500 守卫不变 —— 证据：`test_http_contract.py` 相关用例 + 现有 `test_password_auth.py` 全绿
- [ ] C4 未 commit（`git log` HEAD 未前进）

## 三、回归套件（必须全绿）

1. `cd backend && make verify`（全量；应含新增 `test_rnd276_per_user_auth.py` 与既有 `test_password_auth.py`）。
2. 若环境有 `DATABASE_URL`：per-user 的 DB 支撑用例（P1–P6）应真实跑通；否则按门控 skip，报告中注明「需有 DB 环境复测」，不得判 FAIL。
3. `alembic check`（确认无 pending change，因本票无 schema 变更）。

## 四、智能路由判定（每轮必给）

- 改了 `wecom_login`/`wecom_callback` 或引入了 schema 变更 → 判 FAIL，明确范围溢出。
- per-user 未生效 / `verify_password` 未复用 / `role` 仍 None → 反馈开发 agent 修复，附 `routers/auth.py` 行号 + 期望。
- 错误响应分叉（泄露用户名存在）→ 反馈修复，附失败分支代码。
- 既有 `test_password_auth.py` 契约回归失败 → 优先判断是开发改坏还是 mock 漏补 `role`；mock 漏补属实现问题，反馈开发在测试内补 `role` 字段（仅测试 mock，不改契约）。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第1轮修复，第2轮回归；仍不过则标注遗留。

## 五、交付报告格式

```
RND-276 验收结论：PASS / FAIL
依赖/范围：D1 F0-1 __；S1/S2 文件清单 __
per-user 鉴权：B1–B7 PASS/FAIL + 证据
零泄露：L1/L2/L3 PASS/FAIL
WeCom 不变：W1/W2 PASS/FAIL
契约：make verify __（绿/红）；alembic check __；http_contract __
遗留：__
```
