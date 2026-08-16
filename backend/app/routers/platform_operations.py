"""Platform-only SaaS operations dashboard and controlled manual operations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, sessionmaker

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import require_platform_admin
from app.db.models import PlatformAdmin
from app.db.session import get_db
from app.schemas.platform_operations import (
    ControlledOperationIn,
    ManualFinancialTransactionIn,
    ManualFinancialTransactionOut,
    ManualSubscriptionOut,
    ManualSubscriptionUpdateIn,
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
from app.services.payment_provider import PaymentProvider
from app.services.wechat_pay import (
    WechatPayConfigurationError,
    WechatPayProtocolError,
    get_wechat_pay_provider,
)
from app.web import render_template

router = APIRouter(tags=["platform-operations"])


def get_operations_payment_provider() -> PaymentProvider:
    try:
        return get_wechat_pay_provider()
    except WechatPayConfigurationError as error:
        raise HTTPException(status_code=503, detail="payment_provider_unavailable") from error


def _factory(db: Session):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


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


@router.get("/platform/operations", response_class=HTMLResponse)
def operations_page(_admin: PlatformAdmin = Depends(require_platform_admin)) -> HTMLResponse:
    """Internal-only HTML shell. Browser HTTP Basic auth remains platform-only."""
    return HTMLResponse(render_template("platform_operations"))


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
                provider,
                tenant_id,
                order_id,
                now=datetime.now(timezone.utc),
            )
        )
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
    except WechatPayProtocolError as error:
        raise HTTPException(status_code=502, detail="payment_provider_failed") from error


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
