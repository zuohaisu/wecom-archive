[Goal check] This work advances 开发（Development） by 交付平台超管内容访问申请的审计门控端点，直接复用已就绪的 require_platform_tenant_scope（自动写审计），不重造审计逻辑。

# RND-309 开发提示词（Developer Prompt）

> 开始前必须先读 `DEV_AGENT_RULES.md` 与 `docs/ticket-autopilot-workflow.md`，并在工作开头输出上面那行 `[Goal check]`。

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是 RND-309 的开发 agent，任务从你读到这句话开始。不要问"你希望我做什么"；不要先输出计划再等确认；直接开始下面的实现清单。唯一允许停下的情况是触发「人工点位」里定义的 `BLOCKED_NEEDS_HUMAN`。

---

## 任务身份
- 工单：RND-309「B1-5 内容访问申请 gate」｜父 Epic RND-275（B1 平台总控台）
- 优先级：Medium｜风险等级：**R2**（合规存档产品的核心防线之一：默认任何人不可看内容，超管访问必须留痕）｜milestone：R5 · 云商业化前台

## 背景与项目现状（已实地核实）

B1-2（<issue>RND-305</issue>）**已 Done 并已上线**，且**已经交付了本票几乎全部需要的原语**——`app/auth.py:238` 的
```python
def require_platform_tenant_scope(
    tenant_id: str = Query(..., min_length=1),
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PlatformAdminTenantScope:
    """Resolve explicit tenant_id and append its mandatory access audit row."""
    write_audit(db, tenant_id=tenant_id, action=PLATFORM_TENANT_ACCESS_ACTION,
                object_type=PLATFORM_TENANT_OBJECT_TYPE, object_id=tenant_id,
                detail={"platform_admin_id": ..., "platform_admin_email": ...})
    return PlatformAdminTenantScope(platform_admin=platform_admin, tenant_id=tenant_id)
```
这个依赖**已经做到**「验证平台超管身份 + 解析显式 `tenant_id` + 强制写审计」——本票只需要把它接到一个端点上，**几乎不需要新写审计逻辑**。

**❗ 本项目高频踩坑：**
- **不要新建一套审计/审批表**：本票 AC 说的"审计门控请求（记录）"，字面上容易让人联想到 <issue>RND-316</issue>（导出审批 gate）那种带 `export_approval_tokens` 表的完整审批工作流——**本票不是那个**。RND-309 的 Scope 只是"记录访问意图 + 默认不放行"，`require_platform_tenant_scope` 调用即完成"记录"这一步，**不需要新表**。若发现 AC 无法在不建新表的前提下满足，按 Escalation 上报，不要照抄 RND-316 建一套审批 token 表。
- **不放在共享的 `platform.py`**：为避免与 RND-307/310/312/313/314 在 `platform.py` 上的并行改动冲突，本票**新建独立文件** `app/routers/platform_access.py`。
- 架构冻结 D1：纯后端。

## 目标（Goal）
交付一个端点：平台超管对某租户发起"内容访问申请"，请求本身被强制审计（经 `require_platform_tenant_scope` 自动完成），响应明确告知"已记录，默认不返回任何内容"。

## 范围边界

**In scope：**
1. `backend/app/routers/platform_access.py`（**新建**）：
   - `POST /api/platform/content-access-requests`：依赖 `require_platform_tenant_scope`（`tenant_id` 走 query 参数，鉴权+审计已由该依赖完成）。
   - 响应：`{"tenant_id": ..., "recorded": true, "granted": false, "note": "access request recorded; this endpoint does not return message content"}`。
   - 挂载到 `app/main.py`（仿既有 router 注册方式，追加，不改其他行）。
2. 测试：`backend/tests/test_rnd309_content_access_request.py`。

**Out of scope（显式非目标）：**
- **不新建审批 token 表**（区别于 <issue>RND-316</issue> 的导出审批工作流，见上）。
- **不实现"申请后真正放行查看内容"的机制**——本票的 AC 明确是"默认不可读内容"，放行机制（如果未来需要）是独立的下游范围。
- 不做审批人二次确认 / 审批流程 UI。
- 不改 `require_platform_tenant_scope` / `write_audit` 本身。

**本工单拥有的文件（只许写这些）：**
- `backend/app/routers/platform_access.py`（新）
- `backend/app/schemas/platform_access.py`（新，或就近放在 schema 目录已有的合适文件，视项目现有 schema 组织方式而定，不新建重复文件）
- `backend/app/main.py` —— **仅新增** 1 行 import + 1 行 `include_router`
- `backend/tests/test_rnd309_content_access_request.py`（新）
- `backend/tests/test_http_contract.py` —— 契约同步

**只读、绝不可写：** `app/auth.py`（只调用 `require_platform_tenant_scope`）、`app/audit.py`、`app/db/models.py`、`app/routers/platform.py`（本波次其他票的共享文件，本票不碰）、其他票拥有的一切文件。

> 若发现必须改他人拥有的文件 → **停止**，`BLOCKED_NEEDS_HUMAN`。

## 验收标准（Acceptance Criteria）

- **AC-1 申请记入审计**：`POST /api/platform/content-access-requests?tenant_id=X`（合法平台超管凭据）→ 一条 `AuditLog` 生成（`action=PLATFORM_TENANT_ACCESS_ACTION`），含超管身份 + 目标 `tenant_id`。**这一步应完全由调用 `require_platform_tenant_scope` 自动完成**，不要在端点函数体里再手写一次 `write_audit`（会产生重复审计记录）。
- **AC-2 默认不可读内容**：响应体**不含**任何消息正文/媒体/联系人明细字段，只有 `recorded`/`granted`/`note` 一类元信息；`granted` 恒为 `false`（本票不实现放行）。
- **AC-3 复用而非重写（关键）**：`grep -n "require_platform_tenant_scope" backend/app/routers/platform_access.py` 应有命中；**不得**在本票里手写角色校验/审计写入逻辑。
- **AC-4 鉴权**：无凭据 / 错凭据 / 非平台超管会话 → 401；缺 `tenant_id` 参数 → 422。
- **AC-5 无重复审计**：一次请求只产生**一条** `AuditLog`（不是因为端点内又手写了一次 `write_audit` 导致产生两条）。须有测试断言调用一次后 `AuditLog` 恰好新增 1 条。
- **AC-6 未新建审批表**：`backend/alembic/versions/` 无新文件；`app/db/models.py` 无改动。
- **AC-7 契约同步 + 回归**：`test_http_contract.py` 已同步；`make verify` 全绿；`test_architecture_boundary.py` 通过。

## 验证方式（Verification — 确定性闸）
```bash
make verify
.venv/bin/python -m pytest backend/tests/test_rnd309_content_access_request.py -q
.venv/bin/python -m pytest backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q
grep -n "require_platform_tenant_scope" backend/app/routers/platform_access.py    # AC-3
grep -n "write_audit" backend/app/routers/platform_access.py    # 应为空或仅出现在 import 语句里（AC-1 说明），端点函数体不应再手写
git diff --stat -- backend/app/db/models.py    # AC-6：必须无输出
ls backend/alembic/versions/ | tail -3          # AC-6：无新文件
git status --porcelain
git log origin/main..HEAD    # 必须无输出
```

## 依赖（Dependencies）
RND-305（B1-2）**已 Done 并已核实落地**（`require_platform_tenant_scope` 于 `app/auth.py:238`）。无剩余前置。

## 完成定义（Definition of Done）
- [ ] AC-1 ~ AC-7 全满足，每条有测试
- [ ] `make verify` 全绿
- [ ] `git status` 只显示本票拥有的文件
- [ ] QA Summary 已产出
- [ ] **未 commit、未 push**

## 风险与回滚
- **风险 1**：端点函数体里又手写一次 `write_audit`，与 `require_platform_tenant_scope` 内部已经写的那次重复，产生两条审计记录，污染合规日志——由 AC-5 防守。
- **风险 2**：误建了一套类似 RND-316 的审批 token 表，超出本票 Scope——由 AC-6 防守。
- 回滚：纯新增文件 + `main.py` 两行，`git checkout -- backend/app/main.py && rm backend/app/routers/platform_access.py` 即可；无迁移、无数据影响。

## 人工点位
- **Trigger**：Haisu 置 In Progress。
- **Gate（R2）**：本票是合规访问留痕的核心一环，**建议 Haisu 过一遍审计记录字段是否符合预期**再 approve commit。
- **Escalation**：若发现"记录访问意图"这一 AC 在不新建表的前提下无法满足（例如产品实际想要的是可查询的申请状态列表，而不只是审计日志里的一条记录）→ `BLOCKED_NEEDS_HUMAN`，说明具体缺口，**不要**照抄 RND-316 自行建一套审批表。

## 开发 agent 执行指引
1. 读 `DEV_AGENT_RULES.md`、`docs/ticket-autopilot-workflow.md`、`app/auth.py:238`（`require_platform_tenant_scope` 完整实现）、`app/audit.py:54`（`write_audit`，仅作背景了解，本票不直接调用）。
2. 新建 `platform_access.py`，端点依赖 `require_platform_tenant_scope`，函数体只组装响应，不再手写审计。
3. 挂载到 `main.py`。
4. 写测试覆盖 AC-1~AC-5（**AC-5 无重复审计是重点**）。
5. 同步 `test_http_contract.py`。
6. 跑全部验证命令，输出 QA Summary + `git status`，**不 commit**。

## 硬性约束（DEV_AGENT_RULES.md）
- 不 commit / push / 建分支 / 改 git 历史；不改 CI/CD、`.gitignore`、部署配置。
- 不碰生产数据与密钥；测试用固定假租户数据。
- 不扩大 Scope：不建审批表、不做放行机制、不做审批 UI。
- 复用优先：`require_platform_tenant_scope` 只调用不重写，不重复写审计。
- 证据优先，以 exit 0 / 测试通过为证。
