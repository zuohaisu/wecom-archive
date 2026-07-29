# RND-316 开发执行提示词 — C2-2 导出安全审批 gate（合规评审）

> 本文档是交给**开发 agent** 的执行 brief。交付物 = 本文件 + 同目录 `RND-316-qa-prompt.md`（验收 brief）。
> 工程纪律（来自 issue + `DEV_AGENT_RULES.md`）：**Agent 绝不 `git commit` / `git push`**；不引入 React（架构冻结 D1）；代码标识符加反引号；范围守门（见 §7）。

---

## 1. 任务概述（来自 Linear RND-316）

- **标题**：`[BLOCKED: A7] C2-2 安全审批 gate（合规评审）`
- **Epic**：RND-271 · C2 证据导出（PDF/Excel）
- **Scope（后端）**：导出前安全审批 gate（如需要审批令牌 / 二次确认）。
- **Dependencies**：A7（Audit）。
- **Non-goals（本票）**：不做实际 PDF/Excel 文件生成（那是 C2-1 / RND-315 的职责）；不做导出审计钩子的落地写入（那是 C2-3 / RND-317）；不做前端。
- **Acceptance Criteria**：审批 gate 生效；未审批拒绝导出。
- **Estimate**：0.75w · **Labels**：backend, export, audit。

**本票交付一个可复用的「导出审批 gate」原语**（令牌签发 + 校验）+ 一个「二次确认」签发端点 + 一个「gate 强制面」端点（仅做闸门校验、不生成文件，供验收与未来 C2-1 消费端对接）。所有审批动作记入 `audit_logs`（A7）。

---

## 2. 决策背景与基线核实（已替你核实，开发 agent 仍需开工自检）

RND-316 标着 `BLOCKED: A7`。但工作树现状表明 **A7 基础设施已落地**：
- `backend/app/db/models.py:232` — `AuditLog` 模型（`audit_logs` 表，不可变 append-only）。
- `backend/app/audit.py` — `write_audit(...)` 审计写入钩子（RND-294 / A7-2），fail-safe、只 add+flush 不 commit。
- Alembic 迁移 `0019_audit_log.py` 已存在。

**`PasswordResetToken`（`models.py:265-278`，RND-278 / F0-3）是本项目「哈希化、单行用、有过期」令牌表的现成范式** —— 本票的 `ExportApprovalToken` 必须**完全镜像**它的字段设计与「只存 SHA-256、原始令牌只出现一次」约定（SF-1 数据最小化）。

**关键现实**：C2-1 导出服务（RND-315）当前**尚未实现**（全仓 grep `export|reportlab|openpyxl|fpdf` 无命中）。因此本票的 gate 是一个**独立可测的守卫原语**，不包裹任何既有端点；「未审批拒绝导出」通过 (a) `require_export_approval()` 单元的拒绝路径 + (b) `/execute` 网关端点的 HTTP 403/200 来验证。

### 2.1 设计决策（已拍板，开发 agent 照办）

1. **双重机制同时满足「审批令牌 / 二次确认」**：
   - **二次确认** = `/approve` 端点要求调用方用 `verify_password` 重新校验自己的登录密码（sudo 式在场证明）。
   - **审批令牌** = 校验通过后签发一个**短时效、单行用**的 `approval_token`（默认 TTL 300s），原始令牌仅返回一次，库中只存 SHA-256。
2. **`params_hash` 契约**：`/approve` 与未来 `/execute` 的生成消费端必须对**同一份导出请求参数**算出相同的 `params_hash`（canonical JSON，`sort_keys` + 紧凑分隔符），否则 gate 拒绝。这是把「令牌」和「实际要导出的内容」绑定的核心，防止用 A 的审批去导出 B 的内容。
3. **不新建任何 secret / 不引依赖**：令牌 = `secrets.token_urlsafe(32)` 原始值 + `sha256` 入库，与 `password_reset_tokens` 一致，无需服务端密钥。
4. **`/execute` 是「gate 强制面」而非导出端点**：它**只**调用 `require_export_approval` 并返回 `{"allowed": true}`；**不生成任何文件**、不触碰消息内容。真正的文件生成归 C2-1。QA 用它证明「未审批拒绝导出」的 HTTP 契约。

---

## 3. 前置依赖与开工闸口

### 3.1 A7 自检（开工第一步，必须做）
用 import 断言确认 A7 已在树：
```python
from app.audit import write_audit          # 必须存在
from app.db.models import AuditLog         # 必须存在
from app.auth import verify_password, get_current_user  # 必须存在
```
- **若任一缺失** → 立即停止，在回复中报告 `BLOCKED: A7 基础设施未就位（缺 app.audit.write_audit / AuditLog 模型）`，**不得自建审计模块、不得绕过 gate**。
- 若俱全 → 继续。注：Linear 仍标 `BLOCKED: A7`，派发前请你确认 A7 已真正合并（工作树现状显示已落地）。

### 3.2 其他已存在的依赖（不应重建）
`AdminUser`（`password_hash` / `id` 来自 F0-1，已落地）、`AdminSession`、`get_current_user` 返回的 `(AdminUser, tenant_id)` 授权范围、`verify_password`。

---

## 4. 精确落点（`文件:行号:函数`）

| 落点 | 坐标 | 改动 |
|---|---|---|
| 令牌模型 | `backend/app/db/models.py:278` 之后（紧接 `PasswordResetToken`） | 新增 `ExportApprovalToken` 类 |
| 迁移 | `backend/alembic/versions/`（**动态 `down_revision`**） | 新增 `00xx_export_approval_tokens.py` |
| gate 原语 | `backend/app/export_approval.py`（**新建 flat 模块**） | `issue_export_approval` / `require_export_approval` / `ExportNotApprovedError` / `_params_hash` |
| 审计常量 | `backend/app/audit.py:32` `AuditAction` 类 + `:47` `AuditObjectType` 类 | 扩展导出审批动作/对象常量 |
| 路由 | `backend/app/routers/export_approval.py`（**新建**，`APIRouter(prefix="/api/admin/export")`） | `/approve`、`/execute` |
| 装配 | `backend/app/main.py:108`（`app.include_router(messages_router)` 之后） | `app.include_router(export_approval_router)` |
| 边界登记 | `backend/tests/test_architecture_boundary.py:70` `_FLAT_SERVICE_MODULES` 集合 | 加入 `"app.export_approval"` |
| 测试 | `backend/tests/test_rnd316_export_approval.py`（**新建**） | 单元 + API 契约 |

**关键既有签名（务必对齐）**：
- `app/auth.py:319 get_current_user(session_id: Optional[str] = Cookie(...), db) -> Tuple[AdminUser, str]` —— 返回 `(user, tenant_id)`，`tenant_id` 是全量 admin 查询的**唯一授权范围**，绝不接受请求体里的 tenant_id。
- `app/auth.py:144 verify_password(plain: str, stored_hash: str) -> bool`。
- `app/audit.py:54 write_audit(db, *, tenant_id, action, object_type, admin_user_id=None, object_id=None, detail=None)` —— **只 add+flush，不 commit**；调用方在自己的 `db.commit()` 中原子持久化；fail-safe 永不抛。
- `AuditLog.detail`（`models.py:258`，`JSONB`）**严禁**写入消息体或解密 payload（SF-1）。

---

## 5. 实现步骤

### 5.1 模型 `ExportApprovalToken`（`models.py`，紧接 `PasswordResetToken` 之后）
```python
# —— RND-316 (C2-2) 导出安全审批令牌 ——
class ExportApprovalToken(Base):
    __tablename__ = "export_approval_tokens"

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # 只存原始令牌的 SHA-256 hex；原始令牌仅出现在 /approve 返回值与浏览器/调用方，不入库。
    token = Column(String(64), nullable=False, unique=True, index=True)
    # 导出请求参数的 SHA-256（canonical JSON），用于将令牌绑定到具体导出内容。
    params_hash = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```
> 必须让 ORM 元数据与该迁移**逐字一致**（含 `func.now()` 默认、`Boolean`、`String(64)`、`unique=True`+`index=True`），否则 `alembic check`（RND-227 漂移门）会红。

### 5.2 迁移 `00xx_export_approval_tokens.py`（镜像 `0019_audit_log.py` 风格）
- `revision` = 动态（见 §7.3）；`down_revision` = `alembic heads` 的输出（当前预期 `0019`，但**以运行时为准**）。
- `upgrade()`：`op.create_table("export_approval_tokens", ...)` 列集与 5.1 完全一致；`sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"])`、`(["admin_user_id"... 实为 admin_user_id -> admin_users.id])`；`PrimaryKeyConstraint("id")`；`op.create_index` 三个：`ix_export_approval_tokens_tenant_id`、`_admin_user_id`、`_params_hash`（参考 `password_reset_tokens` 迁移的索引命名）。
- `downgrade()`：逆序 drop。

### 5.3 gate 原语 `app/export_approval.py`（新建 flat 模块）
```python
"""RND-316 (C2-2) 导出安全审批 gate。

提供令牌签发(issue)与校验(require)。原始令牌只返回一次；库内仅存 SHA-256。
所有审批动作通过 app.audit.write_audit 记入 audit_logs（A7），detail 仅含
params_hash / expires_at 等非敏感上下文（SF-1）。
"""
from __future__ import annotations
import hashlib, json, secrets, uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from sqlalchemy.orm import Session
from app.db.models import ExportApprovalToken
from app.audit import write_audit, AuditAction, AuditObjectType

DEFAULT_APPROVAL_TTL_SECONDS = 300

class ExportNotApprovedError(Exception):
    """require_export_approval 在 gate 拒绝时抛出；路由层映射为 HTTP 403。"""

def _params_hash(params: Mapping[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def issue_export_approval(*, db: Session, admin_user_id: str, tenant_id: str,
                          params: Mapping[str, Any],
                          ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS) -> tuple[str, datetime]:
    raw = secrets.token_urlsafe(32)
    token_sha = hashlib.sha256(raw.encode()).hexdigest()
    params_hash = _params_hash(params)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    row = ExportApprovalToken(
        id=str(uuid.uuid4()), admin_user_id=admin_user_id, tenant_id=tenant_id,
        token=token_sha, params_hash=params_hash, expires_at=expires_at, used=False,
    )
    db.add(row); db.flush()
    write_audit(db, tenant_id=tenant_id, action=AuditAction.EXPORT_APPROVAL_GRANTED,
                object_type=AuditObjectType.EXPORT_APPROVAL_TOKEN, admin_user_id=admin_user_id,
                object_id=row.id,
                detail={"params_hash": params_hash, "expires_at": expires_at.isoformat()})
    return raw, expires_at  # 原始令牌仅此一处返回

def require_export_approval(*, db: Session, token: Optional[str], params: Mapping[str, Any],
                            admin_user_id: str, tenant_id: str) -> None:
    """gate 校验。通过则不返回值；任何不满足都抛 ExportNotApprovedError。"""
    if not token:
        raise ExportNotApprovedError("missing approval token")
    token_sha = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    row = db.query(ExportApprovalToken).filter(ExportApprovalToken.token == token_sha).first()
    if row is None or row.tenant_id != tenant_id or row.admin_user_id != admin_user_id:
        raise ExportNotApprovedError("invalid approval token")
    if row.used:
        raise ExportNotApprovedError("approval token already consumed")
    if row.expires_at <= now:
        raise ExportNotApprovedError("approval token expired")
    if row.params_hash != _params_hash(params):
        raise ExportNotApprovedError("approval token params mismatch")
    row.used = True  # 单行用；由调用方 db.commit() 持久化
    write_audit(db, tenant_id=tenant_id, action=AuditAction.EXPORT_APPROVAL_CONSUMED,
                object_type=AuditObjectType.EXPORT_APPROVAL_TOKEN, admin_user_id=admin_user_id,
                object_id=row.id, detail={"params_hash": row.params_hash})
```

### 5.4 扩展 `app/audit.py` 常量（小改，落在既有类内）
`AuditAction`（`:32` 之后）增加：
```python
EXPORT_APPROVAL_GRANTED = "export.approval_granted"
EXPORT_APPROVAL_CONSUMED = "export.approval_consumed"
EXPORT_APPROVAL_DENIED  = "export.approval_denied"
```
`AuditObjectType`（`:47` 之后）增加：
```python
EXPORT_APPROVAL_TOKEN = "export_approval_token"
```

### 5.5 路由 `app/routers/export_approval.py`（新建）
```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.auth import get_current_user, verify_password
from app.db.session import get_db
from app.export_approval import (
    issue_export_approval, require_export_approval, ExportNotApprovedError,
)
from app.audit import write_audit, AuditAction, AuditObjectType

router = APIRouter(prefix="/api/admin/export", tags=["export-approval"])

class ExportApprovalRequest(BaseModel):
    password: str            # 二次确认：重新校验登录密码
    params: dict             # 将审批的导出请求（过滤器/格式/ids）；会 canon 成 params_hash

@router.post("/approve")
def approve_export(req: ExportApprovalRequest,
                   auth=Depends(get_current_user), db: Session = Depends(get_db)):
    user, tenant_id = auth
    if not verify_password(req.password, user.password_hash):
        write_audit(db, tenant_id=tenant_id, action=AuditAction.EXPORT_APPROVAL_DENIED,
                    object_type=AuditObjectType.EXPORT_APPROVAL_TOKEN, admin_user_id=user.id,
                    detail={"reason": "bad_password"})
        raise HTTPException(401, "Invalid password")
    raw, expires_at = issue_export_approval(
        db=db, admin_user_id=user.id, tenant_id=tenant_id, params=req.params)
    db.commit()
    return {"approval_token": raw, "expires_at": expires_at.isoformat()}

class ExportExecuteRequest(BaseModel):
    approval_token: str
    params: dict

@router.post("/execute")
def execute_export(req: ExportExecuteRequest,
                   auth=Depends(get_current_user), db: Session = Depends(get_db)):
    # GATE 强制面 ONLY —— 真正的文件生成归 C2-1；本端点不产出任何文件、不碰消息内容。
    user, tenant_id = auth
    try:
        require_export_approval(db=db, token=req.approval_token, params=req.params,
                                admin_user_id=user.id, tenant_id=tenant_id)
    except ExportNotApprovedError as e:
        raise HTTPException(403, str(e))
    db.commit()
    return {"allowed": True}
```

### 5.6 装配 `main.py`
在 `app.include_router(messages_router)`（`main.py:108`）之后加一行：
```python
from app.routers.export_approval import router as export_approval_router
...
app.include_router(export_approval_router)
```
（模块级 import 与其他 router 保持同区；不要在 `create_app` 内做 import。）

### 5.7 边界登记 `test_architecture_boundary.py`
在 `_FLAT_SERVICE_MODULES`（`:70` 起）集合中加入 `"app.export_approval"`（与 `app.audit`、`app.session_lifecycle` 同列）。新建的 `app/routers/export_approval.py` 自动归 `router` 层，无需登记。

---

## 6. RED → GREEN 验证（开发 agent 自检）

1. **RED**：跑现有边界测试 `python -m pytest backend/tests/test_architecture_boundary.py -q` —— 预期因 `app.export_approval` 未登记而 FAIL（若你在 5.7 之前跑）。
2. **GREEN（实现后）**：
   - `alembic upgrade head` 成功；`alembic check` 绿（ORM↔迁移无漂移）。
   - `python -m pytest backend/tests/test_rnd316_export_approval.py -q` 全绿（见下测试清单）。
   - `python -m pytest backend/tests/ -q` 全量无回归（重点：架构边界、auth、audit 列表测试）。
   - 手动冒烟：`POST /api/admin/export/approve`（密码正确）→ 200 + `approval_token`；用该 token `POST /api/admin/export/execute` → 200 `{"allowed": true}`；重放同 token → 403（已消费）；不带 token → 403。

---

## 7. 硬约束（违反即判退回）

1. **A7 闸口**：`app.audit.write_audit` / `AuditLog` 缺失则 STOP 报 BLOCKED，绝不自建审计、绝不绕过 gate。
2. **SF-1 数据最小化**：`audit_logs.detail` 与 `export_approval_tokens` 表**绝不**存消息体/解密 payload/原始 `params` 全文；只存 `params_hash`(SHA-256) + `expires_at` 等非敏感上下文。原始 `approval_token` 仅 `/approve` 返回一次，库内只存 SHA-256。
3. **租户隔离**：`require_export_approval` 必须校验 `row.tenant_id == tenant_id` **且** `row.admin_user_id == admin_user_id`；跨租户 / 跨管理员的令牌一律拒绝（防串味）。
4. **参数绑定**：`params_hash` 不匹配 → 拒绝（防「用 A 的审批导出 B」）。canonical 化用 `sort_keys=True, separators=(",",":"), ensure_ascii=False`，`/approve` 与 `/execute` 必须一致。
5. **单行用 + 过期**：`used` 标记 + `expires_at` 校验，缺一不可。
6. **B 层纪律**：**严禁**新建/修改 `backend/scripts/` 任何文件（生产定时调度归 RND-237）；本票 gate 由 API 触发，无 worker。
7. **迁移编号**：**绝不硬编码** revision 号；实现时 `alembic heads` 取 `down_revision`（当前预期 `0019`，但 RND-287 若先落地则顺延为 `0020+`）。`alembic check` 必须绿。
8. **范围守门（见 §4 落点表）**：仅可改 `models.py` / 新建迁移 / `export_approval.py` / `audit.py`(常量) / `routers/export_approval.py` / `main.py`(一行) / `test_architecture_boundary.py`(加一项) / 新建测试。
   - **禁止**：实现 C2-1 的 PDF/Excel 生成、实现 C2-3 的审计钩子落地、触碰 `backend/scripts/`、改 `.env.example`、改前端、新建其他 router、引入新依赖（除 stdlib `secrets`/`hashlib`/`json`/`uuid`，均已在栈）。
9. **不 commit / 不 push**：交付代码后停下，由用户本人提交（或交 QA agent 验收后由用户提交）。
10. **D1 冻结**：本票纯后端，不引入 React / 不碰 `review_console.html` 等前端。

---

## 8. 收尾

- 新建 `backend/tests/test_rnd316_export_approval.py`，至少覆盖：
  - 单元：`issue_export_approval` 返回原始令牌 + 过期时间；`require_export_approval` 对**有效令牌通过**；对【缺令牌 / 错令牌 / 已过期 / 已消费 / 跨租户 / 跨管理员 / params_hash 不匹配】**逐一抛 `ExportNotApprovedError`**。
  - API：`/approve` 密码错 → 401 且记 `EXPORT_APPROVAL_DENIED`；密码对 → 200 + token。
  - API `/execute`：无 token → 403；有效 token → 200；重放同 token → 403；params 不一致 → 403；**跨租户会话用 A 的 token → 403**。
  - SF-1 断言：`audit_logs.detail` 中**不存在** `content`/`payload`/`body`/`decrypted` 等键；`export_approval_tokens.token` 列不等于任何曾返回的明文。
  - 迁移：测试库 `alembic upgrade head` + `alembic check` 绿（或 sqlite in-memory 能建 `export_approval_tokens` 表）。
- 全部绿后，**停止并提交给 QA agent**（不要自行 commit）。
- 在回复中简述：实际 `down_revision` 取值、是否触发了 A7 闸口、是否有任何超出 §4 范围的改动（如有，先停下报告）。
