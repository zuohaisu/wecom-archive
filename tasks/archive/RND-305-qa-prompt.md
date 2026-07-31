[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-305 的 7 条 AC（重点验证租户管理员不得升格、租户内鉴权零回归、跨租户访问必留痕）并产出带证据的 PASS/FAIL 判定。

# RND-305 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**（默认 Codex）。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-305「B1-2 跨租户鉴权作用域」｜风险等级 **R2（鉴权与权限边界）**
- **这是全站权限模型的改动，且 `auth.py` 是全站鉴权中枢（爆炸半径最大的文件之一）。AC-2 与 AC-4 从严判定，不接受任何"应该没问题"式论证。**

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 依赖可用
- 证据：合法平台超管凭据通过 `require_platform_admin`；缺凭据 → 401；错凭据 → 401。三种情况均有用例。
- 判定：三个用例齐全且通过 = PASS。

### AC-2 — 租户内零回归（**关键，爆炸半径最大**）
- 证据：`pytest backend/tests/test_auth.py backend/tests/test_admin_users_api.py -q` **全绿**；audit / media_library 相关既有测试全绿。
- **代码审阅（重点）**：`git diff -- backend/app/auth.py` —— 确认 diff 中**只有新增的 `require_platform_admin` 及其辅助**，`require_role` / `verify_platform_admin` / 既有会话解析逻辑**逐行未变**。
- 背景：`auth.py` 是全站鉴权中枢，所有租户 API 都依赖它。改坏了会同时打断所有租户的访问。
- 判定：既有测试全绿 + diff 仅含新增 = PASS。**若 `require_role` 或会话逻辑被改动 → FAIL（`SCOPE_VIOLATION`, severity: blocker）。**

### AC-3 — 跨租户访问必写审计
- 证据：测试断言超管跨租户读取后 `audit_logs` 新增一条，含超管身份标识 + 目标 `tenant_id`。
- 代码审阅：`grep -n "write_audit\|from app.audit import" backend/app/auth.py` 应有命中——**必须调用既有 `app/audit.py:54` 的 `write_audit`，不是自造插入**。
- 反模式：确认审计调用**没有**被额外 `try/except: pass` 包住（`write_audit` 本身已 fail-safe，再包一层会掩盖问题）。
- 判定：经 `write_audit` + 字段齐全 + 无多余吞异常 = PASS。

### AC-4 — 普通租户管理员不得升格（**最高风险项，权限提升的唯一防线**）
- 证据：**必须存在**反例测试——构造持租户内 `admin` 或 `owner` 角色的会话，尝试通过 `require_platform_admin`，断言**被拒绝**（401/403）。
- 背景：若这条不成立，任何租户的管理员都能跨租户读取全部客户数据——这是本产品最严重的可能漏洞（合规存档产品，存的是全部聊天记录）。
- 判定：反例测试存在且通过 = PASS。**缺该测试 → 直接 FAIL（`INSUFFICIENT_TEST_COVERAGE`, severity: blocker）**，不接受"代码逻辑上不会通过"这类未经验证的断言。若测试存在但实际能通过（即真的可升格）→ FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-5 — 例外语义有文档
- 证据：代码注释明确说明——① 为何此处允许从请求参数取 `tenant_id`；② 两道保险是什么（身份已验证为平台超管 + 每次写审计）；③ **明确标注这是对"tenant_id 绝不来自请求参数"铁律的有意例外，不得照抄到租户内 API**。
- 背景：没有这段注释，后来者要么把它当 bug"修掉"，要么照抄这个模式到租户 API 从而全线失守。
- 判定：三点都写到 = PASS。仅有"这里取 tenant_id"这类无解释注释 = FAIL（`IMPLEMENTATION_DEFECT`, minor 但必须记）。

### AC-6 — 契约同步（若适用）
- 若本票**新增了路由**：`test_http_contract.py` 必须已同步（`route_count` 为当前基线 +1、expected/snapshot 追加）。未同步 → FAIL（`REGRESSION`），`recommended_next_state: FIXING`（**不要**建议另开契约维护票）。
- 若本票**触发了 RBAC 白名单**（`test_rnd280_rbac_scaffold.py`）：须已同步。
- 若本票**未新增路由**（只交付依赖原语）：此条 `NOT_APPLICABLE`，在 verdict 中如实标注。
- 判定：按实际情况判定；契约测试绿 = PASS。

### AC-7 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **未改审计基础设施**：`git diff --stat -- backend/app/audit.py` **必须无输出**。
2. **未改模型 / 无迁移**：`git diff --stat -- backend/app/db/models.py` 无输出；`backend/alembic/versions/` 无新文件。
3. **未越界做下游票**：diff 中不得出现跨租户聚合业务逻辑（RND-307）、租户启停（RND-310）、内容访问 gate（RND-309）。本票只交付鉴权原语。出现 → FAIL（`SCOPE_VIOLATION`）。
4. **架构边界**：`app.auth` 未 import `app.routers.*` 或 `app.main`（它是扁平域模块，反向依赖会触发硬闸）。
5. **过渡认证有替换点**：若用 HTTP Basic 作过渡，确认代码里是**单一收敛点**（未来换 session cookie 只需改一处），且有注释说明。非硬性 FAIL 条件，但应在 `notes` 记录。
6. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `app/auth.py`（仅新增）、`tests/test_platform_auth_scope.py`（新）、可选 `tests/test_rnd280_rbac_scaffold.py` / `tests/test_http_contract.py`（契约同步）、可选文档。
   > 共享工作树可能含他票在途改动（见 `docs/ticket-autopilot-workflow.md` §3.4）——先 `git status` 分离归因，只把本票的部分记在本票账上。

## 附加检查（Security）
- 无真实凭据 / 密码 / 密钥进入代码或测试固定值。
- 确认 `require_platform_admin` 的失败路径**不泄露信息**（错凭据与不存在的账号应返回相同的 401，不要区分"账号不存在"与"密码错误"）。若可区分 → 记 finding（`SECURITY_VIOLATION`, minor）。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_platform_auth_scope.py -q
.venv/bin/python -m pytest backend/tests/test_auth.py backend/tests/test_admin_users_api.py -q   # AC-2
.venv/bin/python -m pytest backend/tests/test_rnd280_rbac_scaffold.py backend/tests/test_http_contract.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/auth.py     # AC-2：逐行核对，只应有新增
grep -n "write_audit" backend/app/auth.py   # AC-3
git diff --stat -- backend/app/audit.py backend/app/db/models.py   # 必须全无输出
git status --porcelain
git log origin/main..HEAD                    # 必须无输出
```

## 产出
写入 `tasks/RND-305-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：过渡期认证方式与替换点位置、跨租户 `tenant_id` 的传入参数名、审计记录字段结构（供 RND-307/309/310 对接）。

## 禁止事项
- 不改任何文件、不补做缺失实现或测试、不放松 AC。
- **AC-4 若无「租户管理员不得升格」的反例测试 → 直接 FAIL（blocker）。** 这是权限提升的唯一防线，不接受任何形式的"逻辑上不会发生"。
- **AC-2 若 `auth.py` 的 diff 触及 `require_role` 或既有会话逻辑 → 直接 FAIL。** 那会波及全站所有租户 API。
