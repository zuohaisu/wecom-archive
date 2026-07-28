# RND-295 QA / 验收 agent 提示词 —— A7-3 审计列表/筛选/分页 API

> 面向独立测试/QA agent。只读言、不改实现、不 commit/push。
> 验收对象：开发 agent 按 `rnd-295-execution-prompt.md` 产出的改动。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS。

## 一、验收目标

确认 RND-295（A7-3）达成 Linear 验收标准且零回归：
- **可筛选分页**：`GET /api/admin/audit-logs` 按动作/时间/操作人/关键词筛选，`limit`/`offset` 分页，返回 `total` + `has_more`。
- **不可篡改（只读）**：端点仅 `GET`，无增/删/改路径；返回的是 A7-1 既存不可变行的只读投影。
- **租户隔离**：仅返回当前会话租户的审计行；跨租户物理不可见。
- **角色门禁**：所有管理员角色（含 `readonlyaudit`）可读；未认证 401。
- **契约不变**：无 schema 变更（`alembic check` 绿）、`test_http_contract.py` 仅增量更新、架构边界 PASS、无新依赖。

前置：A7-1（RND-293）已落地（否则开发 agent 应已停下报告，本票无可验收内容）。

## 二、逐条验收清单（PASS/FAIL，附证据）

### 后端 — 端点行为（核心验收）
- [ ] B1 同租户写入 N 条 `AuditLog` 后 `GET /api/admin/audit-logs`（无筛选）→ 200；`total == N`、`items` 长度 == min(N, default_limit=50)；`created_at` **降序**（最新在前，对齐设计稿）；`has_more` 按 `offset+limit < total` 正确。 —— 证据：pytest + DB 断言。
- [ ] B2 **租户隔离**：第二租户写入 M 条后，当前租户查询 `total` 仍为 N（跨租户不可见）；构造跨租户 id 不作为过滤输入（tenant 仅来自会话）。 —— 证据：pytest 建两租户断言。
- [ ] B3 **动作筛选**：`?action=export`（或 `?action=view,search` 多值逗号）→ 仅返该 action 行；其它 action 被排除。 —— 证据：pytest。
- [ ] B4 **操作人筛选**：`?operator=system` → 仅返 `admin_user_id IS NULL` 的系统动作；`?operator=<某 id>` → 仅返该 actor；混合数据下互斥正确。 —— 证据：pytest。
- [ ] B5 **时间窗**：`?from=ISO&to=ISO` → 仅返 `created_at` 落在区间内的行（含边界）；区间外被排除。 —— 证据：pytest（构造不同 created_at 行）。
- [ ] B6 **关键词**：`?q=<object_id 片段>` / `?q=<actor name>` / `?q=<actor email>` / `?q=<audit id 片段>` → ILIKE 命中对应行；无关行不返。 —— 证据：pytest。
- [ ] B7 **分页**：`?limit=2&offset=0` → `items` 长度 2、`has_more=True`；`?offset=2` 取后半；`limit` 超 200 或 <1 → 422（pydantic `ge/le`）；`offset` <0 → 422。 —— 证据：pytest。
- [ ] B8 **响应形状**：每条含 `id`/`actor_name`(可为 None)/`action`/`object_type`/`object_id`(可 None)/`detail`(dict 或 None)/`created_at`(ISO 8601 字符串)；外层 `AuditLogListOut` 含 `items`/`total`/`limit`/`offset`/`has_more`。系统动作 `actor_name` 为 None（非报错）。 —— 证据：pytest 断言 schema。
- [ ] B9 **角色门禁**：以 `readonlyaudit` 角色调用 → 200（审计读取角色本应可读）；`owner`/`admin`/`compliance`/`legal` 均 200；未认证 → 401（先 401 后 403 由 `require_role` 保证）。 —— 证据：pytest。

### 后端 — 安全与契约（不可篡改）
- [ ] S1 **只读、无副作用**：`GET /api/admin/audit-logs` 调用前后 `audit_logs` 行数不变（无 INSERT/UPDATE/DELETE）；路由仅注册 `GET` 方法（`app.routes` 中该 path 的 `methods == {"GET"}`）。 —— 证据：pytest 前后 count 断言 + grep 路由定义确认无 `@router.post/patch/delete`。
- [ ] S2 **租户隔离（硬）**：目标查询 `AuditLog.tenant_id == 会话租户`（读 `routers/audit.py:list_audit_logs` 确认 filter 含 `tenant_id`）；无任何请求参数可注入 tenant。 —— 证据：读源码 + B2。
- [ ] S3 **零内容泄露回归**：抽样 `detail` 字段 `grep`/`json` 断言不含消息正文 / `decrypted_payload` / `msgid` / `content_text`（A7-1 已保证，此处护栏）。 —— 证据：pytest 检查返回 detail。
- [ ] S4 不返回 `record_hash` / 不暴露任何哈希链字段（设计稿 seal 属 UI，非本票；A7-1 schema 无该列）。 —— 证据：读源码 + 响应字段断言。

### 全局契约
- [ ] C1 **无 schema 变更**：`alembic check` 绿；`git diff` 不含 `app/db/models.py`/`alembic/versions/` 改动。 —— 证据：命令 + `git diff`。
- [ ] C2 **架构边界**：`backend/tests/test_architecture_boundary.py` PASS；`app/routers/audit.py` 未 `import app.routers.*`/`app.main`；`app/main.py` 仅新增 `include_router`，无内联路由。 —— 证据：`make verify` + grep。
- [ ] C3 **HTTP 契约同步**：`test_http_contract.py` 三处已更新且 `make verify` 绿 —— `route_count` 改为实际值（L325）、path 集合含 `/api/admin/audit-logs`（L334-377）、snapshot 列表含 `("/api/admin/audit-logs", frozenset({"GET"}), "AuditLogListOut", "None")`（L403-492）。 —— 证据：`make verify` + 读测试。
- [ ] C4 **既有路由不变**：`reachability_audit`/`password_login`/`wecom_*`/`auth_me`/`sync` 等 URL 与行为不变；`app/audit.py`(A7-2) 未被本票改动（本票不依赖 A7-2）。 —— 证据：`git diff` 仅含新增 + 契约测试更新；既有测试全绿。
- [ ] C5 `make verify` 全绿（含新增 `test_rnd295_audit_list.py`）。 —— 证据：命令输出。
- [ ] C6 **无新第三方依赖 / 不碰 B 层**：`requirements.txt`、`.env.example`、systemd、`deploy.yml`、`backend/scripts` 未改动。 —— 证据：`git diff`。

## 三、回归套件（必须全绿）

`make verify` 全绿，重点确认：
- `backend/tests/test_architecture_boundary.py`
- `backend/tests/test_http_contract.py`（本票已同步更新）
- `backend/tests/test_password_auth.py`（F0-2 契约）
- `backend/tests/test_auth.py`、`test_rnd225_auth_fail_closed.py`
- `backend/tests/test_verify_alembic_head.py`、`test_makefile_lint_diff.py`
- `backend/tests/test_i18n_foundation.py`
- 新增 `backend/tests/test_rnd295_audit_list.py`（见第二节 B1-B9、S1-S4）

DB 支撑测试用 `DATABASE_URL` 门控；无 DB 时相关用例自动 skip，但 SQLAlchemy mock 路径（B1-B9 中可无 DB 运行的部分）必须可无 DB 运行（参照 `test_password_auth.py` 的 `dependency_overrides[get_db]` 工厂模式）。

## 四、智能路由判定（每轮必给）

- **源码有 Bug**（如跨租户未隔离、筛选失效、分页 total/has_more 错、只读被破坏出现写路径、契约测试未更新导致 route_count 不符、角色门禁缺失）→ 反馈开发 agent 修复，附：失败用例/错误栈 + 期望行为 + 相关文件:行号；不自行改实现。
- **测试代码有 Bug**（如断言旧路径、mock 缺字段）→ 可自行修正测试，仅当断言已过期路径时须显式标注「修正测试而非实现」。
- **全部通过** → 报告 SUCCESS，附 RED→GREEN 对比。
- 最多 2 轮：第 1 轮修复，第 2 轮回归；仍不过则标注遗留并交回开发 agent。

## 五、RED→GREEN 记录要求

- RED（改前基线）：`app/routers/audit.py` 不存在；`/api/admin/audit-logs` 404；`route_count` = 42；`AuditLog` 是否可 import 视 A7-1 是否先合。
- GREEN（改后）：B1-B9、S1-S4、C1-C6 全 PASS；`alembic check` 绿；`make verify` 绿。
- 量化：B1 断言 `total` 与插入数相等；B7 断言 `has_more` 逻辑；其余为布尔 PASS。

## 六、交付报告格式

```
RND-295 验收结论：PASS / FAIL
A7-1 前置：已落地 / 未落地（未落地则无验收内容）
迁移 head：<alembic heads 输出>  alembic check：绿/红
核心验收：
  - 可筛选（动作/操作人/时间/关键词）：PASS/FAIL（证据 B3-B6）
  - 分页+total+has_more：PASS/FAIL（证据 B1/B7）
  - 租户隔离：PASS/FAIL（证据 B2/S2）
  - 角色门禁（含 readonlyaudit）：PASS/FAIL（证据 B9）
  - 不可篡改（只读无副作用）：PASS/FAIL（证据 S1）
安全：零内容泄露 / 无哈希链越界：PASS/FAIL（S3/S4）
契约：alembic 无变更 / http_contract 增量更新 / 架构边界 PASS / 无新依赖：PASS/FAIL
回归：make verify ___（绿/红）
遗留：___
```
