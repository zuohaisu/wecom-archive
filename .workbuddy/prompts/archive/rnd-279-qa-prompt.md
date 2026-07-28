# RND-279 QA / 验收 agent 提示词 —— F0-4 会话生命周期自动化（清理 + last_active_at 维护）

> 面向独立测试/QA agent。只读言、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-279-execution-prompt.md` 产出的改动。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS（本票无前端改动）。

## 一、验收目标

确认 RND-279（F0-4）达成 Linear 验收标准且零回归：
- **清理过期/吊销会话**：`expires_at` 已过或 `is_revoked=True` 的 `admin_sessions` 行被物理删除，有效未过期行保留；返回删除计数。
- **活动维护 last_active_at**：每次已认证活动（API 经 `get_current_user`、HTML 页经 `require_html_session`）更新 `AdminUser.last_active_at`；节流（约 5 分钟）；`last_login_at` 不受影响。
- **SESSION_TTL_HOURS 可配置**：会话 `expires_at` 与 cookie `max_age` 由 env `SESSION_TTL_HOURS` 驱动（默认 8、夹取 [1, 8760]）。
- **无 schema 变更**：不新建表、不新增 Alembic 迁移（两张表/列已存在）。
- **契约不变**：既有登录/WeCom/OAuth/me/logout 行为、URL、OpenAPI、租户隔离、架构边界全部不变。

前置：F0-1（RND-277）已落地（否则开发 agent 应已停下报告，本票无可验收内容）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 后端 — 现有会话基础设施（确认未破坏）
- [ ] M1 `AdminSession` 模型（`app/db/models.py:204-229`）结构不变（列 `id`/`admin_user_id`/`tenant_id`/`wecom_user_id`/`created_at`/`expires_at`/`is_revoked`），**无** 新增 `last_active_at` 列（会话表不该有，那是 `AdminUser` 的字段）—— 证据：`python -c "from app.db import models; print([c.name for c in models.AdminSession.__table__.columns])"`。
- [ ] M2 `AdminUser.last_active_at` 存在（F0-1 已加，`models.py:198`）—— 证据：同上列名清单含 `last_active_at`。
- [ ] M3 **无新迁移**：`ls alembic/versions/ | tail -1` 与开发前一致；`git diff alembic/versions/` 为空；`git status` 无新增迁移文件 —— 证据：`git diff --stat` + `git status`。

### 后端 — 清理逻辑（核心验收）
- [ ] C1 新增 `app/session_lifecycle.py` 含 `cleanup_expired_sessions(db) -> int` —— 证据：读源码 + `python -c "import app.session_lifecycle"`。
- [ ] C2 过期会话被删：构造 `AdminSession`（`expires_at` 在过去）→ 调 `cleanup_expired_sessions` → 该行消失 —— 证据：pytest + DB 断言（DATABASE_URL 门控）。
- [ ] C3 已吊销会话被删：构造 `is_revoked=True`（且 `expires_at` 在未来，排除过期因素）→ 调清理 → 该行消失 —— 证据：pytest（隔离变量，证明确实是 is_revoked 触发而非过期）。
- [ ] C4 有效未过期且未吊销会话**保留**：构造此类会话 → 调清理 → 行仍在 —— 证据：pytest。
- [ ] C5 返回正确计数：C2+C3 共 2 行被删时返回 `2`；仅有效行时返回 `0` —— 证据：pytest 断言返回值。
- [ ] C6 登录后触发：贯通 `password_login` 成功后，DB 中既有过期/吊销会话被清掉（可先种入过期行再登录，断言登录后该行消失）—— 证据：pytest（串联登录路径）。`wecom_callback` 同路径（可 mock WeCom 调用验证，或仅静态确认代码插入）。

### 后端 — last_active_at 维护（核心验收）
- [ ] A1 `get_current_user`（`auth.py:238`）在已认证 API 请求后更新 `AdminUser.last_active_at` —— 证据：pytest（构造 session + user，`get_db` override 注入，调 `get_current_user` 后断言 `last_active_at` 近似 `now`）。
- [ ] A2 `require_html_session`（`auth.py:287`）在 HTML 页加载后同样更新 `last_active_at` —— 证据：pytest（调该依赖后断言）。
- [ ] A3 **节流**：同一 `now` 连续两次活动，第二次 `last_active_at` 不变（被 300s 窗口挡住）；推进 `now` 超过 300s 后再活动则更新 —— 证据：pytest（单测用 fake db 或 DB 门控）。
- [ ] A4 **不碰 last_login_at**：活动前后 `last_login_at` 不变（取一行先置 `last_login_at`，touch 后该值不变）—— 证据：pytest（关键隔离证据，证明字段语义独立）。
- [ ] A5 **默认 8h**：env 未设 `SESSION_TTL_HOURS` 时 `get_session_ttl_hours()` == 8 —— 证据：pytest（monkeypatch 清 env）。
- [ ] A6 **边界 clamp**：`"0"`→1、`"99999"`→8760、`"-3"`→1、`"abc"`→8（并记 warning）—— 证据：pytest。
- [ ] A7 **会话创建使用配置**：`password_login` 创建的 `AdminSession.expires_at == now + timedelta(hours=get_session_ttl_hours())`，cookie `max_age == ttl*3600`；设 `SESSION_TTL_HOURS=2` 后登录，断言 `expires_at` 为 ~2h 后 —— 证据：pytest（串联，验证配置真正生效而非仍硬编码 8）。

### 安全与契约
- [ ] S1 安全延续：grep `session_lifecycle.py` / `auth.py` 改动，确认未把 session token / 密码 / wecom 身份写入日志（沿用现有脱敏纪律）—— 证据：读源码。
- [ ] S2 清理无越权副作用：清理只动 `admin_sessions`，不碰 `admin_users` / 其它表 —— 证据：pytest（清理后断言 user 行数不变）。
- [ ] S3 不改现有路由：`password_login`/`wecom_login`/`wecom_callback`/`auth_me`/`logout` 行为与 URL 不变；cookie 标志不变；`/api/auth/password/login` 404/500 守卫不变 —— 证据：`git diff` 仅含新增调用/配置读取，无对既有路由体的删除或修改；`make verify` 中 auth 既有测试全绿。

### 全局契约
- [ ] G1 架构边界：`backend/tests/test_architecture_boundary.py` PASS；`app.session_lifecycle` 已加入 `_FLAT_SERVICE_MODULES`；`app/main.py` 无新增路由；`session_lifecycle.py` 未 `import app.routers.*` / `app.main` —— 证据：`make verify` + 读测试列表 + grep。
- [ ] G2 租户隔离未被破坏：清理按全局条件删除，不引入租户串扰；`get_current_user` 仍严格以 cookie session 的 `tenant_id` 为唯一授权域 —— 证据：pytest（沿用 `test_password_auth.py` / `test_auth.py` 既有租户断言仍绿）。
- [ ] G3 `test_http_contract.py` 仍 PASS（本票为内部逻辑变更，无新增公开路由，不应破坏既有契约断言）—— 证据：`make verify`。
- [ ] G4 `make verify` 全绿（lint-diff / typecheck / build / test，含新增 `test_rnd279_session_lifecycle.py`）—— 证据：命令输出。
- [ ] G5 无新第三方依赖：`backend/requirements.txt` 未新增 —— 证据：`git diff requirements.txt`。
- [ ] G6 B 层生产路径未触碰：`backend/scripts/`、`deploy.yml`、systemd 单元名、`/srv/apps/wecom-archive-365`、`.env.example` 均无改动（`git diff` 不含这些路径）—— 证据：`git diff --stat` + `git status`。

## 三、回归套件（必须全绿）

`make verify` 全绿，重点确认：
- `backend/tests/test_architecture_boundary.py`
- `backend/tests/test_password_auth.py`（RND-276 契约）
- `backend/tests/test_auth.py`、`test_rnd225_auth_fail_closed.py`
- `backend/tests/test_rnd277_admin_user_extension.py`（F0-1 列仍在）
- `backend/tests/test_verify_alembic_head.py`、`test_makefile_lint_diff.py`
- 新增 `backend/tests/test_rnd279_session_lifecycle.py`（见第二节 C1-C6、A1-A7、G1-G6）

DB 支撑测试用 `DATABASE_URL` 门控；无 DB 时相关用例自动 skip，但**配置解析（A5/A6）与节流逻辑（A3，用 fake db）必须可无 DB 运行**（参照 `test_password_auth.py` 的 `dependency_overrides[get_db]` 工厂模式 / `test_tenant_foundation.py` 的 `_DB_AVAILABLE` 门控）。

## 四、智能路由判定（每轮必给）

- **源码有 Bug**（如清理误删有效会话、节流失效、`last_login_at` 被改、架构边界违规、`SESSION_TTL_HOURS` 仍硬编码 8）→ 反馈开发 agent 修复，附：失败用例/错误栈 + 期望行为 + 相关文件:行号；不自行改实现。
- **测试代码有 Bug**（如断言旧路径、mock 缺字段）→ 可自行修正测试，仅当断言已过期路径时须显式标注「修正测试而非实现」。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 对比。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留并交回开发 agent。

## 五、RED→GREEN 记录要求

- RED（改前基线）：`app/session_lifecycle` 不存在；`SESSION_TTL_HOURS` 硬编码 8（`auth.py:45`，`routers/auth.py` 4 处引用）；`_FLAT_SERVICE_MODULES` 无 `session_lifecycle`；`last_active_at` 在任何活动后均不被更新（DB 中保持 `None`）；`alembic/versions/` 无本票新增迁移。
- GREEN（改后）：M1-M3、C1-C6、A1-A7、S1-S3、G1-G6 全 PASS；无新迁移；`make verify` 绿。
- 量化：节流窗口 300s（A3 需精确断言「同 now 二次不写」）；SESSION_TTL_HOURS 边界 clamp 值（A6）需逐档断言。

## 六、交付报告格式

```
RND-279 验收结论：PASS / FAIL
F0-1 前置：已落地 / 未落地（未落地则无验收内容）
迁移：无新增（符合预期）/ 意外新增（FAIL）
核心验收：
  - 清理过期/吊销会话：PASS/FAIL（证据 C2-C6）
  - 登录后触发清理：PASS/FAIL（证据 C6）
  - 活动维护 last_active_at：PASS/FAIL（证据 A1-A4）
  - SESSION_TTL_HOURS 可配置+生效：PASS/FAIL（证据 A5-A7）
  - 节流 300s：PASS/FAIL（证据 A3）
  - 不动 last_login_at：PASS/FAIL（证据 A4）
契约：http_contract 不变 / 架构边界 PASS / 无新依赖 / B 层未碰：PASS/FAIL
回归：make verify ___（绿/红）
遗留：___
```
