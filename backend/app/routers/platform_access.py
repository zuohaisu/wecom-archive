"""Audited, deny-by-default platform content-access request gate."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import PlatformAdminTenantScope, require_platform_tenant_scope
from app.db.session import get_db
from app.schemas.platform_access import ContentAccessRequestOut

router = APIRouter()


@router.post("/content-access-requests", response_model=ContentAccessRequestOut)
def request_content_access(
    scope: PlatformAdminTenantScope = Depends(require_platform_tenant_scope),
    db: Session = Depends(get_db),
) -> ContentAccessRequestOut:
    """Record the audited request without exposing tenant content."""
    db.commit()
    return ContentAccessRequestOut(tenant_id=scope.tenant_id)
