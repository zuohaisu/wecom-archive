"""Atomic self-service organization provisioning (RND-348)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType
from app.auth import get_session_ttl_hours
from app.db.models import (
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    AuditLog,
    Tenant,
    TenantWecomConfig,
    ThirdPartyOrganizationBinding,
    WecomOrganizationClaim,
)


class OrganizationProvisioningError(RuntimeError):
    pass


class OrganizationProvisioningConflict(OrganizationProvisioningError):
    pass


@dataclass(frozen=True)
class ProvisioningResult:
    tenant_id: str
    session_id: str


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_slug(corp_id: str) -> str:
    return f"organization-{hashlib.sha256(corp_id.encode('utf-8')).hexdigest()[:12]}"


def _existing_result(db: Session, claim: WecomOrganizationClaim) -> ProvisioningResult | None:
    if not claim.provisioned_tenant_id or not claim.provisioning_session_id:
        return None
    session = (
        db.query(AdminSession)
        .filter(
            AdminSession.id == claim.provisioning_session_id,
            AdminSession.tenant_id == claim.provisioned_tenant_id,
            AdminSession.session_scope == "provisioning",
            AdminSession.is_revoked.is_(False),
        )
        .first()
    )
    if session is None:
        return None
    return ProvisioningResult(claim.provisioned_tenant_id, session.id)


def provision_organization(db: Session, raw_claim_ref: str | None) -> ProvisioningResult:
    if not raw_claim_ref:
        raise OrganizationProvisioningError("Organization claim is required")
    now = datetime.now(timezone.utc)
    claim = (
        db.query(WecomOrganizationClaim)
        .filter(
            WecomOrganizationClaim.public_ref_hash == _digest(raw_claim_ref),
            WecomOrganizationClaim.expires_at > now,
        )
        .with_for_update()
        .first()
    )
    if claim is None:
        raise OrganizationProvisioningError("Organization claim is invalid or expired")
    existing_result = _existing_result(db, claim)
    if claim.state == "consumed" and existing_result is not None:
        return existing_result
    if claim.state not in {"pending", "confirmed"}:
        raise OrganizationProvisioningError("Organization claim has already been used")

    existing_config = (
        db.query(TenantWecomConfig.id)
        .filter(TenantWecomConfig.corp_id == claim.corp_id, TenantWecomConfig.is_active.is_(True))
        .first()
    )
    existing_binding = (
        db.query(ThirdPartyOrganizationBinding)
        .filter(ThirdPartyOrganizationBinding.corp_id == claim.corp_id)
        .first()
    )
    if existing_config is not None or existing_binding is not None:
        raise OrganizationProvisioningConflict("Organization already exists")

    tenant_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    # admin_sessions.id is VARCHAR(36) in PostgreSQL.  Keep the cookie value
    # unguessable while matching that production constraint exactly.
    session_id = str(uuid.uuid4())
    tenant = Tenant(
        id=tenant_id,
        name=claim.corp_name,
        slug=_safe_slug(claim.corp_id),
        lifecycle_status="provisioning",
    )
    db.add(tenant)
    db.add(
        ThirdPartyOrganizationBinding(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            corp_id=claim.corp_id,
            agent_id=claim.agent_id,
            permanent_code_encrypted=claim.permanent_code_encrypted,
            authorization_mode="admin",
        )
    )
    owner = AdminUser(
        id=owner_id,
        tenant_id=tenant_id,
        wecom_user_id=claim.authorized_subject,
        role="owner",
        status="active",
    )
    db.add(owner)
    db.add(
        AdminLoginIdentity(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider="wecom_third_party",
            subject=claim.authorized_subject,
            admin_user_id=owner_id,
            verified_at=now,
        )
    )
    db.add(
        AdminSession(
            id=session_id,
            admin_user_id=owner_id,
            tenant_id=tenant_id,
            wecom_user_id=claim.authorized_subject,
            session_scope="provisioning",
            expires_at=now + timedelta(hours=get_session_ttl_hours()),
            is_revoked=False,
        )
    )
    claim.state = "consumed"
    claim.confirmed_at = claim.confirmed_at or now
    claim.consumed_at = now
    claim.provisioned_tenant_id = tenant_id
    claim.provisioning_session_id = session_id
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            admin_user_id=owner_id,
            action=AuditAction.ORGANIZATION_PROVISIONED,
            object_type=AuditObjectType.TENANT,
            object_id=tenant_id,
            detail={"lifecycle_status": "provisioning"},
            created_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        winner = (
            db.query(WecomOrganizationClaim)
            .filter(WecomOrganizationClaim.public_ref_hash == _digest(raw_claim_ref))
            .first()
        )
        result = _existing_result(db, winner) if winner is not None else None
        if result is not None:
            return result
        raise OrganizationProvisioningConflict("Organization provisioning conflict") from exc
    except Exception:
        db.rollback()
        raise
    return ProvisioningResult(tenant_id, session_id)
