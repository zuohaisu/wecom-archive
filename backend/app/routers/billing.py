"""Owner billing page, payment-order APIs, and WeChat Pay notification route."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import qrcode
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import (
    BILLING_MANAGER_ROLES,
    BillingAccessContext,
    get_billing_manager,
    get_billing_viewer,
)
from app.db.models import BillingPlan, PaymentOrder, PlanEntitlement
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.schemas.billing import (
    BillingPlanOut,
    CreatePaymentOrderIn,
    PaymentOrderOut,
    StorageCapacityOut,
    StorageTrendOut,
    StorageTrendPointOut,
    SubscriptionOverviewOut,
)
from app.schemas.refunds import RefundOut
from app.services.entitlements import ANNUAL_PLAN_CODE, UNLIMITED_SEATS
from app.services.alipay import (
    ALIPAY_PROVIDER,
    AlipayConfigurationError,
    AlipayProtocolError,
    alipay_is_enabled,
    get_alipay_provider,
)
from app.services.payment_orders import (
    CreateOrderCommand,
    InvalidPaymentOrderError,
    PaymentActivationPendingError,
    PaymentOrderConflictError,
    PaymentOrderNotFoundError,
    PaymentOrderSummary,
    apply_trusted_payment,
    close_order,
    create_payment_order,
    get_checkout_url,
    get_latest_order,
    get_redirect_checkout_url,
    get_order,
    query_and_reconcile_order,
)
from app.services.payment_provider import PaymentProvider
from app.services.payment_recovery import (
    FINDING_CALLBACK_DECRYPT_FAILURE,
    FINDING_CALLBACK_SIGNATURE_FAILURE,
    record_callback_failure,
)
from app.services.refunds import get_latest_refund_for_tenant
from app.services.storage_capacity import measure_storage_capacity
from app.services.storage_trend import storage_trend
from app.services.subscription_overview import get_subscription_overview
from app.services.tenant_activation import spawn_activation_worker
from app.services.wechat_pay import (
    WECHAT_PAY_PROVIDER,
    WechatPayConfigurationError,
    WechatPayDecryptError,
    WechatPayProtocolError,
    WechatPaySignatureVerificationError,
    get_wechat_pay_provider,
    wechat_pay_is_enabled,
)
from app.web import render_template
from app.web.sidenav import render_provisioning_sidenav, render_sidenav

router = APIRouter()

logger = logging.getLogger(__name__)


def _log_wechat_pay_callback_verification_rejected(
    error: WechatPaySignatureVerificationError, raw_body: bytes
) -> None:
    """Emit sufficient verification evidence without retaining untrusted inputs."""
    metadata = error.metadata
    logger.warning(
        "event=wechat_pay_callback_verification_rejected "
        "failure_class=%s route=/api/payments/wechat/notify "
        "header_serial_present=%s header_timestamp_present=%s "
        "header_nonce_present=%s header_signature_present=%s "
        "serial_match=%s sign_test=%s body_length=%d body_sha256=%s",
        error.failure_class.value,
        metadata.header_serial_present,
        metadata.header_timestamp_present,
        metadata.header_nonce_present,
        metadata.header_signature_present,
        metadata.serial_match,
        metadata.sign_test,
        len(raw_body),
        hashlib.sha256(raw_body).hexdigest(),
    )


def get_payment_provider() -> PaymentProvider:
    """Select only a provider currently accepting new payment creation."""
    try:
        if alipay_is_enabled():
            return get_alipay_provider()
        if wechat_pay_is_enabled():
            return get_wechat_pay_provider()
    except (AlipayConfigurationError, WechatPayConfigurationError) as error:
        raise HTTPException(status_code=503, detail="payment_unavailable") from error
    raise HTTPException(status_code=503, detail="payment_unavailable")


def get_wechat_payment_provider() -> PaymentProvider:
    try:
        return get_wechat_pay_provider()
    except WechatPayConfigurationError as error:
        raise HTTPException(status_code=503, detail="payment_unavailable") from error


def get_alipay_payment_provider() -> PaymentProvider:
    try:
        return get_alipay_provider()
    except AlipayConfigurationError as error:
        raise HTTPException(status_code=503, detail="payment_unavailable") from error


def _provider_for_order(
    db: Session, tenant_id: str, order_id: str
) -> PaymentProvider:
    provider_code = get_order(db, tenant_id, order_id).provider
    if provider_code == ALIPAY_PROVIDER:
        return get_alipay_payment_provider()
    if provider_code == WECHAT_PAY_PROVIDER:
        return get_wechat_payment_provider()
    raise PaymentOrderConflictError("unsupported payment provider")


def _factory(db: Session):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def _out(
    summary: PaymentOrderSummary,
    context: Optional[BillingAccessContext] = None,
) -> PaymentOrderOut:
    out = PaymentOrderOut(**asdict(summary))
    # RND-407: read-only roles may inspect an order's status but must not be
    # able to pay it; hide the checkout QR flag so the client never renders
    # a payment surface for them.
    if context is not None and context.role not in BILLING_MANAGER_ROLES:
        out.qr_available = False
    return out


def _raise_order_error(error: Exception) -> None:
    if isinstance(error, PaymentOrderNotFoundError):
        raise HTTPException(status_code=404, detail="payment_order_not_found") from error
    if isinstance(error, InvalidPaymentOrderError):
        raise HTTPException(status_code=422, detail="invalid_payment_order") from error
    if isinstance(error, PaymentOrderConflictError):
        raise HTTPException(status_code=409, detail="payment_order_conflict") from error
    if isinstance(error, (AlipayProtocolError, WechatPayProtocolError)):
        raise HTTPException(status_code=502, detail="payment_provider_failed") from error
    raise error


def _overview_out(db: Session, tenant_id: str) -> SubscriptionOverviewOut:
    overview = get_subscription_overview(db, tenant_id)
    return SubscriptionOverviewOut(
        plan_code=overview.plan_code,
        plan_name=overview.plan_name,
        stored_status=overview.stored_status,
        effective_status=overview.effective_status,
        display_state=overview.display_state,
        unavailable_reason=overview.unavailable_reason,
        is_entitled=overview.is_entitled,
        starts_at=overview.starts_at,
        ends_at=overview.ends_at,
        grace_ends_at=overview.grace_ends_at,
        entitlements=list(overview.entitlements),
        renewal_count=overview.renewal_count,
        measured_at=overview.measured_at,
        tenant_lifecycle_status=overview.tenant_lifecycle_status,
    )


@router.get("/admin/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    context: BillingAccessContext = Depends(get_billing_viewer),
) -> HTMLResponse:
    paths = {route.path for route in request.app.routes if hasattr(route, "path")}
    sidenav = (
        render_provisioning_sidenav("billing")
        if context.session_scope == "provisioning"
        else render_sidenav("billing", paths)
    )
    return HTMLResponse(
        render_template(
            "billing",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=sidenav,
            billing_mode=context.session_scope,
            billing_can_order=(
                "true" if context.role in BILLING_MANAGER_ROLES else "false"
            ),
        )
    )


@router.get("/api/billing/plan", response_model=BillingPlanOut)
def billing_plan(
    _context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> BillingPlanOut:
    plan = db.scalar(
        select(BillingPlan).where(
            BillingPlan.code == ANNUAL_PLAN_CODE,
            BillingPlan.is_active.is_(True),
        )
    )
    if plan is None:
        raise HTTPException(status_code=503, detail="billing_plan_unavailable")
    unlimited = db.scalar(
        select(PlanEntitlement.id).where(
            PlanEntitlement.plan_id == plan.id,
            PlanEntitlement.capability == UNLIMITED_SEATS,
            PlanEntitlement.is_enabled.is_(True),
        )
    )
    try:
        payment_enabled = alipay_is_enabled() or wechat_pay_is_enabled()
    except (AlipayConfigurationError, WechatPayConfigurationError) as error:
        raise HTTPException(status_code=503, detail="payment_unavailable") from error
    return BillingPlanOut(
        code=plan.code,
        display_name=plan.display_name,
        amount_cents=plan.amount_cents,
        currency=plan.currency,
        billing_period_months=plan.billing_period_months,
        storage_quota_bytes=plan.storage_quota_bytes,
        unlimited_seats=unlimited is not None,
        tencent_archive_fee_separate=True,
        payment_enabled=payment_enabled,
    )


@router.get("/api/billing/capacity", response_model=StorageCapacityOut)
def billing_capacity(
    context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> StorageCapacityOut:
    # Serialize the first daily-rollup insert with media writers and other
    # owner reads; the endpoint commits immediately after this measurement.
    snapshot = measure_storage_capacity(db, context.tenant_id, lock_tenant=True)
    db.commit()
    return StorageCapacityOut(
        plan_code=snapshot.plan_code,
        subscription_status=snapshot.subscription_status,
        quota_bytes=snapshot.quota_bytes,
        used_bytes=snapshot.used_bytes,
        remaining_bytes=snapshot.remaining_bytes,
        utilization_basis_points=snapshot.utilization_basis_points,
        state=snapshot.state,
        usage_status=snapshot.usage_status,
        can_accept_new_media=snapshot.can_accept_new_media,
        measured_at=snapshot.measured_at,
    )


@router.get("/api/billing/capacity/trend", response_model=StorageTrendOut)
def billing_capacity_trend(
    range_days: int = Query(30, ge=7, le=90),
    context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> StorageTrendOut:
    """Tenant storage trend + depletion estimate (RND-163).

    Re-measures today's authoritative capacity (upserting today's rollup)
    then reads the same rollup history, so the trend shares one measurement
   口径 with the live capacity card. The forecast is an estimate only.
    """
    snapshot = measure_storage_capacity(db, context.tenant_id, lock_tenant=True)
    db.commit()
    trend = storage_trend(
        db,
        context.tenant_id,
        range_days=range_days,
        quota_bytes=snapshot.quota_bytes,
        used_bytes=snapshot.used_bytes,
        measured_at=snapshot.measured_at,
    )
    return StorageTrendOut(
        range_days=trend.range_days,
        series=[
            StorageTrendPointOut(date=point.date, bytes=point.bytes)
            for point in trend.series
        ],
        measured_points=trend.measured_points,
        avg_daily_growth_bytes=trend.avg_daily_growth_bytes,
        days_until_full=trend.days_until_full,
        estimate_available=trend.estimate_available,
        quota_bytes=trend.quota_bytes,
        used_bytes=trend.used_bytes,
        measured_at=trend.measured_at,
    )


@router.get("/api/billing/subscription", response_model=SubscriptionOverviewOut)
def billing_subscription(
    context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> SubscriptionOverviewOut:
    return _overview_out(db, context.tenant_id)


@router.get("/api/billing/refunds/latest", response_model=Optional[RefundOut])
def latest_refund(
    context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> Optional[RefundOut]:
    """Read-only refund status for the Owner billing page (RND-404).

    Owners never initiate a refund here — only a platform admin can, via
    the separate controlled `app.routers.refunds` surface.
    """
    summary = get_latest_refund_for_tenant(db, context.tenant_id)
    if summary is None:
        return None
    values = asdict(summary)
    values.pop("term_grant_id", None)
    return RefundOut(**values)


@router.get("/api/billing/orders/latest", response_model=Optional[PaymentOrderOut])
def latest_payment_order(
    context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> Optional[PaymentOrderOut]:
    summary = get_latest_order(db, context.tenant_id)
    return _out(summary, context) if summary is not None else None


@router.get("/api/billing/orders/{order_id}", response_model=PaymentOrderOut)
def payment_order(
    order_id: str,
    context: BillingAccessContext = Depends(get_billing_viewer),
    db: Session = Depends(get_db),
) -> PaymentOrderOut:
    try:
        return _out(get_order(db, context.tenant_id, order_id), context)
    except Exception as error:
        _raise_order_error(error)
        raise


@router.post("/api/billing/orders", response_model=PaymentOrderOut, status_code=201)
def create_order(
    payload: CreatePaymentOrderIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    context: BillingAccessContext = Depends(get_billing_manager),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> PaymentOrderOut:
    try:
        summary = create_payment_order(
            _factory(db),
            provider,
            CreateOrderCommand(
                tenant_id=context.tenant_id,
                plan_code=payload.plan_code,
                idempotency_key=idempotency_key,
                now=datetime.now(timezone.utc),
            ),
        )
        return _out(summary, context)
    except Exception as error:
        _raise_order_error(error)
        raise


@router.get("/api/billing/orders/{order_id}/qr")
def payment_order_qr(
    order_id: str,
    context: BillingAccessContext = Depends(get_billing_manager),
    db: Session = Depends(get_db),
) -> Response:
    try:
        checkout_url = get_checkout_url(db, context.tenant_id, order_id)
    except Exception as error:
        _raise_order_error(error)
        raise
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(checkout_url, optimize=0)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return Response(
        content=output.getvalue(),
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/billing/orders/{order_id}/checkout", include_in_schema=False)
def payment_order_checkout(
    order_id: str,
    context: BillingAccessContext = Depends(get_billing_manager),
    db: Session = Depends(get_db),
) -> Response:
    try:
        checkout_url = get_redirect_checkout_url(db, context.tenant_id, order_id)
    except Exception as error:
        _raise_order_error(error)
        raise
    return RedirectResponse(
        checkout_url,
        status_code=303,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/api/billing/orders/{order_id}/refresh", response_model=PaymentOrderOut)
def refresh_payment_order(
    order_id: str,
    context: BillingAccessContext = Depends(get_billing_manager),
    db: Session = Depends(get_db),
) -> PaymentOrderOut:
    try:
        provider = _provider_for_order(db, context.tenant_id, order_id)
        return _out(
            query_and_reconcile_order(
                _factory(db),
                provider,
                context.tenant_id,
                order_id,
                now=datetime.now(timezone.utc),
            ),
            context,
        )
    except PaymentActivationPendingError as error:
        raise HTTPException(status_code=503, detail="payment_activation_pending") from error
    except Exception as error:
        _raise_order_error(error)
        raise


@router.post("/api/billing/orders/{order_id}/close", response_model=PaymentOrderOut)
def close_payment_order(
    order_id: str,
    context: BillingAccessContext = Depends(get_billing_manager),
    db: Session = Depends(get_db),
) -> PaymentOrderOut:
    try:
        provider = _provider_for_order(db, context.tenant_id, order_id)
        return _out(
            close_order(
                _factory(db),
                provider,
                context.tenant_id,
                order_id,
                now=datetime.now(timezone.utc),
            ),
            context,
        )
    except Exception as error:
        _raise_order_error(error)
        raise


@router.post("/api/payments/wechat/notify", include_in_schema=False)
async def wechat_payment_notification(
    request: Request,
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_wechat_payment_provider),
) -> Response:
    raw_body = await request.body()
    try:
        event = provider.verify_and_parse_notification(request.headers, raw_body)
        summary = apply_trusted_payment(_factory(db), provider, event)
    except PaymentActivationPendingError:
        return JSONResponse(
            status_code=500,
            content={"code": "FAIL", "message": "temporary processing failure"},
        )
    except WechatPaySignatureVerificationError as error:
        _log_wechat_pay_callback_verification_rejected(error, raw_body)
        try:
            record_callback_failure(
                _factory(db), kind=FINDING_CALLBACK_SIGNATURE_FAILURE
            )
        except Exception:  # noqa: BLE001 - callback must still fail closed
            logger.exception("payment callback signature finding write failed")
        return JSONResponse(
            status_code=400,
            content={"code": "FAIL", "message": "invalid notification"},
        )
    except WechatPayDecryptError:
        try:
            record_callback_failure(_factory(db), kind=FINDING_CALLBACK_DECRYPT_FAILURE)
        except Exception:  # noqa: BLE001 - callback must still fail closed
            logger.exception("payment callback decrypt finding write failed")
        return JSONResponse(
            status_code=400,
            content={"code": "FAIL", "message": "invalid notification"},
        )
    except (PaymentOrderNotFoundError, PaymentOrderConflictError, WechatPayProtocolError):
        return JSONResponse(
            status_code=400,
            content={"code": "FAIL", "message": "invalid notification"},
        )
    # RND-388 auto-activation trigger: the subscription is committed, so a
    # provisioning tenant that already passed the other gates can now
    # activate without a human. Best-effort and detached — the 204 must not
    # wait for the gate evaluation (which probes WeCom connectivity).
    try:
        order = db.scalar(
            select(PaymentOrder).where(PaymentOrder.id == summary.order_id)
        )
        if order is not None:
            spawn_activation_worker(
                db.get_bind(), order.tenant_id, actor="payment_notify"
            )
    except Exception:  # noqa: BLE001 -- activation is best-effort by contract
        logger.exception("activation trigger after payment notify failed")
    return Response(status_code=204)


@router.post("/api/payments/alipay/notify", include_in_schema=False)
async def alipay_payment_notification(
    request: Request,
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_alipay_payment_provider),
) -> Response:
    raw_body = await request.body()
    try:
        event = provider.verify_and_parse_notification(request.headers, raw_body)
        summary = apply_trusted_payment(_factory(db), provider, event)
    except PaymentActivationPendingError:
        return PlainTextResponse("failure", status_code=500)
    except (PaymentOrderNotFoundError, PaymentOrderConflictError, AlipayProtocolError):
        return PlainTextResponse("failure", status_code=400)
    try:
        order = db.scalar(
            select(PaymentOrder).where(PaymentOrder.id == summary.order_id)
        )
        if order is not None:
            spawn_activation_worker(
                db.get_bind(), order.tenant_id, actor="payment_notify"
            )
    except Exception:  # noqa: BLE001 -- activation is best-effort by contract
        logger.exception("activation trigger after payment notify failed")
    return PlainTextResponse("success")
