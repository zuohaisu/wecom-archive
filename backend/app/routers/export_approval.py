"""HTTP approval and enforcement surface for the RND-316 export gate."""
from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import get_current_user, verify_password
from app.db.models import AdminUser
from app.db.session import get_db
from app.export_approval import (
    ExportNotApprovedError,
    issue_export_approval,
    require_export_approval,
)

router = APIRouter(prefix="/api/admin/export", tags=["export-approval"])


class ExportApprovalRequest(BaseModel):
    password: str
    params: dict


@router.post("/approve")
def approve_export(
    req: ExportApprovalRequest,
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reconfirm the caller's password and issue a bound approval token."""
    user, tenant_id = auth
    if not verify_password(req.password, user.password_hash):
        write_audit(
            db,
            tenant_id=tenant_id,
            action=AuditAction.EXPORT_APPROVAL_DENIED,
            object_type=AuditObjectType.EXPORT_APPROVAL_TOKEN,
            admin_user_id=user.id,
            detail={"reason": "bad_password"},
        )
        # There is no primary mutation on denial, so commit the required audit
        # row before returning the HTTP error.
        db.commit()
        raise HTTPException(401, "Invalid password")

    raw_token, expires_at = issue_export_approval(
        db=db,
        admin_user_id=user.id,
        tenant_id=tenant_id,
        params=req.params,
    )
    db.commit()
    return {"approval_token": raw_token, "expires_at": expires_at.isoformat()}


class ExportExecuteRequest(BaseModel):
    approval_token: str
    params: dict


@router.post("/execute")
def execute_export(
    req: ExportExecuteRequest,
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enforce the gate only; file generation belongs to C2-1."""
    user, tenant_id = auth
    try:
        require_export_approval(
            db=db,
            token=req.approval_token,
            params=req.params,
            admin_user_id=user.id,
            tenant_id=tenant_id,
        )
    except ExportNotApprovedError as error:
        raise HTTPException(403, str(error))
    db.commit()
    return {"allowed": True}
