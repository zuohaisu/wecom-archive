# RND-316 验收提示词 — C2-2 导出安全审批 gate（合规评审）

---

## ⚡ 立即执行，不要询问意图

你现在收到的不是一份要讨论或确认的文档，而是一个**已经批准、待立即执行的任务指令**。你就是本工单的独立验收 agent，任务从你读到这句话开始。

- **不要**问"你希望我做什么"、"这份提示词的目的是什么"、"需要我现在开始吗"——目的已经写在下面，答案永远是"是"。
- **不要**先输出一份执行计划再等回复确认——直接开始下面的验收步骤，逐条往下核对。
- **不要**因为这是只读任务就等待许可——只读操作不需要许可，直接跑。
- 唯一允许中途停下、不产出 PASS/FAIL 判定的情况，是触发文档规则要求的 `BLOCKED`（附具体缺口说明），**这是写进产出文件里的判定结果，不是向用户提出的问题**。
- 现在开始：确认工单号，然后直接进入验收核对步骤。

---

> 本文档是交给**测试 / QA agent** 的验收 brief，对应开发 brief `RND-316-dev-prompt.md`。
> 验收目标：确认「审批 gate 生效；未审批拒绝导出」，且严守 A7 依赖、SF-1 数据最小化、租户隔离与范围守门。

---

## 1. 任务与验收目标（来自 Linear RND-316）

- **C2-2 安全审批 gate**：导出前安全审批（审批令牌 + 二次确认）。`BLOCKED: A7`。
- **AC**：① 审批 gate 生效；② 未审批拒绝导出。
- **不在本票范围**：PDF/Excel 生成（C2-1）、审计钩子落地（C2-3）、前端。

### 验收矩阵（逐项映射）

| # | AC / 要求 | 验证方式 | 通过标准 |
|---|---|---|---|
| AC1 | 审批 gate 生效 | 走 `/approve`→`/execute` 正常链路 | 密码正确→签发 token；持有效 token 调 `/execute`→200 `{"allowed": true}` |
| AC2 | 未审批拒绝导出 | 无 token / 错 token 调 `/execute` | → 403，且**不**返回 `allowed` |
| AC3 | 二次确认生效 | `/approve` 传错密码 | → 401，且**不**签发 token（库无对应行） |
| AC4 | 单行用 | 重放同一 token 调 `/execute` 两次 | 第一次 200，第二次 403（已消费） |
| AC5 | 过期拒绝 | 用超过 TTL（默认 300s）的 token | → 403 |
| AC6 | 租户隔离 | 租户 A 会话签发 token，租户 B 会话持同 token 调 `/execute` | → 403 |
| AC7 | 跨管理员隔离 | admin X 签发 token，admin Y 会话消费 | → 403 |
| AC8 | 参数绑定 | `/approve` 与 `/execute` 的 `params` 不一致 | → 403（params_hash 不匹配） |
| AC9 | 审计记录（A7） | 检查 `audit_logs` | 签发记 `export.approval_granted`、消费记 `export.approval_consumed`、错密码记 `export.approval_denied` |
| AC10 | SF-1 数据最小化 | 检查 `audit_logs.detail` 与 `export_approval_tokens` 表 | 无消息体/解密 payload/原始 params 全文；表 `token` 列只存 SHA-256 |
| AC11 | 架构边界 | 跑 `test_architecture_boundary.py` | 绿；`app.export_approval` 已在 `_FLAT_SERVICE_MODULES` |
| AC12 | 迁移无漂移 | `alembic upgrade head` + `alembic check` | 均绿 |
| AC13 | 无回归 | 全量 `pytest backend/tests/` | 全绿（重点 auth / audit 列表 / 边界） |

---

## 2. 范围守门（diff 允许集合 — 超出即判违规退回）

**允许的改动文件**：
- `backend/app/db/models.py` —— 新增 `ExportApprovalToken` 类（紧接 `PasswordResetToken`）。
- `backend/alembic/versions/00xx_export_approval_tokens.py` —— 新建迁移。
- `backend/app/export_approval.py` —— 新建 flat 模块。
- `backend/app/audit.py` —— 仅在 `AuditAction` / `AuditObjectType` 类内**新增常量**。
- `backend/app/routers/export_approval.py` —— 新建路由（`/api/admin/export/approve`、`/execute`）。
- `backend/app/main.py` —— 仅新增一行 `include_router(export_approval_router)` + 对应 import。
- `backend/tests/test_architecture_boundary.py` —— `_FLAT_SERVICE_MODULES` 加入 `"app.export_approval"`。
- `backend/tests/test_rnd316_export_approval.py` —— 新建测试。

**禁止（出现任一即退回）**：
- 实现任何 PDF/Excel 文件生成（那是 C2-1 / RND-315）。
- 实现导出审计钩子**落地写入**逻辑（那是 C2-3 / RND-317）；本票只通过 `app.audit.write_audit` 记审批动作。
- 新建/修改 `backend/scripts/` 任何文件（B 层，归 RND-237）。
- 改动 `.env.example`、前端（`review_console.html` 等）、引入新 pip 依赖（仅用 stdlib `secrets`/`hashlib`/`json`/`uuid`）。
- 改动其他 router、其他模型、其他迁移、`app/audit.py` 既有函数签名。

用 `git diff --stat` 与 `git status` 核对实际改动集合，与上方清单比对；多出文件必须要求开发 agent 解释或回退。

---

## 3. 架构边界 / 迁移 / 契约检查

1. **边界**：`python -m pytest backend/tests/test_architecture_boundary.py -q` 必须绿。`app.export_approval` 必须出现在 `_FLAT_SERVICE_MODULES`（不应出现在 `app.routers` 之外被误分类）。`app/routers/export_approval.py` 的 `@router.*` 装饰器必须在模块作用域（不应在 `create_app` 内注册路由）。
2. **迁移**：
   - `cd backend && alembic upgrade head` 成功（或测试用 sqlite in-memory 能建 `export_approval_tokens`）。
   - `alembic check`（RND-227 漂移门）**绿** —— ORM 元数据（`ExportApprovalToken`）与迁移**逐字一致**（列名/类型/`func.now()` 默认/`Boolean`/`String(64)`/`unique=True`+`index=True`/三个索引命名）。
   - `down_revision` 必须等于**实现时** `alembic heads` 的输出（当前预期 `0019`；若 RND-287 先落地则顺延）。**不得硬编码过期编号**。
3. **HTTP 契约**：`/api/admin/export/approve` 与 `/api/admin/export/execute` 均挂在 `prefix="/api/admin/export"`；`get_current_user` 提供 `(user, tenant_id)`；`/execute` **只**返回 `{"allowed": true}`，不含任何文件字节或消息内容。

---

## 4. 安全 / 数据最小化（重点）

1. **令牌保密**：`export_approval_tokens.token` 列只存 SHA-256 hex；断言库内**不存在**任何曾返回的明文 `approval_token`。
2. **原始令牌生命周期**：仅在 `/approve` 响应体出现一次；URL/日志/审计 `detail` 中不得出现明文 token。
3. **审计 detail 干净**：逐条检查本票写入的 `audit_logs` 行，`detail` JSON **不含** `content` / `payload` / `body` / `decrypted` / `raw_*` 等键；只应含 `params_hash`(SHA-256) + `expires_at`(ISO) + 可选 `reason`（如 `"bad_password"`）。
4. **params 不入审计**：`params` 原始内容不得进 `audit_logs.detail`，只进其 hash。
5. **fail-closed**：`get_current_user` 失败 / DB 异常时，gate 必须**拒绝**（401/403），绝不降级为匿名通过。
6. **租户/管理员绑定**（AC6/AC7）：`require_export_approval` 必须同时校验 `tenant_id` 与 `admin_user_id`；用跨租户或跨管理员的 token 必须 403。

---

## 5. 回归套件 + RED → GREEN

### 5.1 新建测试 `backend/tests/test_rnd316_export_approval.py`
至少覆盖 §1 验收矩阵全部 13 项。参考项目既有 node/pytest 混合范式；本票纯后端，用 `pytest` + `fastapi.TestClient` + sqlite in-memory（参考 `backend/tests/fakes.py` 与 `test_rnd295_audit_list.py` 的 fixtures）。

### 5.2 RED → GREEN 流程
1. **RED（可选）**：若开发 agent 未登记 `app.export_approval` 到边界集合，先跑 `test_architecture_boundary.py` 应 FAIL。
2. **GREEN（实现后）**：
   - `python -m pytest backend/tests/test_rnd316_export_approval.py -q` 全绿（13 项 AC 全过）。
   - `python -m pytest backend/tests/test_architecture_boundary.py -q` 绿。
   - `cd backend && alembic upgrade head && alembic check` 绿。
   - `python -m pytest backend/tests/ -q` 全量无回归。
3. 任一 RED 或范围越界 → 退回开发 agent，写明具体失败项与期望。

---

## 6. 智能路由（遇到以下情况如何处理）

- **A7 未就位**（开发 agent 报 `BLOCKED: A7`）：确认工作树确实缺 `app.audit.write_audit` / `AuditLog`；若是，判定本票**前置未满足**，退回并建议先完成 A7 后再派发，**不**要求开发 agent 自建审计。
- **超出范围改动**（如顺手实现了 PDF 生成或审计钩子）：判定违规，要求回退至 §2 允许集合；解释「功能票不越界」。
- **令牌表与 `password_reset_tokens` 不一致**（如误用 JSONB / 漏 `used` 列 / 漏索引）：要求对齐范式，否则 `alembic check` 会红。
- **硬编码迁移编号**：检查 `down_revision` 是否为实现时的 `alembic heads`；若写死 `0019` 但 RND-287 已先落地（head 变 `0020+`），要求改为动态取值，否则与既有迁移冲突。
- **未写审计常量**：检查 `app/audit.py` 的 `AuditAction`/`AuditObjectType` 是否含 `EXPORT_APPROVAL_*`；缺失则 gate 记审计会用到未定义常量 → 退回补全。

---

## 7. 交付判定

全部满足以下条件方可判 **PASS**：
1. §1 验收矩阵 13 项全绿。
2. §2 范围守门通过（`git diff --stat` 仅含允许文件）。
3. §3 边界 / 迁移 / 契约全绿。
4. §4 安全与 SF-1 全通过。
5. §5 回归套件全绿，无既有测试回归。

PASS 后**不要自行 commit/push**，将结论回报用户，由用户本人提交。
