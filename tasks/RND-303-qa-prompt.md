[Goal check] This work advances 独立验收（QA） by 逐条核对 RND-303 的 8 条 AC（重点验证批量端点真复用 `invite_user` 核心逻辑、单条失败隔离、`invite_user` 零回归）并产出带证据的 PASS/FAIL 判定。

# RND-303 验收提示词（Acceptance / QA Prompt）

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；只读操作不需要许可，直接开始下面的验收步骤。唯一允许中途停下、不产出 PASS/FAIL 的情况，是触发规则要求的 `BLOCKED`——这是写进产出文件的判定，不是向用户提问。

---

> 交给**独立验收 agent**。只做验证与判定，**不修改任何文件**。

## 任务身份
- 工单：RND-303「A9-2 邀请合规团队」｜风险等级 R1
- 本票新增 1 个端点，核心风险是"是否真复用了 A3-2 的邀请逻辑"以及"批量场景下单条失败会不会拖累整批"。

## 你的角色与权限
- 可以：读所有文件、跑只读命令。
- 不可以：改任何文件、commit、push、放松 AC。
- 缺口 → FAIL 并列具体缺口，**不替开发 agent 补做**。

## 验收方法（证据优先）

### AC-1 — 批量邀请可用
- 证据：测试断言多条合法记录一次提交 → 每条创建挂起账号 + 触发发邮件；响应含逐条 `{email, ok: true}`。
- 判定：功能齐全 = PASS。

### AC-2 — 单条失败隔离
- 证据：测试构造一批含 1 条非法 `role`、其余合法 → 断言合法条目仍被创建，非法条目返回 `{ok: false, error: "invalid_role"}`，**整体 HTTP 状态码仍为 200**（不是因一条错就 400 整批）。
- 判定：隔离行为符合 = PASS。**若一条失败导致整批回滚/整体 4xx → FAIL（`IMPLEMENTATION_DEFECT`, major）。**

### AC-3 — 复用而非重写（关键）
- 证据：`grep -n "_create_pending_invite" backend/app/routers/auth.py` **应 ≥ 2 处命中**（helper 定义 + 至少 `invite_user` 一处调用）。
- 代码审阅：确认批量端点内部是**循环调用该 helper**，而不是另写一套角色校验/token 生成/邮件发送逻辑。
- 判定：真复用 = PASS。**若批量端点平行实现了一套邀请创建逻辑 → 直接 FAIL（`IMPLEMENTATION_DEFECT`, severity: major）**——这会导致未来 A3-2 改邀请规则时只改了一处，另一处悄悄漂移。

### AC-4 — `invite_user` 零回归
- 证据：`git diff -- backend/app/routers/auth.py` 逐行核对 —— `invite_user` 的**对外**行为（URL `/api/admin/users/invite`、请求体字段、响应体 `{"ok": true}`、状态码、错误码 `invalid_role`/`wecom_user_id_or_email_required`）**必须完全不变**；允许的改动仅限于"把内联逻辑挪进 helper 再调用它"。
- 证据补充：覆盖 `invite_user`/`accept_invite` 的既有测试全绿。
- 判定：对外行为不变 + 既有测试绿 = PASS。**对外行为有任何变化 → FAIL（`REGRESSION`, blocker）。**

### AC-5 — 批量上限
- 证据：测试断言超过上限条数（如 21 条，若实现上限为 20）→ 400，明确错误信息；不接受无界批量被静默接受。
- 判定：有上限 + 有测试 = PASS。无上限 → FAIL（`IMPLEMENTATION_DEFECT`, minor，记入 findings 但不必是 blocker——除非完全没有任何上限机制，视具体实现严重性判定）。

### AC-6 — 租户隔离
- 代码审阅：批量端点的 `tenant_id` 只来自 `get_current_user()` 会话解包，**不接受**请求参数中的 `tenant_id`。
- 判定：符合 = PASS。请求体/查询参数可覆盖 `tenant_id` → FAIL（`SECURITY_VIOLATION`, blocker）。

### AC-7 — 契约同步
- 背景：本票新增 1 个路由 → 必须同步 `test_http_contract.py`。
- 判定：全绿 = PASS。**新增路由但未同步 → FAIL（`REGRESSION`）**，`recommended_next_state: FIXING`（不是 `BLOCKED_NEEDS_HUMAN`，不建议另开维护票）。

### AC-8 — 回归
- 证据：`make verify` exit 0；`test_architecture_boundary.py` 通过。
- 判定：全部 exit 0 = PASS。

## 本项目专属检查（必查）
1. **未新建 `routers/onboarding.py`**：`git status --porcelain` 中不得出现该文件——它被 RND-304（同 Epic 兄弟票，目前仍 Blocked）预定。出现 → FAIL（`SCOPE_VIOLATION`），会与 RND-304 未来的新建操作冲突。
2. **未新增角色**：diff 中不得出现在 `{"owner","admin","compliance","legal","readonlyaudit"}` 之外的新角色值。若确有新增角色的需求，应是 `BLOCKED_NEEDS_HUMAN`（产品决策），不是 agent 自行拍板。
3. **未改邮件模板内部实现**：`git diff --stat -- backend/app/email.py` 应无输出或仅有微小改动（只调用不重写）。
4. **架构边界**：`routers/auth.py` 未 import `app.main`。
5. **文件所有权**：`git status --porcelain` 中属于本票的改动应限于 `routers/auth.py`（helper 提炼 + 新端点）、`tests/test_onboarding_invite_batch.py`（新）、可选 `tests/test_http_contract.py` / `tests/test_rnd280_rbac_scaffold.py`。
   > 共享工作树可能含他票在途改动（见 `docs/ticket-autopilot-workflow.md` §3.4）——先 `git status` 分离归因。

## 附加检查（Security）
- 测试中使用固定假邮箱，非真实客户/员工邮箱。
- `git log origin/main..HEAD` **应为空** → 有输出即 FAIL。

## 验证命令（只读）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_onboarding_invite_batch.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -n "_create_pending_invite" backend/app/routers/auth.py   # AC-3：应 ≥ 2 处
git diff -- backend/app/routers/auth.py                        # AC-4：人工核对 invite_user 对外行为不变
git status --porcelain                                         # 含 AC-1 所有权检查
git log origin/main..HEAD                                      # 必须无输出
```

## 产出
写入 `tasks/RND-303-qa-verdict.json`，遵循 `tasks/_templates/qa-verdict.schema.json`。
`notes` 中记录：批量端点的确切请求/响应结构、批量上限具体数值（供前端向导页对接）。

## 禁止事项
- 不改任何文件、不补做缺失内容、不放松 AC。
- **AC-3 若发现平行实现一套邀请逻辑 → 直接 FAIL**，不接受"功能上也能跑"。
- **AC-4 若 `invite_user` 对外行为有任何变化 → 直接 FAIL**，那会波及已上线的 A3-2 邀请流程。
