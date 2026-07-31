[Goal check] This work advances 开发（Development） by 交付租户开通后向首位管理员自动发送激活邀请的能力，复用 RND-303 提炼出的邀请创建原语，不重写。

# RND-313 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-313 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## ⚠️ 开工前必须先核实的前置条件（本票的真实阻塞点）

本票的 Linear 依赖写的是 B2-1（<issue>RND-311</issue>，已 Done）+ A3-2（<issue>RND-285</issue>，已 Done），**但这两个都只是必要条件，不是充分条件**。本票真正要调用的创建逻辑是 <issue>RND-303</issue>（A9-2 批量邀请）计划从 `invite_user` 提炼出的纯函数 `_create_pending_invite(db, *, tenant_id, admin_user_id, email, name, role, wecom_user_id=None)`（位置：`backend/app/routers/auth.py`）。

**原因**：`invite_user`（`auth.py:312`）本身的鉴权依赖是 `get_current_user()`——要求调用方已有一个**租户内**的登录会话。但本票触发时机是"平台超管刚创建完一个全新租户"，这个租户**还没有任何管理员账号**，根本不存在可用的租户会话去调用 `POST /api/admin/users/invite`。所以本票不能直接打那个 HTTP 端点，必须在**函数层面**复用创建挂起账号的核心逻辑——而这个可直接调用的纯函数版本，要等 RND-303 把它从 `invite_user` 里提炼出来才存在。

**开工第一步，必须先跑：**
```bash
grep -n "_create_pending_invite" backend/app/routers/auth.py
```
- **若无命中**（RND-303 尚未落地）→ **停止**，`BLOCKED_NEEDS_HUMAN`，写明"依赖 RND-303 的 `_create_pending_invite` 尚未提炼，无法在不重写邀请创建逻辑的前提下继续"。**不要**为了不等待而自己在 `platform.py` 或别处重写一套平行的邀请创建逻辑（去重挂起 / token 生成 / 角色校验）——那会导致两份逻辑今后各自漂移。
- **若有命中** → 继续下面的正常实现流程。

## 任务身份
- 工单：RND-313「B2-3 激活邮件（建 AdminUser 邀请）」｜父 Epic RND-270（B2 租户开通）
- 优先级：Medium｜风险等级：**R1**｜milestone：R5 · 云商业化前台

## 背景与项目现状（已实地核实）

B2-1（<issue>RND-311</issue>，创建租户）已 Done：`app/routers/platform.py:23` 的 `create_tenant`，创建 `Tenant` + `TenantWecomConfig` 后返回 `TenantProvisionOut`（**不含**任何管理员邮箱字段——当前 `TenantProvisionIn` 里也没有"首位管理员邮箱"这个输入，见下方 Scope 里需要新增的字段）。

A3-2（<issue>RND-285</issue>，邀请流程）已 Done：邮件发送用 `app/email.py` 的 `send_invite_email(email, accept_link)`，`accept_link` 用 `app/settings.py` 的 `get_email_settings().invite_base_url`（或 `get_wecom_oauth_settings().admin_domain` 兜底）拼 `/admin/accept-invite?token=...`。

**❗ 本项目高频踩坑：**
- **`platform.py` 是本波次共享文件**：RND-307/310/312/314 也会往这个文件加端点/改动 `create_tenant`。开始前先 `git diff`/`git status` 看清哪些兄弟票的端点已落地。`test_http_contract.py` 的 `route_count` 用**当前实际值** +1 回填。
- **本票需要修改 `create_tenant` 的入参/流程**（新增"首位管理员邮箱"字段 + 创建后自动发邀请），这与 RND-307/310/312/314 纯新增端点不同——**改动 `create_tenant` 前务必 `git diff` 确认没有兄弟票也在改它**，若冲突，本票的改动优先级更高（因为改的是 `create_tenant` 本身），但仍要如实记录在 QA Summary。
- 架构冻结 D1：纯后端。

## 目标（Goal）
租户创建成功后，自动为其"首位管理员"建一个挂起邀请账号并发送激活邮件，复用 RND-303 提炼出的邀请创建原语，不重写。

## 范围边界

**In scope：**
1. `backend/app/schemas/tenant_provision.py`：`TenantProvisionIn` **新增**必填字段 `owner_email: EmailStr`（首位管理员邮箱），`TenantProvisionOut` **新增** `owner_invite_sent: bool`。
2. `backend/app/routers/platform.py` 的 `create_tenant`：**在** `db.commit()` **成功之后**，调用 `_create_pending_invite(db, tenant_id=tenant.id, admin_user_id=None, email=payload.owner_email, name=None, role="owner")`（`admin_user_id=None` 表示系统发起，非某个已登录管理员发起——若 `_create_pending_invite` 的签名不接受 `None`，按实际签名调整，且在 QA Summary 里说明）。
3. 测试：`backend/tests/test_rnd313_tenant_activation_email.py`。

**Out of scope（显式非目标）：**
- 不改 `_create_pending_invite` 本身（只调用）。
- 不做接受邀请后的租户内首次登录引导（属 <issue>RND-269</issue> onboarding）。
- 不做连通性自检 / 列表 / 启停 / 聚合（其他票）。
- 不允许多个首位管理员（本票只支持创建时指定恰好一个 `owner`）。

**本工单拥有的文件（只许写这些，`platform.py` 为共享追加/修改）：**
- `backend/app/routers/platform.py` —— **修改** `create_tenant`（追加"创建成功后发邀请"这一步，其余逻辑不变）
- `backend/app/schemas/tenant_provision.py` —— 新增 `owner_email` / `owner_invite_sent` 字段
- `backend/tests/test_rnd313_tenant_activation_email.py`（新）
- `backend/tests/test_rnd311_tenant_provision.py` —— **仅当**新增必填字段导致既有测试的请求体需要补字段时才碰，只补字段不改断言逻辑
- `backend/tests/test_http_contract.py` —— 契约同步（若 `TenantProvisionOut` 响应形状变化需要更新 snapshot）

**只读、绝不可写：** `app/routers/auth.py`（只 import `_create_pending_invite`，不修改该文件）、`app/email.py`（只调用）、`app/db/models.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-0 前置核实**：开工前已确认 `_create_pending_invite` 存在于 `backend/app/routers/auth.py`（见上方「开工前必须先核实」）。
- **AC-1 创建即发邀请**：`POST /tenants` 传入 `owner_email` → 租户创建成功后，一条 `AdminUser`（`role="owner"`, `invite_status="pending"`）被创建，且 `send_invite_email` 被调用（测试可 mock 该函数断言被调用一次，参数含目标邮箱）。
- **AC-2 复用而非重写（关键）**：`grep -n "_create_pending_invite" backend/app/routers/platform.py` 应有命中；`create_tenant` **不得**内联重写角色校验/去重/token 生成/发邮件的任何一步。
- **AC-3 邀请失败不回滚租户创建**：若发邀请步骤抛异常（如邮件服务不可用），**租户和 `TenantWecomConfig` 仍应创建成功**（`db.commit()` 已在发邀请之前完成，见 Scope 第 2 点的顺序要求），响应体 `owner_invite_sent=false`，不是 500。须有测试覆盖这一场景（mock `send_invite_email` 抛异常）。
- **AC-4 `create_tenant` 既有行为零回归**：不传 `owner_email`（若做成必填，测试改为覆盖"缺字段 → 422"）；已有的租户创建成功路径（`test_rnd311_tenant_provision.py`）全绿，`TenantProvisionOut` 原有字段不变、只新增。
- **AC-5 契约同步**：若响应形状变化，`test_http_contract.py` 的 snapshot 已同步。
- **AC-6 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
grep -n "_create_pending_invite" backend/app/routers/auth.py   # AC-0：开工前必须先跑，见上方
make verify
.venv/bin/python -m pytest backend/tests/test_rnd313_tenant_activation_email.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q   # AC-4
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "_create_pending_invite" backend/app/routers/platform.py    # AC-2
git diff -- backend/app/routers/platform.py backend/app/schemas/tenant_provision.py
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-311 ✅ Done、RND-285 ✅ Done。**RND-303（`_create_pending_invite` 提炼）是本票真正的执行前置**——若尚未落地，见上方「开工前必须先核实」直接 `BLOCKED_NEEDS_HUMAN`，不要绕过。

## 完成定义（Definition of Done）
- [ ] AC-0 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，说明 `_create_pending_invite` 的实际签名（若与本提示词假设的不同，需说明如何适配）
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：邀请步骤失败导致整个租户创建被回滚——由 AC-3 防守，`db.commit()` 顺序必须在发邀请之前完成。
- 风险：`_create_pending_invite` 实际签名与本提示词假设不符——按实际签名适配，不要为了凑参数去改 `auth.py`（那是只读文件）。
- 回滚：`git checkout -- backend/app/routers/platform.py backend/app/schemas/tenant_provision.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress（**建议在 RND-303 落地之后**才实际派给开发 agent 执行，见上方 AC-0）。
- **Gate**：测试绿即可，无需额外人工审阅（R1）。
- **Escalation**：`_create_pending_invite` 不存在 → `BLOCKED_NEEDS_HUMAN`（见上）。若其签名与本票假设差异较大导致无法干净复用 → 同样 `BLOCKED_NEEDS_HUMAN`，说明差异点。

## 开发 agent 执行指引
1. **先跑 AC-0 的 grep 命令**，确认 `_create_pending_invite` 存在。不存在则立即上报，不要往下做。
2. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/routers/platform.py`（先 `git diff`/`git status`）、`backend/app/routers/auth.py` 中 `_create_pending_invite` 的实际签名、`app/email.py` 的 `send_invite_email`。
3. 给 `TenantProvisionIn` 加 `owner_email`，`create_tenant` 提交事务后调用 `_create_pending_invite`。
4. 写测试覆盖 AC-1~AC-4。
5. 同步契约测试（若响应形状变化）。
6. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假邮箱，**mock** `send_invite_email`，不发真实邮件。
- 不扩大 Scope：多首位管理员 / 登录引导 / 自检 / 列表 / 启停一律 Out。
- 复用优先：`_create_pending_invite` / `send_invite_email` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
