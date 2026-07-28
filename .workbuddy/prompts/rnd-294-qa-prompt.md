# RND-294 QA / 验收 agent 提示词 —— A7-2 审计写入钩子（write_audit 工具）

> 面向独立测试/QA agent。只读验收、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-294-execution-prompt.md` 产出的工作树改动。

## 一、验收目标

确认 RND-294 在已落地的 A7-1 `audit_logs` 之上完成了审计写入钩子：
1. 新建 `backend/app/audit.py`，提供 `write_audit(...)` + 规范词表 `AuditAction` / `AuditObjectType`；
2. `write_audit` 为 **fail-safe**（savepoint 隔离、吞异常、绝不向外抛、不破坏主事务）；
3. **additive** 接入认证生命周期（`wecom_callback` 登录、`auth_logout` 退出，`password_login` 与 RND-276 协调）；
4. 配套测试 + 架构边界登记。

**零回归**：不改 schema（`alembic check` 仍绿）、**不新建路由**（route count 不变）、不改登录/WeCom 语义、`make verify` 全绿、`AuditLog` 模型/迁移非本票所建（属 A7-1）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 前置依赖（A7-1 已合并）
- [ ] P1 `AuditLog` 模型存在 —— 证据：`python -c "from app.db.models import AuditLog"`
- [ ] P2 `alembic check` 绿（开发 agent 未动 schema；若红，先判其越界改了模型/迁移）

### 写入钩子模块（文件 `backend/app/audit.py`）
- [ ] B1 `write_audit(db, *, tenant_id, action, object_type, admin_user_id=None, object_id=None, detail=None)` 存在，签名含 keyword-only 参数 —— 证据：读 `app/audit.py`
- [ ] B2 `write_audit` 内部用 `with db.begin_nested():` 包裹 `db.add(AuditLog(...))` + `db.flush()`，且整体在 `try/except Exception` 中 —— 证据：读源码
- [ ] B3 `write_audit` 吞异常（`except Exception:` 仅 `logger.exception(...)`，**无 `raise`**），且**不自行 `db.commit()`** —— 证据：读源码
- [ ] B4 `AuditAction` / `AuditObjectType` 常量存在且为字符串：`LOGIN=="auth.login"`、`LOGOUT=="auth.logout"`、`USER=="admin_user"`、`SESSION=="admin_session"` 等 —— 证据：grep + 断言
- [ ] B5 `detail` 仅结构化上下文；模块 docstring 明确「绝不存消息正文 / decrypted_payload」（SF-1）—— 证据：读 docstring

### 架构边界
- [ ] C0 `backend/tests/test_architecture_boundary.py` 的 `_FLAT_SERVICE_MODULES` 含 `"app.audit",`（约 `L71` 紧邻 `"app.auth",` 之后）—— 证据：grep
- [ ] C0b `make verify` 含 `test_architecture_boundary.py` 全过（无 `service→router` / `router→main` 违规）

### 接入认证生命周期（文件 `backend/app/routers/auth.py`，additive）
- [ ] A1 `wecom_callback`：在 `L550` `db.flush()` 之后、`L571` `db.commit()` 之前插入 `write_audit(... action=AuditAction.LOGIN ...)` —— 证据：grep `write_audit` + 行号对照
- [ ] A2 `auth_logout`：在 `L650` `session.is_revoked=True` 之后、`L651` `db.commit()` 之前、`if session:` 块内插入 `write_audit(... action=AuditAction.LOGOUT ...)` —— 证据：grep + 行号对照
- [ ] A3 `password_login`：若存在 `write_audit` 调用，须为 additive（置于 session 创建块之后、其 `db.commit()` 之前），且**未回退** RND-276 的 per-user 改动 —— 证据：读 `password_login` 与 `git diff` 比对
- [ ] A4 两处稳定接入（`wecom_callback` / `auth_logout`）**未触碰** `get_current_user` / WeCom OAuth / 登录语义；`routers/auth.py` 顶部已 `from app.audit import write_audit, AuditAction, AuditObjectType`，且 `AuditLog` 并入 `from app.db.models import ...` —— 证据：读 import 区 + diff

### 测试
- [ ] T1 `backend/tests/test_rnd294_audit_hook.py` 存在，含：词表单测 + DB 门控写入/回环（detail 字典回环）+ fail-safe 单测（stub `begin_nested` 抛异常时 `write_audit` 不向外抛）+ DB 门控集成（logout 后 `audit_logs` 出现 `LOGOUT` 行）—— 证据：文件 + pytest

### 范围守门（scaffold，不得溢出）
- [ ] G1 `git diff --name-only` 仅含：`backend/app/audit.py`（新）+ `backend/app/routers/auth.py` + `backend/tests/test_architecture_boundary.py` + `backend/tests/test_rnd294_audit_hook.py`
- [ ] G2 `git diff` **不含**任何 Alembic migration、不含 `app/db/models.py` 的 `AuditLog` 定义（模型/迁移专属 A7-1）—— 证据：grep `AuditLog` 在 diff 中仅出现在 `routers/auth.py` 的 import 与 `app/audit.py` 的 import
- [ ] G3 `git grep "^\s*@router\.\|@app\."` 路由数较基线**不变**（本票不新建端点）—— 证据：grep 比对 / `test_http_contract` 路由数断言
- [ ] G4 未改动 `get_current_user` / `require_html_session` / WeCom OAuth 流程语义

### 全局契约
- [ ] K1 `alembic check` 绿（无 pending change；本票零 schema 变更）
- [ ] K2 `make verify` 全绿（含 `test_architecture_boundary.py`、`test_http_contract.py`、`test_rnd294_audit_hook.py`）
- [ ] K3 无 git commit 产生（`git log` HEAD 未前进；改动全在工作区）

## 三、回归套件

1. `cd backend && make verify`（全量；应含 `test_rnd294_audit_hook.py` + `test_architecture_boundary.py` + `test_http_contract.py`）。
2. 若环境有 `DATABASE_URL`：额外确认 `alembic check` 绿，以及 DB 支撑测试（写入/回环、logout→audit 集成）通过。
3. 若 `DATABASE_URL` 缺失：DB 支撑类测试按 `_DB_AVAILABLE` 门控自动 skip；此类项标记「需有 DB 环境复测」，不得判 FAIL。

## 四、智能路由判定（每轮必给）

- `AuditLog` 导入失败 / `alembic check` 红（A7-1 未合并）→ 先判**前置未满足**，反馈「需先合并 RND-293（A7-1）」，不要求本票补模型/迁移。
- `write_audit` 非 fail-safe（抛异常 / 无 `begin_nested` / 有 `raise`）→ 反馈修复，附 `app/audit.py` 具体行号。
- `app.audit` 未入 `_FLAT_SERVICE_MODULES` 导致架构测试失败 → 反馈补登记（约 `L71`）。
- 接入点位置错（在 `db.commit()` 之后、或破坏登录语义、或回退 RND-276/278 改动）→ 反馈修正，附 `routers/auth.py` 行号 + 期望。
- 溢出本票范围（自建 `AuditLog` 模型/迁移、新建路由、改登录语义）→ 判 FAIL 并明确标注范围溢出。
- 全部通过 → 报告 SUCCESS。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留。

## 五、交付报告格式

```
RND-294 验收结论：PASS / FAIL
前置：A7-1 AuditLog 已合并 __（是/否）；alembic check __（绿/红）
写入钩子：B1/B2/B3/B4/B5 PASS/FAIL
架构边界：C0 app.audit 入 allowlist __；C0b 边界测试 __（过/红）
接入：A1 wecom_callback __；A2 auth_logout __；A3 password_login 协调状态 __；A4 语义零改动 __
测试：T1 PASS/FAIL
范围守门：diff 文件清单 __；G2 无模型/迁移改动 __；G3 路由数不变 __；G4 登录语义零改动 __
回归：make verify __（绿/红）；alembic check __（绿/红）
契约：未 commit __
遗留：__
```
