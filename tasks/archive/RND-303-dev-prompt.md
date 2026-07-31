[Goal check] This work advances 开发（Development） by 交付「首次配置向导」批量邀请合规团队的后端能力，复用 RND-285 已上线的单人邀请原语，不重造邀请/令牌/邮件逻辑。

# RND-303 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-303 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-303「A9-2 邀请合规团队」｜父 Epic RND-269（A9 首次配置向导）
- 优先级：High｜风险等级：**R1**（新增端点，但复用已有邀请原语，无新数据模型）｜milestone：R2 · 开源发布闭环

## 背景与现状（已实地核实）

前置 A3-2（<issue>RND-285</issue>，邀请流程）**已 Done 并已上线**：`backend/app/routers/auth.py:312` 的
```python
@router.post("/api/admin/users/invite")
def invite_user(body: _InviteBody, current=Depends(get_current_user), db=Depends(get_db)):
    if body.role not in {"owner", "admin", "compliance", "legal", "readonlyaudit"}:
        raise HTTPException(status_code=400, detail="invalid_role")
    ...
```
**角色白名单里已经有 `compliance` / `legal` / `readonlyaudit`**——设计稿（`design/Crowntime WeCom Archive Design System/pages/onboarding-invite.html:52-53`）里"合规管理员/法务/只读审计"三个选项，逻辑上**已经被 A3-2 的角色体系完全覆盖**，本票**不需要新角色、不需要新的邀请/令牌/邮件逻辑**。

设计稿里向导的邀请步骤是**一次填多行**（多个 email + role），逐行提交单人邀请接口在体验上可行，但会让前端处理"部分失败"（如某一行 role 非法、某一行邮箱重复）变得笨拙。本票的净新增范围因此收窄为：**一个批量邀请端点，内部逐条复用 `invite_user` 的创建逻辑**，返回逐条结果，不在前端做拆分请求、不在后端重写邀请创建逻辑。

**❗ 本项目高频踩坑：**
- **不建新文件**：`backend/app/routers/onboarding.py` 已被 RND-304（A9-3，同一 Epic 下的兄弟票）预定为其新建文件，RND-304 目前仍 Blocked（等 F0/RND-244 配置中心）尚未创建该文件。**为避免与 RND-304 未来创建同名文件冲突，本票不新建 `routers/onboarding.py`，批量邀请端点加在既有 `backend/app/routers/auth.py` 里，紧邻 `invite_user`。**
- **不要重写邀请核心逻辑**：`invite_user` 里"角色校验 → 去重挂起邀请 → 生成 token → 发邮件"这一整套必须被**提炼成一个私有 helper**（如 `_create_pending_invite(db, *, tenant_id, admin_user_id, email, name, role, wecom_user_id=None)`），由 `invite_user` 和新的批量端点**共同调用**——不是复制粘贴一份平行实现。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个批量邀请端点，让首次配置向导能一次性邀请多名合规团队成员（合规管理员/法务/只读审计等角色），单条失败不拖累其余条目，且复用 A3-2 已有的邀请核心逻辑。

## 范围边界

**In scope：**
1. `backend/app/routers/auth.py` 新增 `POST /api/admin/users/invite-batch`：
   - 请求体：邀请列表，每条含 `email`（必填）、`role`（必填，同 `invite_user` 白名单）、`name`（可选）。
   - 从 `invite_user` 中**提炼**共享 helper（角色校验 / 去重挂起 / token 生成 / 发邮件），`invite_user` 改为调用该 helper（保持其对外行为、URL、响应体完全不变）。
   - 逐条独立成败：一条的 `HTTPException`（如 `invalid_role`）**不得**中断其余条目；返回体包含每条的 `{email, ok, error?}`。
   - 批量条数上限（建议 20 条/次），超限 → 400，防止滥用。
   - 鉴权：与 `invite_user` 一致，`tenant_id` 只来自 `get_current_user()` 会话，不接受请求参数。
2. 测试：`backend/tests/test_onboarding_invite_batch.py`（新）。

**Out of scope（显式非目标）：**
- 不新建角色（`compliance`/`legal`/`readonlyaudit` 已存在于 A3-2）。
- 不新建 `routers/onboarding.py`（见上，留给 RND-304）。
- 不做前端向导页面（属其他票）。
- 不改邮件模板 / `send_invite_email` 内部实现。
- 不做完整租户开通（B2）。

**本工单拥有的文件（只许写这些）：**
- `backend/app/routers/auth.py` —— **仅新增** `_create_pending_invite` helper + `POST /api/admin/users/invite-batch`；**必须**将 `invite_user` 的函数体改为调用该 helper（重构范围仅限"提炼 helper"，不改 `invite_user` 的对外行为/响应/状态码）
- `backend/tests/test_onboarding_invite_batch.py`（新）
- `backend/tests/test_http_contract.py` —— **仅当**新增路由需要（强制随附，route_count 读当前基线 +1）
- `backend/tests/test_rnd280_rbac_scaffold.py` —— **仅当**触发该白名单时才碰（`invite_user`/`invite-batch` 走的是 `get_current_user`，不是 `require_role`，预期不触发；实现后跑一遍确认）

**只读、绝不可写：** `app/email.py`（只调用 `send_invite_email`）、`app/db/models.py`、`app/routers/onboarding.py`（不存在，不得创建）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 批量邀请可用**：`POST /api/admin/users/invite-batch` 传入多条合法记录 → 每条创建挂起账号 + 发邀请邮件，响应含逐条 `{email, ok: true}`。
- **AC-2 单条失败隔离**：批量中某条 `role` 非法（不在白名单）→ 该条 `{email, ok: false, error: "invalid_role"}`，**其余合法条目仍正常创建**，整体请求返回 200（不是因为一条错就整批 400）。
- **AC-3 复用而非重写（关键）**：`grep -n "_create_pending_invite" backend/app/routers/auth.py` 命中 ≥ 2 处（helper 定义 + `invite_user` 调用处）；`invite_user` 函数体**不再**内联角色校验/token 生成/发邮件逻辑，而是调用该 helper。**若批量端点另写了一套平行的创建逻辑 → FAIL。**
- **AC-4 `invite_user` 零回归**：既有 `test_auth.py`（或覆盖 `invite_user`/`accept_invite` 的测试）全绿，URL/请求体/响应体/状态码均不变。
- **AC-5 批量上限**：超过上限条数 → 400，明确错误信息；不接受无界批量。
- **AC-6 租户隔离**：`tenant_id` 仅来自 `get_current_user()` 会话（同 `invite_user` 既有模式），不接受请求参数覆盖。
- **AC-7 契约同步**：新增 1 个路由 → `test_http_contract.py` 已同步（`route_count` 当前基线 +1、expected/snapshot 追加）。见 `docs/ticket-autopilot-workflow.md` §3.3。
- **AC-8 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_onboarding_invite_batch.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_rnd280_rbac_scaffold.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
grep -n "_create_pending_invite" backend/app/routers/auth.py    # AC-3：helper 定义 + invite_user 调用处，≥ 2 处命中
git diff -- backend/app/routers/auth.py    # 人工核对：invite_user 对外行为不变，只是内部改调 helper
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-285（A3-2 邀请流程）**已 Done 并已核实落地**（`backend/app/routers/auth.py:312`）。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-8 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明批量端点的请求/响应结构（供前端向导页对接）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：提炼 helper 时改坏 `invite_user` 既有行为（URL/响应体/状态码）——由 AC-4 防守。
- 风险：批量端点无上限被滥用发邮件——由 AC-5 防守。
- 回滚：纯新增 + 局部重构，`git checkout -- backend/app/routers/auth.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿 + `git diff` 确认 `invite_user` 行为未变 即可，无需额外人工审阅（R1，非鉴权边界改动）。
- **Escalation**：若发现 `invite_user` 的现有实现无法在不改对外行为的前提下提炼出干净的共享 helper（例如夹杂了太多和"单条"强耦合的逻辑）→ `BLOCKED_NEEDS_HUMAN`，说明具体耦合点，**不要**为了硬提炼而改变 `invite_user` 的对外行为。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/routers/auth.py`（重点：`invite_user` @312、`accept_invite` @369、`_InviteBody`）。
2. 从 `invite_user` 提炼 `_create_pending_invite` helper，`invite_user` 改为调用它（对外行为不变）。
3. 新增 `POST /api/admin/users/invite-batch`，循环调用 helper，逐条捕获异常，拼装逐条结果。
4. 写测试覆盖 AC-1~AC-6。
5. 跑 `test_rnd280_rbac_scaffold.py` 确认未触发（`get_current_user` 路径，非 `require_role`）。
6. 同步 `test_http_contract.py`。
7. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假邮箱/凭据。
- 不扩大 Scope：不新建角色、不新建 `routers/onboarding.py`、不做前端。
- 复用优先：`invite_user` 的核心逻辑只提炼不重写。
- 证据优先，以 exit 0 / 测试通过为证。
