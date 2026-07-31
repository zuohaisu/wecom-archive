# RND-317 开发执行提示词 — C2-3 导出审计钩子（合规评审）

> 本文档是交给**开发 agent** 的执行 brief。交付物 = 本文件 + 同目录 `RND-317-qa-prompt.md`（验收 brief）。
> 工程纪律（来自 issue + `DEV_AGENT_RULES.md`）：**Agent 绝不 `git commit` / `git push`**；不引入 React（架构冻结 D1）；代码标识符加反引号；范围守门（见 §7）。

---

## 1. 任务概述（来自 Linear RND-317）

- **标题**：`[BLOCKED: A7] C2-3 导出审计钩子`
- **Epic**：RND-271 · C2 证据导出（PDF/Excel）
- **Scope（后端）**：导出审计钩子——把"导出被执行"这一事件可靠地写入 `audit_logs`（A7）。
- **Dependencies**：A7（Audit）。**现状：A7 已合并**（见 §2），故 `[BLOCKED: A7]` 对数据层**已非硬阻塞**，可直接开工。
- **Non-goals（本票）**：不做实际 PDF/Excel 文件生成（那是 C2-1 / RND-315 的职责）；不实现审批 gate 本身（那是 C2-2 / RND-316 的职责，本票仅**消费**其 gate）；不做前端。
- **Acceptance Criteria**：导出被记录。
- **Estimate**：0.75w · **Labels**：backend, export, audit。

**本票交付一个可复用的「导出审计钩子」原语**（`record_export_audit`）+ 一个**自包含、可独立验收**的 `POST /api/admin/export/record` 端点（调用钩子记录审计），供现在验收、也供未来 C2-1 在真正生成文件时调用。

---

## 2. 决策背景与基线核实（已替你核实，开发 agent 仍需开工自检）

RND-317 标着 `BLOCKED: A7`。但工作树现状表明 **A7 基础设施已落地**：
- `backend/app/db/models.py:236` — `AuditLog` 模型（`audit_logs` 表，不可变 append-only，列：`id/tenant_id/admin_user_id/action/object_type/object_id/detail/created_at`）。
- `backend/app/audit.py:54` — `write_audit(...)` 审计写入钩子（RND-294 / A7-2），**FAIL-SAFE**：在 `db.begin_nested()` savepoint 内 add+flush，**不 commit**，任何异常被吞掉，绝不污染主事务。
- Alembic 迁移 `0019_audit_log.py` 已存在。
- `app.audit` 已在 `tests/test_architecture_boundary.py:72` 的 `_FLAT_SERVICE_MODULES` 集合中 → 本票把钩子放进 `app/audit.py`，**无需新增 flat 模块、无需改 `_FLAT_SERVICE_MODULES`、无新迁移**。

**C2 兄弟票现状（避免撞车，务必读）**：
- C2-2 / RND-316（审批 gate）已交付提示词但**尚未合并** —— 它占有 `app/routers/export_approval.py` 的 `POST /api/admin/export/approve` 与 `POST /api/admin/export/execute`（gate 强制面，不生成文件），并占用 `AuditAction.EXPORT_APPROVAL_*` / `AuditObjectType.EXPORT_APPROVAL_TOKEN` 常量。
- C2-1 / RND-315（导出服务，生成 PDF/Excel）**尚未实现**，也无提示词。

**关键现实**：本票是"审计钩子"，不是"导出服务"。因此：
1. **不生成文件**（那是 C2-1）。
2. **不复刻 gate**（那是 RND-316）。本票端点 `POST /api/admin/export/record` 仅消费 RND-316 的 `require_export_approval`（若存在则校验，若不存在则 soft-gate，见 §5.3）。
3. 端点路径必须**避开** RND-316 的 `/approve` / `/execute` → 用 `/record`。

### 2.1 设计决策（已拍板，开发 agent 照办）

1. **钩子落点 = `app/audit.py`**：新增 `AuditAction.EXPORT = "export.executed"` + `AuditObjectType.EXPORT = "export"` 常量，以及 `record_export_audit(...)` 函数（封装 `write_audit`，FAIL-SAFE，detail 仅含非敏感上下文）。这与 `write_audit` 同模块，零新模块、零迁移。
2. **端点 `POST /api/admin/export/record`**：自包含的导出审计记录入口。
   - 租户范围**仅**来自 `get_current_user` 解包，绝不接受请求体里的 `tenant_id`。
   - 轻量 `func.count` 计算本次导出覆盖的记录数（仅 `ArchiveMessage` 元数据列，绝不 `SELECT content_text` / `decrypted_payload` —— SF-1）。
   - **消费 RND-316 gate**：若 `app.export_approval` 可导入且请求带 `approval_token` → 调 `require_export_approval`（失败 → 403）；若 RND-316 尚未合并（模块不可导入）→ `gate_enforced=False` 并打 warning，仍记录审计（这是钩子独立验收路径，见 §5.3 软闸门说明）。
   - 调 `record_export_audit` 写入 `EXPORT` 审计行，detail 含 `{format, record_count, scope, params_hash, approval_ref, gate_enforced}`。
   - `db.commit()` 原子持久化审计行。
3. **`params_hash` 契约与 RND-316 对齐**：用 `json.dumps(scope, sort_keys=True, separators=(",",":"), ensure_ascii=False)` 再 `sha256` 得到 `params_hash`，使审计里的 `params_hash` 与 RND-316 签发令牌时绑定的 `params_hash` 可对应（便于合规核对"某次导出是否被审批过"）。
4. **零新依赖**：仅用栈内 stdlib（`hashlib`/`json`/`uuid`/`datetime`）。
5. **C2-1 未来集成点**：`record_export_audit` 即 C2-1 在真正生成 PDF/Excel 后应调用的钩子；本票端点仅是其"现在就能验收"的替身。

---

## 3. 前置依赖与开工闸口

### 3.1 A7 自检（开工第一步，必须做）
用 import 断言确认 A7 已在树：
```python
from app.audit import write_audit          # 必须存在
from app.db.models import AuditLog         # 必须存在
```
- **若任一缺失** → 立即停止，在回复中报告 `BLOCKED: A7 基础设施未就位（缺 app.audit.write_audit / AuditLog 模型）`，**不得自建审计模块**。
- 若俱全 → 继续。注：Linear 仍标 `BLOCKED: A7`，派发前请你确认 A7 已真正合并（工作树现状显示已落地）。

### 3.2 其他已存在的依赖（不应重建）
- `AdminUser`（`id` / `password_hash` 来自 F0-1，已落地）。
- `get_current_user` 返回的 `(AdminUser, tenant_id)` 授权范围。
- `write_audit`（见 §2）。
- （可选消费）`require_export_approval` 来自 RND-316 的 `app.export_approval` —— **仅当该模块可导入时使用**，不可导入则 soft-gate。

---

## 4. 精确落点（`文件:行号:函数`）

| 落点 | 坐标 | 改动 |
|---|---|---|
| 审计常量 | `backend/app/audit.py` `AuditAction` 类（`:44` `CONFIG_CHANGED` 之后） | 新增 `EXPORT = "export.executed"` |
| 审计常量 | `backend/app/audit.py` `AuditObjectType` 类（`:51` `PASSWORD_RESET_TOKEN` 之后） | 新增 `EXPORT = "export"` |
| 钩子函数 | `backend/app/audit.py`（`write_audit` 之后，约 `:83`） | 新增 `record_export_audit(...)` |
| 路由 | `backend/app/routers/export_audit.py`（**新建**，`APIRouter(prefix="/api/admin/export")`） | `POST /record` |
| 装配 | `backend/app/main.py:19`（import 区） + `:106`（`settings_router` 之后） | 新增 `export_audit_router` 注册 |
| 边界登记 | **无需改动**（`app.audit` 已在 `_FLAT_SERVICE_MODULES`，本票不新增 flat 模块） | — |
| 测试 | `backend/tests/test_rnd317_export_audit.py`（**新建**） | 单元 + API 契约 |

**关键既有签名（务必对齐）**：
- `app/auth.py:319 get_current_user(session_id: Optional[str] = Cookie(...), db) -> Tuple[AdminUser, str]` —— 返回 `(user, tenant_id)`，`tenant_id` 是全量 admin 查询的**唯一授权范围**，绝不接受请求体里的 tenant_id。
- `app/audit.py:54 write_audit(db, *, tenant_id, action, object_type, admin_user_id=None, object_id=None, detail=None)` —— **只 add+flush，不 commit**；调用方在自己的 `db.commit()` 中原子持久化；fail-safe 永不抛。
- `AuditLog.detail`（`models.py:258`，`JSONB`）**严禁**写入消息体或解密 payload（SF-1）。
- （RND-316 若已合并）`app/export_approval.require_export_approval(*, db, token, params, admin_user_id, tenant_id)` —— 校验失败抛 `ExportNotApprovedError`。

---

## 5. 实现步骤

### 5.1 扩展 `app/audit.py` 常量
`AuditAction` 类（`CONFIG_CHANGED` 之后）增加：
```python
EXPORT = "export.executed"
```
`AuditObjectType` 类（`PASSWORD_RESET_TOKEN` 之后）增加：
```python
EXPORT = "export"
```

### 5.2 新增钩子函数 `record_export_audit`（`write_audit` 之后）
```python
def record_export_audit(
    db: Session,
    *,
    tenant_id: str,
    admin_user_id: Optional[str],
    export_format: str,
    record_count: Optional[int] = None,
    scope: Optional[Mapping[str, Any]] = None,
    approval_ref: Optional[str] = None,
    gate_enforced: Optional[bool] = None,
) -> None:
    """C2-3 导出审计钩子：记录一次导出事件到 audit_logs。

    FAIL-SAFE（经 write_audit 的 savepoint）。detail 仅含结构化非敏感上下文，
    严禁写入消息体/解密 payload（SF-1）。由调用方在自己的 db.commit() 中持久化。
    """
    params_hash = None
    if scope is not None:
        canonical = json.dumps(
            {"format": export_format, "scope": scope},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        params_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    write_audit(
        db,
        tenant_id=tenant_id,
        action=AuditAction.EXPORT,
        object_type=AuditObjectType.EXPORT,
        admin_user_id=admin_user_id,
        detail={
            "format": export_format,
            "record_count": record_count,
            "params_hash": params_hash,
            "approval_ref": approval_ref,   # 仅存令牌 SHA-256（若有），不存明文
            "gate_enforced": gate_enforced,
        },
    )
```

### 5.3 路由 `app/routers/export_audit.py`（新建）
```python
"""RND-317 (C2-3) 导出审计钩子端点。

自包含、可独立验收的导出审计记录入口。不生成文件（C2-1 职责）；
消费 RND-316 的审批 gate（若存在则校验，否则 soft-gate）。
"""
from __future__ import annotations
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.audit import record_export_audit, AuditAction, AuditObjectType
from app.db.models import ArchiveMessage
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/export", tags=["export-audit"])


class ExportRecordScope(BaseModel):
    conversation_ids: Optional[list[str]] = None
    date_from: Optional[int] = None   # ArchiveMessage.msgtime 为毫秒 epoch
    date_to: Optional[int] = None
    msgtype: Optional[str] = None


class ExportRecordRequest(BaseModel):
    format: str = "csv"                # csv | excel | pdf（仅记录，不生成）
    scope: ExportRecordScope = ExportRecordScope()
    approval_token: Optional[str] = None


def _params_hash(s: ExportRecordScope, fmt: str) -> str:
    import hashlib, json
    canonical = json.dumps(
        {"format": fmt, "scope": s.model_dump(exclude_none=True)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _count_records(db: Session, tenant_id: str, scope: ExportRecordScope) -> int:
    """轻量计数：仅 ArchiveMessage 元数据列，绝不 SELECT 消息体（SF-1）。"""
    q = db.query(func.count(ArchiveMessage.id)).filter(
        ArchiveMessage.tenant_id == tenant_id
    )
    if scope.conversation_ids:
        q = q.filter(ArchiveMessage.roomid.in_(scope.conversation_ids))
    if scope.date_from is not None:
        q = q.filter(ArchiveMessage.msgtime >= scope.date_from)
    if scope.date_to is not None:
        q = q.filter(ArchiveMessage.msgtime <= scope.date_to)
    if scope.msgtype:
        q = q.filter(ArchiveMessage.msgtype == scope.msgtype)
    return int(q.scalar() or 0)


@router.post("/record")
def record_export(req: ExportRecordRequest,
                  auth=Depends(get_current_user), db: Session = Depends(get_db)):
    user, tenant_id = auth

    # —— 消费 RND-316 审批 gate（若存在则校验，否则 soft-gate）——
    gate_enforced = False
    approval_ref = None
    try:
        from app.export_approval import require_export_approval
        if req.approval_token:
            require_export_approval(
                db=db, token=req.approval_token,
                params=req.scope.model_dump(exclude_none=True),
                admin_user_id=user.id, tenant_id=tenant_id,
            )
            gate_enforced = True
            approval_ref = hashlib.sha256(req.approval_token.encode()).hexdigest()
        else:
            logger.warning("RND-317: export recorded WITHOUT approval token "
                           "(RND-316 gate present but token absent)")
    except ImportError:
        logger.warning("RND-317: app.export_approval (RND-316) not deployed; "
                       "running with gate_enforced=False for hook verification")
    except Exception as e:  # require_export_approval 抛 ExportNotApprovedError 等
        raise HTTPException(403, f"export approval rejected: {e}")

    record_count = _count_records(db, tenant_id, req.scope)
    record_export_audit(
        db, tenant_id=tenant_id, admin_user_id=user.id,
        export_format=req.format, record_count=record_count,
        scope=req.scope.model_dump(exclude_none=True),
        approval_ref=approval_ref, gate_enforced=gate_enforced,
    )
    db.commit()
    return {
        "recorded": True,
        "format": req.format,
        "record_count": record_count,
        "gate_enforced": gate_enforced,
    }
```
> **软闸门说明（重要）**：本票是"审计钩子"，审批 gate 是 RND-316 的职责。为让本票可**独立验收**（不依赖 RND-316 先合并），当 `app.export_approval` 不可导入时，端点以 `gate_enforced=False` 记录审计并打 warning，这是预期的 hook 验收路径。生产环境**必须**先合并 RND-316，`gate_enforced=True` 才会生效。审计 `detail` 如实记录 `gate_enforced`，供合规核查。

### 5.4 装配 `main.py`
import 区（`main.py:19` 附近）加：
```python
from app.routers.export_audit import router as export_audit_router
```
在 `app.include_router(settings_router, prefix="/api/admin")`（`main.py:106`）之后加：
```python
app.include_router(export_audit_router, prefix="/api/admin")
```
（模块级 import 与其他 router 同区；不要在 `create_app` 内 import。）

---

## 6. RED → GREEN 验证（开发 agent 自检）

1. **RED**：跑 `python -m pytest backend/tests/test_architecture_boundary.py -q` —— 预期**通过**（本票不新增 flat 模块，无回归）。
2. **GREEN（实现后）**：
   - `python -m pytest backend/tests/test_rnd317_export_audit.py -q` 全绿（见下测试清单）。
   - `python -m pytest backend/tests/ -q` 全量无回归（重点：架构边界、auth、现有 audit 列表测试）。
   - 手动冒烟：`POST /api/admin/export/record`（带 `format`/`scope`）→ 200 且 `recorded=true`；直接查 `audit_logs` 确认有一行 `action='export.executed'`、`object_type='export'`、本租户 `tenant_id`、`detail` 含 `format/record_count/params_hash/gate_enforced`，且**不含**任何消息体键。

---

## 7. 硬约束（违反即判退回）

1. **A7 闸口**：`app.audit.write_audit` / `AuditLog` 缺失则 STOP 报 BLOCKED，绝不自建审计模块、绝不绕过。
2. **SF-1 数据最小化**：`audit_logs.detail` **绝不**存消息体 / 解密 payload / `content_text` / `decrypted_payload`；只存 `format`/`record_count`/`params_hash`/`approval_ref`(SHA-256)/`gate_enforced` 等非敏感上下文。计数查询仅用 `func.count(ArchiveMessage.id)` + 元数据过滤列，绝不 `SELECT content_text`。
3. **租户隔离**：`tenant_id` **仅**来自 `get_current_user` 解包；计数与审计均强制 `tenant_id ==` 过滤；跨租户导出一律隔离（防串味）。
4. **范围守门（见 §4 落点表）**：仅可改 `audit.py`(常量+函数) / 新建 `routers/export_audit.py` / `main.py`(一行 import+一行注册) / 新建测试。
   - **禁止**：生成 PDF/Excel 文件（C2-1）；实现审批 gate 本身（RND-316）；触碰 `backend/scripts/`；改 `.env.example`；改前端；新建其他 flat service 模块；引入新依赖（除栈内 stdlib）。
5. **无新迁移**：本票复用 `audit_logs`，**不得**新建任何 model / Alembic 迁移（若误加，QA 会判退回）。
6. **路径避让**：端点必须是 `/api/admin/export/record`，**不得**用 `/approve`、`/execute`（RND-316 占用）。
7. **路由基线（不写死）**：`tests/test_http_contract.py:326` 当前 `assert route_count == 52`（含 RND-302 +2、RND-297 +1）。本票 +1 端点 → 改为 `== 53`，注释追加 `# RND-317: +1 export audit record route.`。**实现时务必先 `grep` 当前真实值 N，改 N+1，禁止凭记忆写死**（本仓曾因写死漂移值返工）。
8. **不 commit / 不 push**：交付代码后停下，由用户本人提交（或交 QA agent 验收后由用户提交）。
9. **D1 冻结**：本票纯后端，不引入 React / 不碰 `review_console.html` 等前端。

---

## 8. 收尾

- 新建 `backend/tests/test_rnd317_export_audit.py`，至少覆盖：
  - 单元：`record_export_audit` 调用后 `audit_logs` 出现 `action='export.executed'`、`object_type='export'`、正确 `tenant_id`/`admin_user_id`、`detail` 含 `format`/`record_count`/`params_hash`/`gate_enforced`；`detail` 中**不存在** `content`/`payload`/`body`/`decrypted`/`content_text` 等键（SF-1 断言）。
  - API：`POST /api/admin/export/record` → 200 + `recorded=true`；`audit_logs` 确有对应行；跨租户会话调用后审计行**仅属本租户**。
  - 软闸门：当 `app.export_approval` 不可导入时，`gate_enforced=False` 且仍成功记录；当可导入且 `approval_token` 无效 → 403；有效 → `gate_enforced=True`。
  - 路由基线：测试 `test_http_contract.py` 通过（值为 N+1，见 §7.7）。
  - 回归：架构边界测试无新增模块故仍绿；现有 auth/audit 测试无回归。
- 全部绿后，**停止并提交给 QA agent**（不要自行 commit）。
- 在回复中简述：实际路由基线取值、A7 自检结果、RND-316 是否已合并（影响 `gate_enforced` 路径）、是否有任何超出 §4 范围的改动（如有，先停下报告）。
