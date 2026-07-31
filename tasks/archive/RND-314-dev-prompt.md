[Goal check] This work advances 开发（Development） by 交付平台总控台的已开通租户列表只读端点，零泄露密钥，为总控台前端提供数据源。

# RND-314 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-314 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-314「B2-4 已开通租户列表」｜父 Epic RND-270（B2 租户开通）
- 优先级：Medium｜风险等级：**R1**｜milestone：R5 · 云商业化前台

## 背景与项目现状（已实地核实）

B2-1（<issue>RND-311</issue>，创建租户）**已 Done 并已上线**：`backend/app/routers/platform.py` 目前只有一个端点 `POST /tenants`（`create_tenant`，`platform.py:23`），挂载前缀待查（`app/main.py` 里 `include_router`，通常是 `/api/platform`）；鉴权用 `require_platform_admin`（`app/auth.py:207`，已 Done 稳定）。

**Tenant / TenantWecomConfig 模型**（`app/db/models.py:54` 起）：`Tenant` 有 `id`/`name`/`slug`/`is_active`/`created_at`；`TenantWecomConfig` 有 `corp_id`/`agent_id`/`app_secret`（加密）/`private_key_encrypted`（加密）/`is_active`——**响应绝不能包含** `app_secret`/`private_key_encrypted` 或其解密属性（`decrypted_app_secret`/`decrypted_private_key`）。

**❗ 本项目高频踩坑：**
- **`platform.py` 是本波次共享文件**：RND-307 / RND-310 / RND-312 / RND-313 也会往这个文件加端点。**开始前先 `git diff`/`git status` 看清哪些兄弟票的端点已落地**，在文件末尾追加自己的函数，**不要改动别的票已经写好的函数**。`test_http_contract.py` 的 `route_count` 用 `make verify` 报错给出的**当前实际值** +1 回填，不要抄本提示词里的任何示例数字。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个只读端点，返回全部已开通租户的摘要列表（不含任何密钥），供平台总控台渲染。

## 范围边界

**In scope：**
1. `backend/app/routers/platform.py` **新增** `GET /tenants`：
   - 鉴权：`require_platform_admin`。
   - 查询 `Tenant` join `TenantWecomConfig`（`is_active` 状态取 `TenantWecomConfig.is_active`，即连接配置是否启用；`Tenant.is_active` 另有语义，见 RND-310，两者都返回，不要混为一谈）。
   - 响应字段：`tenant_id`/`tenant_name`/`tenant_slug`/`corp_id`/`agent_id`/`tenant_is_active`/`config_is_active`/`created_at`。**不含** `app_secret`/`private_key_encrypted`/两者的解密属性。
2. `backend/app/schemas/tenant_provision.py` **新增** `TenantListItemOut` / `TenantListOut`（或建 `schemas/platform.py`，若已有兄弟票建了该文件则复用，不要重复建）。
3. 测试：`backend/tests/test_rnd314_tenant_list.py`。

**Out of scope（显式非目标）：**
- 不做分页 / 排序 / 筛选（首期全量返回，租户数量级不大）。
- 不做租户启停（<issue>RND-310</issue>）、不做连通性自检（<issue>RND-312</issue>）、不做跨租户聚合统计（<issue>RND-307</issue>）——本票只列清单。
- 不改 `create_tenant` 的既有行为。

**本工单拥有的文件（只许写这些，`platform.py` 为共享追加）：**
- `backend/app/routers/platform.py` —— **仅新增** `GET /tenants` 及其依赖；不改 `create_tenant`
- `backend/app/schemas/tenant_provision.py`（或复用兄弟票已建的 `schemas/platform.py`）—— 仅新增列表相关 schema
- `backend/tests/test_rnd314_tenant_list.py`（新）
- `backend/tests/test_http_contract.py` —— 契约同步（route_count 读当前实际值 +1）

**只读、绝不可写：** `app/db/models.py`、`app/auth.py`、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 列表可用**：`GET /tenants`（平台超管凭据）返回全部租户摘要，字段齐全。
- **AC-2 零泄露（关键）**：响应体逐字段核对，不含 `app_secret`/`private_key_encrypted`/`decrypted_app_secret`/`decrypted_private_key` 或其任何片段。须有测试断言响应 JSON 序列化后**不包含**这些字符串。
- **AC-3 鉴权**：无凭据 / 错凭据 → 401；非平台超管（如租户内 admin/owner 会话）→ 401（复用 `require_platform_admin` 既有行为，不新写鉴权逻辑）。
- **AC-4 空态**：无任何租户时返回空列表，不报错。
- **AC-5 契约同步**：`test_http_contract.py` 已同步（route_count 当前基线 +1、expected/snapshot 追加）。
- **AC-6 回归**：`make verify` 全绿；`test_architecture_boundary.py` 通过；`create_tenant`（RND-311）既有测试全绿。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd314_tenant_list.py -q
.venv/bin/python -m pytest backend/tests/test_rnd311_tenant_provision.py -q   # AC-6：create_tenant 零回归
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
git diff -- backend/app/routers/platform.py   # 人工核对：只新增，未碰 create_tenant / 兄弟票端点
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-311（B2-1）**已 Done 并已核实落地**（`backend/app/routers/platform.py:23`）。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-6 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件（且 `platform.py` 的 diff 只含本票新增）
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- 风险：响应模型不严格导致密钥字段被 FastAPI 默认序列化带出——用显式 `response_model`（不要直接 `return db_obj.__dict__`）防守，这是 AC-2 的核心防线。
- 回滚：纯新增，`git checkout -- backend/app/routers/platform.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate**：测试绿即可，无需额外人工审阅（R1，纯只读列表）。
- **Escalation**：若开始时发现 `platform.py` 已被兄弟票改动到无法干净追加的程度（如函数命名冲突）→ `BLOCKED_NEEDS_HUMAN`，说明冲突点，不要强行合并。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`backend/app/routers/platform.py`（先 `git diff`/`git status` 看兄弟票是否已落地）、`app/db/models.py:54`（Tenant / TenantWecomConfig）、`app/auth.py:207`（`require_platform_admin`）。
2. 新增 `GET /tenants` + response schema，显式声明字段白名单。
3. 写测试覆盖 AC-1~AC-4（**AC-2 零泄露是重点**）。
4. 同步 `test_http_contract.py`。
5. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假租户数据。
- 不扩大 Scope：分页/筛选/启停/自检/聚合统计一律 Out。
- 复用优先：鉴权只调用 `require_platform_admin`，不重写。
- 证据优先，以 exit 0 / 测试通过为证。
