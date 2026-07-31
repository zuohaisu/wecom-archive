[Goal check] This work advances 开发（Development） by 交付平台超管的跨租户鉴权作用域原语与强制审计留痕，为 B1 平台总控台的三张下游票备好安全地基。

# RND-305 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-305 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-305「B1-2 跨租户鉴权作用域」｜父 Epic RND-275（B1 平台总控台）
- 优先级：Medium｜风险等级：**R2（触及鉴权与权限边界）**｜milestone：R5
- **本票 blocks RND-307（跨租户聚合）/ RND-309（内容访问 gate）/ RND-310（租户启停）**

## 背景与现状（已实地核实）

| 前置 | 状态 | 落地证据 |
|---|---|---|
| B1-1 `PlatformAdmin` 实体 + `verify_platform_admin` | ✅ **Done** | `backend/app/auth.py:168` `def verify_platform_admin(db, email, password)`；模型 `models.py:269` |

`PlatformAdmin` 的模型 docstring（`models.py:269-274`）自己写明了本票的职责：

> "Tenant-less by design: a platform admin operates across all tenants (**cross-tenant scope is enforced at the auth layer, see B1-2 / RND-305**)."

**当前租户内的隔离范式**（不要破坏）：所有租户 API 通过 `require_role()` 从**会话**解包 `tenant_id`（`backend/app/routers/users.py:33-38` 是标准范式），且铁律是 **`tenant_id` 绝不从请求参数取**。

**平台超管需要一条不同的路径**：它按定义要跨租户操作，所以 `tenant_id` 只能由调用方显式指定。这与上面的铁律是**有意的例外**——但必须用两道保险把风险收住：① 身份必须先证明是平台超管；② 每次跨租户访问强制写审计。

**审计基础设施已就绪**：`backend/app/audit.py:54` 的
`write_audit(db, *, tenant_id, action, object_type, admin_user_id=None, object_id=None, detail=None)`，append-only 且 **fail-safe（永不抛异常）**。**直接调用，不要重造。**

**❗ 本项目高频踩坑：**
- **架构边界硬闸**：`app.auth` 是已 allowlist 的扁平域模块（`test_architecture_boundary.py:71`），在其中加函数无需改 allowlist；但**不得** import `app.routers.*` 或 `app.main`。
- **RBAC 白名单是闭世界测试**：`backend/tests/test_rnd280_rbac_scaffold.py:77-86` 断言只有 `{audit.py, media_library.py, users.py}` 可以出现 `require_role`。**若本票新建了使用 `require_role` 的 router，必须同步该白名单**（见 `docs/ticket-autopilot-workflow.md` §3.3）。注意：本票的 `require_platform_admin` 是**不同的**依赖，若命名不含 `require_role` 则不触发该断言——实现后跑一遍确认。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个**鉴权作用域原语**：平台超管可以跨租户操作，普通租户管理员绝不能升格，且每次跨租户访问都留下不可篡改的痕迹。

## 范围边界

**In scope：**
1. **`require_platform_admin` 依赖**（加在 `backend/app/auth.py`，紧邻 `verify_platform_admin` / `require_role`）：
   - 解析平台超管身份（过渡期可用 HTTP Basic，**留单一替换点**供 B1 后续换成 session cookie）。
   - 缺凭据 / 错凭据 → 401。
   - 返回 `PlatformAdmin` 对象。
2. **跨租户作用域语义**：定义并实现「超管访问某租户数据时，`tenant_id` 从显式请求参数取」。
   - **必须在代码注释中写清楚为什么这是安全的**（身份已是平台超管 + 每次写审计），且**明确标注这是对"tenant_id 绝不来自请求参数"铁律的有意例外**——防止后来者当成 bug 修掉，也防止有人照抄这个模式到租户内 API。
3. **强制审计**：跨租户访问时调用 `write_audit(...)`，记录「哪个超管 / 访问了哪个租户 / 做了什么」。
4. 测试：`backend/tests/test_platform_auth_scope.py`。

**Out of scope（显式非目标）：**
- **不实现具体的跨租户业务端点** —— 跨租户聚合（RND-307）、租户启停（RND-310）、内容访问 gate（RND-309）都是下游票，它们**消费**本票的原语。本票只交付原语本身（可配一个最小的验证端点，但不做业务）。
- 不实现平台超管登录 UI / session cookie 签发（过渡期 Basic 即可，留替换点）。
- **不改租户内 `require_role` 的既有行为**（零回归）。
- 不改 `PlatformAdmin` 模型 / 无新迁移。

**本工单拥有的文件（只许写这些）：**
- `backend/app/auth.py` —— **仅新增** `require_platform_admin` 及其辅助；**不得**改动 `require_role` / `verify_platform_admin` / 既有会话逻辑
- `backend/tests/test_platform_auth_scope.py`（新）
- `backend/tests/test_rnd280_rbac_scaffold.py` —— **仅当**本票导致该测试失败时（见上方踩坑说明），只加必要条目
- `backend/tests/test_http_contract.py` —— **仅当**本票新增了路由时（强制随附，route_count 读当前基线 +1）
- `docs/<平台超管鉴权说明>.md`（新，可选但建议）

**只读、绝不可写：** `app/audit.py`（只调用 `write_audit`）、`app/db/models.py`、所有 `app/routers/*`（除非新增路由，见上）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 依赖可用**：合法平台超管凭据通过 `require_platform_admin`；缺凭据 / 错凭据 → 401（均有用例）。
- **AC-2 租户内零回归（关键）**：`require_role` 行为与租户隔离逻辑**完全不变**——既有 `test_auth.py` / `test_admin_users_api.py` / audit / media_library 相关测试全绿；`git diff -- backend/app/auth.py` 中**看不到对 `require_role` 或既有会话逻辑的改动**。
- **AC-3 跨租户访问必写审计**：超管跨租户读取时产生一条 `AuditLog`，含超管身份标识 + 目标 `tenant_id`；**经 `write_audit` 写入**（代码可见 import + 调用，非自造插入）。须有测试。
- **AC-4 普通租户管理员不得升格（最高风险项）**：持租户内 `admin` / `owner` 角色的会话**不能**通过 `require_platform_admin`。**必须有反例测试**——这是权限提升的唯一防线。
- **AC-5 例外语义有文档**：代码注释明确说明「为何此处允许从请求参数取 `tenant_id`」及其两道保险，并标注这是对铁律的有意例外、不得照抄到租户内 API。
- **AC-6 契约同步（若适用）**：若新增路由 → `test_http_contract.py` 已同步（route_count 当前基线 +1）；若触发 RBAC 白名单 → 已同步。见 §3.3。
- **AC-7 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_platform_auth_scope.py -q
.venv/bin/python -m pytest backend/tests/test_auth.py backend/tests/test_admin_users_api.py -q   # AC-2：租户内零回归
.venv/bin/python -m pytest backend/tests/test_rnd280_rbac_scaffold.py backend/tests/test_http_contract.py -q
.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/auth.py    # 人工核对：只新增 require_platform_admin，未动 require_role / 会话逻辑
git diff --stat -- backend/app/audit.py backend/app/db/models.py   # 必须全无输出
git status --porcelain             # 共享工作树，先分离归因
```

## 依赖（Dependencies）
RND-306（`verify_platform_admin`）**已 Done 并核实落地**。无剩余前置。**本票 blocks RND-307 / RND-309 / RND-310。**

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出，**说明：过渡期认证方式（Basic？）与未来替换点在哪、跨租户 `tenant_id` 的传入方式（参数名）、审计记录的字段结构**（供 RND-307/309/310 对接）
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1（最高）**：`require_platform_admin` 实现不严，让持租户内高权限角色的会话也能通过 → **权限提升漏洞，可跨租户读取全部客户数据**。**由 AC-4 的反例测试防守，这条不能省。**
- **风险 2**：「从请求参数取 `tenant_id`」的模式被后来者照抄到租户内 API → 租户隔离全线失守。**由 AC-5 的显式注释防守**，必须写清楚这是例外且为何安全。
- **风险 3**：审计写入被遗漏或被额外 `try/except` 吞掉 → 跨租户访问无痕迹，合规链条断裂。`write_audit` 本身已 fail-safe，**不要再包一层静默吞异常**。
- **风险 4**：改动 `auth.py` 时误伤 `require_role` → 打断所有租户内 API 的鉴权（爆炸半径极大，`auth.py` 是全站鉴权中枢）。**由 AC-2 防守**，diff 必须干净。
- 回滚：纯新增函数 + 测试，`git checkout -- backend/app/auth.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress（已置）。
- **Gate（R2 强制）**：**本票触及鉴权与权限边界，必须经 Haisu 人工审阅后才可 commit。** 这是全站权限模型的改动，不适用"测试绿就放行"。
- **Escalation**：若发现平台超管与租户会话共用同一套 cookie/session 机制、无法干净区分两种身份 → `BLOCKED_NEEDS_HUMAN`，说明具体耦合点，**不要**为了区分而改动既有租户会话逻辑（那会波及所有租户 API）。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/auth.py`（重点：`verify_platform_admin` @168、`require_role`、会话解析逻辑）、`models.py:269`（`PlatformAdmin` 及其 docstring）、`routers/users.py:33-38`（租户内隔离范式）、`app/audit.py:54`（`write_audit` 签名）。
2. 在 `auth.py` **新增** `require_platform_admin`（不动既有函数）。
3. 实现跨租户 `tenant_id` 传入语义 + 强制审计调用 + **写清例外注释**。
4. 写测试覆盖 AC-1~AC-5（**AC-4 的"租户管理员不得升格"反例测试是重点**）。
5. 跑 `test_rnd280_rbac_scaffold.py` 确认是否被触发；若触发则按 §3.3 同步。
6. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假凭据。
- 不扩大 Scope：跨租户业务端点、超管登录 UI 一律 Out。
- 复用优先：`verify_platform_admin`、`write_audit` 只调用不重写。
- 证据优先，以 exit 0 / 测试通过为证。
