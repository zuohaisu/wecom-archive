"""Platform-console tenant provisioning routes (RND-311)."""

from __future__ import annotations

from datetime import timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import get_wecom_token, require_platform_admin
from app.db.models import (
    AdminSession,
    DuplicateCorpIdError,
    Tenant,
    TenantBranding,
    TenantWecomConfig,
)
from app.db.session import get_db
from app.routers.auth import _create_pending_invite
from app.schemas.branding import (
    BrandingDomainMetricsOut,
    BrandingStatusOut,
    CertificateStatusIn,
    ManagedBrandingDomainOut,
)
from app.schemas.tenant_provision import (
    TenantListItemOut,
    TenantConnectivityCheckOut,
    TenantListOut,
    TenantProvisionIn,
    TenantProvisionOut,
    TenantStatusUpdateIn,
    TenantStatusUpdateOut,
    TenantUsageItemOut,
    TenantUsageListOut,
)
from app.services.branding import (
    BrandingValidationError,
    DomainStateError,
    branding_status,
    record_certificate_status,
    utc_now,
)
from app.services.usageservice import (
    count_messages,
    count_monitored_employees,
    sum_storage,
    sync_health,
)

router = APIRouter()


@router.post(
    "/tenants",
    response_model=TenantProvisionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    payload: TenantProvisionIn,
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantProvisionOut:
    """Create a tenant and encrypt its WeCom credentials before persistence."""
    if db.query(Tenant).filter(Tenant.slug == payload.slug).first():
        raise HTTPException(status_code=409, detail="Tenant slug already exists")

    tenant = Tenant(
        id=str(uuid.uuid4()),
        name=payload.name,
        slug=payload.slug,
        lifecycle_status="active",
    )
    db.add(tenant)
    config = TenantWecomConfig(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        corp_id=payload.corp_id,
        agent_id=payload.agent_id,
        callback_domain=payload.callback_domain,
        is_active=True,
    )
    config.set_credentials(payload.secret, payload.private_key_pem)
    db.add(config)
    try:
        db.commit()
    except DuplicateCorpIdError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This WeCom CorpID is already assigned to another tenant.",
        )

    db.refresh(config)
    owner_invite_sent = True
    try:
        _create_pending_invite(
            db,
            tenant_id=tenant.id,
            admin_user_id=None,
            email=payload.owner_email,
            name=None,
            role="owner",
        )
    except Exception:
        owner_invite_sent = False

    return TenantProvisionOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        config_id=config.id,
        corp_id=config.corp_id,
        agent_id=config.agent_id,
        is_active=config.is_active,
        owner_invite_sent=owner_invite_sent,
    )


@router.get("/tenants", response_model=TenantListOut)
def list_tenants(
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantListOut:
    """Return all provisioned tenants with their config status (no key leakage)."""
    rows = (
        db.query(Tenant, TenantWecomConfig)
        .outerjoin(TenantWecomConfig, TenantWecomConfig.tenant_id == Tenant.id)
        .all()
    )
    return TenantListOut(
        tenants=[
            TenantListItemOut(
                tenant_id=t.id,
                tenant_name=t.name,
                tenant_slug=t.slug,
                corp_id=c.corp_id if c is not None else None,
                agent_id=c.agent_id if c is not None else None,
                tenant_is_active=t.is_active,
                config_is_active=c.is_active if c is not None else False,
                created_at=t.created_at,
            )
            for t, c in rows
        ]
    )


@router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantStatusUpdateOut,
    status_code=status.HTTP_200_OK,
)
def update_tenant_status(
    tenant_id: str,
    payload: TenantStatusUpdateIn,
    admin_user=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantStatusUpdateOut:
    """Activate or deactivate a tenant (platform admin only).

    - **404**: Target tenant does not exist.
    - **401**: Missing or invalid platform admin credentials.
    - Writes an audit log only for a real state transition.
    """
    # Find the target tenant
    tenant = (
        db.query(Tenant).filter(Tenant.id == tenant_id).first()
    )
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant with id '{tenant_id}' not found",
        )

    old_status = tenant.is_active
    old_lifecycle_status = tenant.lifecycle_status
    new_status = payload.is_active
    if old_status != new_status:
        tenant.is_active = new_status
        tenant.lifecycle_status = "active" if new_status else "suspended"
        promoted_sessions = 0
        if new_status and old_lifecycle_status == "provisioning":
            promoted_sessions = (
                db.query(AdminSession)
                .filter(
                    AdminSession.tenant_id == tenant_id,
                    AdminSession.session_scope == "provisioning",
                    AdminSession.is_revoked.is_(False),
                )
                .update(
                    {AdminSession.session_scope: "admin"},
                    synchronize_session=False,
                )
            )
        audit_detail = {
            "platform_admin_id": admin_user.id,
            "previous_is_active": old_status,
            "is_active": new_status,
        }
        if promoted_sessions:
            audit_detail["promoted_provisioning_sessions"] = promoted_sessions
        write_audit(
            db,
            tenant_id=tenant_id,
            action=(
                AuditAction.PLATFORM_TENANT_DEACTIVATED
                if not new_status else AuditAction.PLATFORM_TENANT_ACTIVATED
            ),
            object_type=AuditObjectType.TENANT,
            # PlatformAdmin is tenant-less and cannot satisfy this FK.
            admin_user_id=None,
            object_id=tenant_id,
            detail=audit_detail,
        )
        # Persist the state transition and its audit row together.
        db.commit()
        db.refresh(tenant)

    return TenantStatusUpdateOut(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        tenant_is_active=tenant.is_active,
        updated_at=tenant.updated_at,
    )


@router.get(
    "/branding/domains",
    response_model=list[ManagedBrandingDomainOut],
)
def list_managed_branding_domains(
    _platform_admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[ManagedBrandingDomainOut]:
    """List non-secret custom-domain work for the managed TLS controller."""
    rows = (
        db.query(TenantBranding)
        .join(Tenant, Tenant.id == TenantBranding.tenant_id)
        .filter(
            Tenant.is_active.is_(True),
            TenantBranding.custom_domain.isnot(None),
            TenantBranding.domain_state.in_(("verified", "active")),
        )
        .order_by(TenantBranding.certificate_expires_at)
        .all()
    )
    return [
        ManagedBrandingDomainOut(
            tenant_id=row.tenant_id,
            hostname=row.custom_domain,
            domain_state=row.domain_state,
            domain_enabled=row.domain_enabled,
            certificate_status=row.certificate_status,
            certificate_expires_at=row.certificate_expires_at,
            certificate_last_checked_at=row.certificate_last_checked_at,
        )
        for row in rows
    ]


@router.get(
    "/branding/domain-metrics",
    response_model=BrandingDomainMetricsOut,
)
def managed_branding_domain_metrics(
    _platform_admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> BrandingDomainMetricsOut:
    """Return aggregate lifecycle counters without DNS values or certificate material."""
    now = utc_now()
    rows = (
        db.query(TenantBranding)
        .join(Tenant, Tenant.id == TenantBranding.tenant_id)
        .filter(Tenant.is_active.is_(True), TenantBranding.custom_domain.isnot(None))
        .all()
    )
    normalized_expiries = {
        row.id: (
            row.certificate_expires_at.replace(tzinfo=timezone.utc)
            if row.certificate_expires_at is not None and row.certificate_expires_at.tzinfo is None
            else row.certificate_expires_at
        )
        for row in rows
    }
    invalid_bindings = 0
    for row in rows:
        expires_at = normalized_expiries[row.id]
        valid = (
            row.domain_state == "active"
            and row.domain_enabled
            and row.certificate_status == "issued"
            and expires_at is not None
            and expires_at > now
        )
        if row.domain_enabled and not valid:
            invalid_bindings += 1
    return BrandingDomainMetricsOut(
        pending_verification_count=sum(row.domain_state == "pending_verification" for row in rows),
        pending_certificate_count=sum(row.certificate_status == "pending" for row in rows),
        certificates_expiring_30_days_count=sum(
            row.certificate_status == "issued"
            and normalized_expiries[row.id] is not None
            and now < normalized_expiries[row.id] <= now + timedelta(days=30)
            for row in rows
        ),
        certificate_failure_count=sum(row.certificate_status in {"failed", "expired"} for row in rows),
        invalid_binding_count=invalid_bindings,
    )


@router.post(
    "/tenants/{tenant_id}/branding/certificate-status",
    response_model=BrandingStatusOut,
)
def report_tenant_branding_certificate_status(
    tenant_id: str,
    payload: CertificateStatusIn,
    platform_admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> BrandingStatusOut:
    """Receive a coarse lifecycle report from the trusted managed TLS edge.

    Tenant administrators cannot call this route and no certificate, private
    key, challenge token, provider response, or topology is accepted/stored.
    The caller is expected to be the platform's certificate controller after
    it has issued/renewed/deployed the certificate at the edge.
    """
    try:
        config = record_certificate_status(
            db,
            tenant_id,
            status=payload.status,
            expires_at=payload.expires_at,
            failure_code=payload.failure_code,
        )
    except (BrandingValidationError, DomainStateError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=None,
        action=AuditAction.BRANDING_CERTIFICATE_STATUS_UPDATED,
        object_type=AuditObjectType.TENANT_CONFIG,
        object_id=config.id,
        detail={"status": payload.status, "platform_admin_id": platform_admin.id},
    )
    db.commit()
    return BrandingStatusOut(**branding_status(db, tenant_id))


_INVALID_CREDENTIAL_ERRCODES = {"40001", "40013", "40125"}


def _connectivity_failure_reason(error: RuntimeError) -> str:
    """Map known token failures to safe, coarse response categories."""
    message = str(error)
    if message == "Failed to fetch WeCom access_token":
        return "network_error"
    if any(f"errcode={code}" in message for code in _INVALID_CREDENTIAL_ERRCODES):
        return "invalid_credentials"
    return "unknown"


@router.post(
    "/tenants/{tenant_id}/connectivity-check",
    response_model=TenantConnectivityCheckOut,
    response_model_exclude_none=True,
)
def check_tenant_connectivity(
    tenant_id: str,
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantConnectivityCheckOut:
    """Check whether the tenant's stored WeCom credentials can obtain a token."""
    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant_id)
        .first()
    )
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant WeCom configuration not found",
        )

    try:
        # Isolate this from production consumers. A repeated check may reuse
        # this key's cache for up to two hours; absolute bypass is out of scope.
        get_wecom_token(
            config.corp_id,
            config.decrypted_app_secret,
            cache_key=f"connectivity-check:{tenant_id}",
        )
    except RuntimeError as error:
        return TenantConnectivityCheckOut(
            ok=False,
            reason=_connectivity_failure_reason(error),
        )
    return TenantConnectivityCheckOut(ok=True)


@router.get("/tenants/usage", response_model=TenantUsageListOut)
def list_tenant_usage(
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantUsageListOut:
    """Return read-only usage summaries for every tenant, including inactive ones."""
    tenants = db.query(Tenant).all()
    # First release accepts these per-tenant aggregates; batch them if tenant
    # cardinality grows enough for the N+1 queries to become material.
    return TenantUsageListOut(
        tenants=[
            TenantUsageItemOut(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                message_count=count_messages(db, tenant_id=tenant.id),
                storage_bytes=sum_storage(db, tenant_id=tenant.id),
                employee_count=count_monitored_employees(db, tenant_id=tenant.id),
                sync_health=sync_health(db, tenant_id=tenant.id),
            )
            for tenant in tenants
        ]
    )
