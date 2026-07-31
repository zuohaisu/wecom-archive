"""Tenant-scoped first-run onboarding status endpoints (RND-304)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, Tenant
from app.db.session import get_db
from app.schemas.onboarding import OnboardingCompleteOut, OnboardingStatusOut

router = APIRouter()


@router.get("/api/onboarding/status", response_model=OnboardingStatusOut)
def get_onboarding_status(
    auth: tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> OnboardingStatusOut:
    """Return whether the authenticated tenant has not completed onboarding."""
    _, tenant_id = auth
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).one()
    return OnboardingStatusOut(first_run=tenant.onboarding_completed_at is None)


@router.post("/api/onboarding/complete", response_model=OnboardingCompleteOut)
def complete_onboarding(
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
) -> OnboardingCompleteOut:
    """Mark onboarding complete once; subsequent calls preserve its timestamp."""
    _, tenant_id = auth
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).one()
    if tenant.onboarding_completed_at is None:
        tenant.onboarding_completed_at = datetime.now(timezone.utc)
        db.commit()
    return OnboardingCompleteOut(first_run=False)
