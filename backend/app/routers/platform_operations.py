"""Platform-only SaaS operations dashboard and controlled manual operations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import require_platform_admin, require_platform_admin_optional
from app.db.models import PlatformAdmin
from app.db.session import get_db
from app.schemas.platform_operations import (
    ControlledOperationIn,
    ManualFinancialTransactionIn,
    ManualFinancialTransactionOut,
    ManualSubscriptionOut,
    ManualSubscriptionUpdateIn,
    PaymentRecoveryFindingResolutionIn,
    PaymentRecoveryFindingResolutionOut,
    PlatformOperationsDashboardOut,
    TenantOperationsDetailOut,
    TenantOperationsListOut,
    TenantServiceStatusOut,
    TenantServiceStatusUpdateIn,
)
from app.services import platform_operations
from app.schemas.billing import PaymentOrderOut
from app.services.payment_orders import (
    PaymentActivationPendingError,
    PaymentOrderConflictError,
    PaymentOrderNotFoundError,
    get_order,
    query_and_reconcile_order,
)
from app.services.alipay import (
    ALIPAY_PROVIDER,
    AlipayConfigurationError,
    AlipayProtocolError,
    alipay_is_enabled,
    get_alipay_provider,
)
from app.services.payment_provider import PaymentProvider
from app.services.payment_recovery import (
    PaymentRecoveryFindingNotFoundError,
    PaymentRecoveryFindingResolutionError,
    finalize_manual_payment_query,
    resolve_historical_callback_signature_finding,
)
from app.services.wechat_pay import (
    WECHAT_PAY_PROVIDER,
    WechatPayConfigurationError,
    WechatPayProtocolError,
    get_wechat_pay_provider,
)
from app.web import render_template
from app.web.sidenav import render_platform_admin_bar, render_platform_sidenav, render_platform_topbar

router = APIRouter(tags=["platform-operations"])

# RND-414: (template name, side-nav active id, topbar breadcrumb) per platform
# page. Every route below is a thin, data-free HTML shell — all rendering is
# client-side, matching the existing platform-operations.js convention; see
# the per-page static/platform-*.js files for the actual API calls.
_PLATFORM_PAGES = {
    # "/platform" itself is registered separately below (operations_page) —
    # it carries a legacy-redirect note and is the one page reachable by
    # its old /platform/operations URL too.
    "/platform/tenants": ("platform_tenants", "tenants", "租户商业状态"),
    "/platform/tenants/new": ("platform_tenant_new", "tenant-new", "新建租户"),
    "/platform/usage": ("platform_usage", "usage", "用量与配额"),
    "/platform/ledger": ("platform_ledger", "ledger", "手工账本"),
    "/platform/infra": ("platform_infra", "infra", "连通性 / 域名 / 渠道"),
    "/platform/analytics": ("platform_analytics", "analytics", "产品使用分析"),
    "/platform/audit": ("platform_audit", "audit", "全局审计"),
}


def _render_platform_page(template: str, active_id: str, breadcrumb: str, admin: PlatformAdmin) -> HTMLResponse:
    return HTMLResponse(
        render_template(
            template,
            sidenav=render_platform_sidenav(active_id),
            admin_bar=render_platform_admin_bar(admin.email),
            topbar=render_platform_topbar(breadcrumb),
        )
    )


def _platform_page_route(path: str, template: str, active_id: str, breadcrumb: str):
    @router.get(path, response_class=HTMLResponse, name=f"platform_page_{active_id}")
    def _page(
        admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
    ) -> HTMLResponse:
        if admin is None:
            return RedirectResponse("/platform/login", status_code=302)
        return _render_platform_page(template, active_id, breadcrumb, admin)

    return _page


for _path, (_template, _active_id, _breadcrumb) in _PLATFORM_PAGES.items():
    _platform_page_route(_path, _template, _active_id, _breadcrumb)


def get_operations_payment_provider() -> PaymentProvider:
    try:
        if alipay_is_enabled():
            return get_alipay_provider()
        return get_wechat_pay_provider()
    except (AlipayConfigurationError, WechatPayConfigurationError) as error:
        raise HTTPException(status_code=503, detail="payment_provider_unavailable") from error


def _provider_for_operations_order(
    db: Session,
    tenant_id: str,
    order_id: str,
    default_provider: PaymentProvider,
) -> PaymentProvider:
    provider_code = get_order(db, tenant_id, order_id).provider
    if provider_code == default_provider.code:
        return default_provider
    if provider_code == ALIPAY_PROVIDER:
        return get_alipay_provider()
    if provider_code == WECHAT_PAY_PROVIDER:
        return get_wechat_pay_provider()
    raise PaymentOrderConflictError("unsupported payment provider")


def _factory(db: Session):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None
        else value.astimezone(timezone.utc)
    )


def _not_found(error: platform_operations.PlatformOperationsNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail="tenant_not_found")


def _record_operation_audit(
    db: Session,
    *,
    tenant_id: str,
    platform_admin: PlatformAdmin,
    action: str,
    object_type: str,
    object_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Require durable audit evidence before committing an operator action."""
    safe_detail = {"platform_admin_id": platform_admin.id}
    if detail:
        safe_detail.update(detail)
    if not write_audit(
        db,
        tenant_id=tenant_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        # AuditLog.admin_user_id is tenant-scoped, so platform identity is
        # recorded only in the structured detail field above.
        admin_user_id=None,
        detail=safe_detail,
    ):
        db.rollback()
        raise HTTPException(status_code=500, detail="operation_audit_failed")


@router.get("/platform", response_class=HTMLResponse)
@router.get("/platform/operations", response_class=HTMLResponse, include_in_schema=False)
def operations_page(
    admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
) -> HTMLResponse:
    """Internal-only HTML shell for the operations dashboard. RND-414 added
    the 9-item side nav and moved the canonical URL to ``/platform``, but
    ``/platform/operations`` (RND-413's original route) stays a first-class
    alias to the same handler rather than a redirect — a redirect would
    turn the existing "unauthenticated /platform/operations goes straight
    to /platform/login" contract into a two-hop redirect through /platform,
    which is an unrelated behavior change this ticket has no reason to
    make. Unauthenticated browsers go to the platform login page (RND-413);
    API clients keep using HTTP Basic."""
    if admin is None:
        return RedirectResponse("/platform/login", status_code=302)
    return _render_platform_page("platform_dashboard", "dashboard", "运营看板", admin)


@router.get("/platform/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_detail_page(
    tenant_id: str,
    admin: Optional[PlatformAdmin] = Depends(require_platform_admin_optional),
) -> HTMLResponse:
    """RND-414 tenant-detail shell (5 tabs, rendered client-side). The
    side nav highlights "tenants" — list and detail share one nav item."""
    if admin is None:
        return RedirectResponse("/platform/login", status_code=302)
    return _render_platform_page("platform_tenant_detail", "tenants", "租户商业状态", admin)


@router.get(
    "/api/platform/operations/dashboard",
    response_model=PlatformOperationsDashboardOut,
)
def operations_dashboard(
    months: int = Query(12, ge=1, le=24),
    _admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Platform-wide summary with no archive content or storage references."""
    return platform_operations.get_dashboard(db, months=months)


@router.get(
    "/api/platform/operations/tenants",
    response_model=TenantOperationsListOut,
)
def operations_tenants(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    lifecycle_status: Optional[str] = Query(None),
    subscription_status: Optional[str] = Query(None),
    refund_status: Optional[str] = Query(None),
    exception_type: Optional[str] = Query(None),
    ends_from: Optional[datetime] = Query(None),
    ends_to: Optional[datetime] = Query(None),
    sort: str = Query("created_desc"),
    _admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Paginated tenant metadata and aggregate usage for operations staff."""
    try:
        return platform_operations.list_tenants(
            db,
            page=page,
            page_size=page_size,
            lifecycle_status=lifecycle_status,
            subscription_status=subscription_status,
            refund_status=refund_status,
            exception_type=exception_type,
            ends_from=ends_from,
            ends_to=ends_to,
            sort=sort,
        )
    except platform_operations.PlatformOperationsValidationError as error:
        raise HTTPException(status_code=422, detail="invalid_operations_filter") from error


@router.get(
    "/api/platform/operations/tenants/{tenant_id}",
    response_model=TenantOperationsDetailOut,
)
def operations_tenant_detail(
    tenant_id: str,
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Return one tenant's safe operational summary and audit the scoped view."""
    try:
        detail = platform_operations.get_tenant_detail(db, tenant_id)
    except platform_operations.PlatformOperationsNotFoundError as error:
        raise _not_found(error) from error
    _record_operation_audit(
        db,
        tenant_id=tenant_id,
        platform_admin=platform_admin,
        action=AuditAction.PLATFORM_TENANT_ACCESSED,
        object_type=AuditObjectType.TENANT,
        object_id=tenant_id,
        detail={"surface": "operations_detail"},
    )
    db.commit()
    return detail


@router.patch(
    "/api/platform/operations/tenants/{tenant_id}/service",
    response_model=TenantServiceStatusOut,
)
def update_operations_tenant_service(
    tenant_id: str,
    payload: TenantServiceStatusUpdateIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TenantServiceStatusOut:
    """Suspend or restore service without granting tenant-admin privileges."""
    try:
        tenant = platform_operations.set_service_status(
            db,
            tenant_id,
            payload.lifecycle_status,
            platform_admin_id=platform_admin.id,
            reason_code=payload.reason_code,
            confirmation=payload.confirmation,
            idempotency_key=idempotency_key,
        )
    except platform_operations.PlatformOperationsNotFoundError as error:
        raise _not_found(error) from error
    except platform_operations.PlatformOperationsConflictError as error:
        raise HTTPException(status_code=409, detail="operation_idempotency_conflict") from error
    except platform_operations.PlatformOperationsValidationError as error:
        raise HTTPException(status_code=422, detail="invalid_service_operation") from error
    db.commit()
    db.refresh(tenant)
    return TenantServiceStatusOut(
        tenant_id=tenant.id,
        lifecycle_status=tenant.lifecycle_status,
        is_active=tenant.is_active,
        updated_at=tenant.updated_at,
    )


@router.post(
    "/api/platform/operations/tenants/{tenant_id}/payments/{order_id}/query",
    response_model=PaymentOrderOut,
)
def query_operations_payment(
    tenant_id: str,
    order_id: str,
    payload: ControlledOperationIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_operations_payment_provider),
) -> PaymentOrderOut:
    """Explicitly reconcile one tenant-scoped payment/activation exception."""
    try:
        get_order(db, tenant_id, order_id)
        replay = platform_operations.authorize_platform_operation(
            db,
            tenant_id=tenant_id,
            platform_admin_id=platform_admin.id,
            action="payment.query",
            target_id=order_id,
            reason_code=payload.reason_code,
            confirmation=payload.confirmation,
            idempotency_key=idempotency_key,
        )
        db.commit()
        summary = (
            get_order(db, tenant_id, order_id)
            if replay
            else query_and_reconcile_order(
                _factory(db),
                _provider_for_operations_order(db, tenant_id, order_id, provider),
                tenant_id,
                order_id,
                now=datetime.now(timezone.utc),
                force_channel_query=True,
            )
        )
        if not replay:
            db.expire_all()
            finalize_manual_payment_query(db, order_id)
        return PaymentOrderOut(**asdict(summary))
    except platform_operations.PlatformOperationsNotFoundError as error:
        raise _not_found(error) from error
    except platform_operations.PlatformOperationsConflictError as error:
        raise HTTPException(status_code=409, detail="operation_idempotency_conflict") from error
    except platform_operations.PlatformOperationsValidationError as error:
        raise HTTPException(status_code=422, detail="invalid_payment_operation") from error
    except PaymentOrderNotFoundError as error:
        raise HTTPException(status_code=404, detail="payment_order_not_found") from error
    except PaymentOrderConflictError as error:
        raise HTTPException(status_code=409, detail="payment_order_conflict") from error
    except PaymentActivationPendingError as error:
        raise HTTPException(status_code=503, detail="payment_activation_pending") from error
    except (AlipayProtocolError, WechatPayProtocolError) as error:
        raise HTTPException(status_code=502, detail="payment_provider_failed") from error


@router.post(
    "/api/platform/operations/payment-findings/{finding_id}/resolve",
    response_model=PaymentRecoveryFindingResolutionOut,
)
def resolve_operations_payment_finding(
    finding_id: str,
    payload: PaymentRecoveryFindingResolutionIn,
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PaymentRecoveryFindingResolutionOut:
    """Resolve a reviewed global callback finding without touching payment state."""
    try:
        finding = resolve_historical_callback_signature_finding(
            db,
            finding_id,
            classification=payload.classification,
            failure_class=payload.failure_class,
            reason_code=payload.reason_code,
            platform_admin_id=platform_admin.id,
        )
    except PaymentRecoveryFindingNotFoundError as error:
        raise HTTPException(status_code=404, detail="payment_finding_not_found") from error
    except PaymentRecoveryFindingResolutionError as error:
        raise HTTPException(status_code=422, detail="invalid_payment_finding_resolution") from error
    db.commit()
    return PaymentRecoveryFindingResolutionOut(
        finding_id=finding.id,
        kind=finding.kind,
        status="resolved",
        occurrence_count=finding.occurrence_count,
        created_at=_as_utc(finding.created_at),
        first_detected_at=_as_utc(finding.first_detected_at),
        last_detected_at=_as_utc(finding.last_detected_at),
        resolved_at=_as_utc(finding.resolved_at),
        classification=finding.resolution_classification,
        failure_class=finding.resolution_failure_class,
        reason_code=finding.resolution_reason_code,
        resolved_by_platform_admin_id=finding.resolved_by_platform_admin_id,
    )


@router.put(
    "/api/platform/operations/tenants/{tenant_id}/subscription",
    response_model=ManualSubscriptionOut,
)
def update_operations_tenant_subscription(
    tenant_id: str,
    payload: ManualSubscriptionUpdateIn,
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> ManualSubscriptionOut:
    """Assign a server-authoritative plan/status/term and append history."""
    try:
        subscription, plan = platform_operations.update_subscription(
            db,
            tenant_id=tenant_id,
            plan_code=payload.plan_code,
            status=payload.status,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
        )
    except platform_operations.PlatformOperationsNotFoundError as error:
        raise _not_found(error) from error
    except platform_operations.PlatformOperationsValidationError as error:
        raise HTTPException(status_code=422, detail="invalid_subscription_update") from error
    _record_operation_audit(
        db,
        tenant_id=tenant_id,
        platform_admin=platform_admin,
        action=AuditAction.PLATFORM_SUBSCRIPTION_UPDATED,
        object_type=AuditObjectType.SUBSCRIPTION,
        object_id=subscription.id,
        detail={
            "plan_code": plan.code,
            "status": subscription.status,
            "revision": subscription.revision,
            "starts_at": subscription.starts_at.isoformat(),
            "ends_at": subscription.ends_at.isoformat(),
        },
    )
    db.commit()
    return ManualSubscriptionOut(
        tenant_id=tenant_id,
        plan_code=plan.code,
        plan_name=plan.display_name,
        status=subscription.status,
        starts_at=subscription.starts_at,
        ends_at=subscription.ends_at,
        renewal_count=subscription.renewal_count,
        revision=subscription.revision,
    )


@router.post(
    "/api/platform/operations/tenants/{tenant_id}/financial-transactions",
    response_model=ManualFinancialTransactionOut,
    status_code=status.HTTP_201_CREATED,
)
def record_operations_financial_transaction(
    tenant_id: str,
    payload: ManualFinancialTransactionIn,
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> ManualFinancialTransactionOut:
    """Record a manual receipt/refund; it never changes subscription authority."""
    try:
        transaction = platform_operations.record_manual_financial_transaction(
            db,
            tenant_id=tenant_id,
            platform_admin_id=platform_admin.id,
            kind=payload.kind,
            amount_cents=payload.amount_cents,
            occurred_at=payload.occurred_at,
            reference=payload.reference,
            note=payload.note,
            at=datetime.now(timezone.utc),
        )
    except platform_operations.PlatformOperationsNotFoundError as error:
        raise _not_found(error) from error
    except platform_operations.PlatformOperationsValidationError as error:
        raise HTTPException(status_code=422, detail="invalid_financial_transaction") from error
    _record_operation_audit(
        db,
        tenant_id=tenant_id,
        platform_admin=platform_admin,
        action=AuditAction.PLATFORM_MANUAL_FINANCIAL_TRANSACTION_RECORDED,
        object_type=AuditObjectType.FINANCIAL_TRANSACTION,
        object_id=transaction.id,
        # Never put operator-entered reference/note in audit detail.
        detail={
            "kind": transaction.kind,
            "amount_cents": transaction.amount_cents,
            "currency": transaction.currency,
        },
    )
    db.commit()
    return ManualFinancialTransactionOut(
        transaction_id=transaction.id,
        tenant_id=transaction.tenant_id,
        kind=transaction.kind,
        amount_cents=transaction.amount_cents,
        currency=transaction.currency,
        occurred_at=transaction.occurred_at,
        reference=transaction.reference,
    )
