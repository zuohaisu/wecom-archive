"""Owner billing page, payment-order APIs, and WeChat Pay notification route."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import qrcode
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import BillingOwnerContext, get_billing_owner
from app.db.models import BillingPlan, PlanEntitlement
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.schemas.billing import (
    BillingPlanOut,
    CreatePaymentOrderIn,
    PaymentOrderOut,
    StorageCapacityOut,
    SubscriptionOverviewOut,
)
from app.services.entitlements import ANNUAL_PLAN_CODE, UNLIMITED_SEATS
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
    get_order,
    query_and_reconcile_order,
)
from app.services.payment_provider import PaymentProvider
from app.services.storage_capacity import measure_storage_capacity
from app.services.subscription_overview import get_subscription_overview
from app.services.wechat_pay import (
    WechatPayConfigurationError,
    WechatPayProtocolError,
    get_wechat_pay_provider,
    wechat_pay_is_enabled,
)
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()


def get_payment_provider() -> PaymentProvider:
    try:
        return get_wechat_pay_provider()
    except WechatPayConfigurationError as error:
        raise HTTPException(status_code=503, detail="payment_unavailable") from error


def _factory(db: Session):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def _out(summary: PaymentOrderSummary) -> PaymentOrderOut:
    return PaymentOrderOut(**asdict(summary))


def _raise_order_error(error: Exception) -> None:
    if isinstance(error, PaymentOrderNotFoundError):
        raise HTTPException(status_code=404, detail="payment_order_not_found") from error
    if isinstance(error, InvalidPaymentOrderError):
        raise HTTPException(status_code=422, detail="invalid_payment_order") from error
    if isinstance(error, PaymentOrderConflictError):
        raise HTTPException(status_code=409, detail="payment_order_conflict") from error
    if isinstance(error, WechatPayProtocolError):
        raise HTTPException(status_code=502, detail="payment_provider_failed") from error
    raise error


def _provisioning_sidenav() -> str:
    return """<nav class="side-nav">
  <div class="side-nav-brand"><img class="side-nav-logo" src="/web/static/brand/icon-tile-24.svg" alt="康冠时代" width="24" height="24"><div class="side-nav-title">组织自助开通</div></div>
  <div class="side-nav-scroll">
    <div class="side-nav-group">开通步骤</div>
    <a class="side-nav-item" href="/admin/provisioning">组织状态</a>
    <a class="side-nav-item active" href="/admin/billing" aria-current="page">购买年度套餐</a>
    <a class="side-nav-item" href="/admin/provisioning/settings">配置准备</a>
  </div>
  <div class="side-nav-user"><button class="btn-logout" onclick="doLogout()">退出登录</button></div>
</nav>"""


@router.get("/admin/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    context: BillingOwnerContext = Depends(get_billing_owner),
) -> HTMLResponse:
    paths = {route.path for route in request.app.routes if hasattr(route, "path")}
    sidenav = (
        _provisioning_sidenav()
        if context.session_scope == "provisioning"
        else render_sidenav("billing", paths)
    )
    return HTMLResponse(
        render_template(
            "billing",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=sidenav,
        )
    )


@router.get("/api/billing/plan", response_model=BillingPlanOut)
def billing_plan(
    _context: BillingOwnerContext = Depends(get_billing_owner),
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
        payment_enabled = wechat_pay_is_enabled()
    except WechatPayConfigurationError as error:
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
    context: BillingOwnerContext = Depends(get_billing_owner),
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


@router.get("/api/billing/subscription", response_model=SubscriptionOverviewOut)
def billing_subscription(
    context: BillingOwnerContext = Depends(get_billing_owner),
    db: Session = Depends(get_db),
) -> SubscriptionOverviewOut:
    overview = get_subscription_overview(db, context.tenant_id)
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
        cancel_at_period_end=overview.cancel_at_period_end,
        entitlements=list(overview.entitlements),
        renewal_count=overview.renewal_count,
        measured_at=overview.measured_at,
    )


@router.get("/api/billing/orders/latest", response_model=Optional[PaymentOrderOut])
def latest_payment_order(
    context: BillingOwnerContext = Depends(get_billing_owner),
    db: Session = Depends(get_db),
) -> Optional[PaymentOrderOut]:
    summary = get_latest_order(db, context.tenant_id)
    return _out(summary) if summary is not None else None


@router.get("/api/billing/orders/{order_id}", response_model=PaymentOrderOut)
def payment_order(
    order_id: str,
    context: BillingOwnerContext = Depends(get_billing_owner),
    db: Session = Depends(get_db),
) -> PaymentOrderOut:
    try:
        return _out(get_order(db, context.tenant_id, order_id))
    except Exception as error:
        _raise_order_error(error)
        raise


@router.post("/api/billing/orders", response_model=PaymentOrderOut, status_code=201)
def create_order(
    payload: CreatePaymentOrderIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    context: BillingOwnerContext = Depends(get_billing_owner),
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
        return _out(summary)
    except Exception as error:
        _raise_order_error(error)
        raise


@router.get("/api/billing/orders/{order_id}/qr")
def payment_order_qr(
    order_id: str,
    context: BillingOwnerContext = Depends(get_billing_owner),
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


@router.post("/api/billing/orders/{order_id}/refresh", response_model=PaymentOrderOut)
def refresh_payment_order(
    order_id: str,
    context: BillingOwnerContext = Depends(get_billing_owner),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> PaymentOrderOut:
    try:
        return _out(
            query_and_reconcile_order(
                _factory(db),
                provider,
                context.tenant_id,
                order_id,
                now=datetime.now(timezone.utc),
            )
        )
    except PaymentActivationPendingError as error:
        raise HTTPException(status_code=503, detail="payment_activation_pending") from error
    except Exception as error:
        _raise_order_error(error)
        raise


@router.post("/api/billing/orders/{order_id}/close", response_model=PaymentOrderOut)
def close_payment_order(
    order_id: str,
    context: BillingOwnerContext = Depends(get_billing_owner),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> PaymentOrderOut:
    try:
        return _out(
            close_order(
                _factory(db),
                provider,
                context.tenant_id,
                order_id,
                now=datetime.now(timezone.utc),
            )
        )
    except Exception as error:
        _raise_order_error(error)
        raise


@router.post("/api/payments/wechat/notify", include_in_schema=False)
async def wechat_payment_notification(
    request: Request,
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
) -> Response:
    raw_body = await request.body()
    try:
        event = provider.verify_and_parse_notification(request.headers, raw_body)
        apply_trusted_payment(_factory(db), provider, event)
        return Response(status_code=204)
    except PaymentActivationPendingError:
        return JSONResponse(
            status_code=500,
            content={"code": "FAIL", "message": "temporary processing failure"},
        )
    except (PaymentOrderNotFoundError, PaymentOrderConflictError, WechatPayProtocolError):
        return JSONResponse(
            status_code=400,
            content={"code": "FAIL", "message": "invalid notification"},
        )
