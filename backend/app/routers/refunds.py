"""Signed WeChat refund callback and platform-admin-only control endpoints."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.auth import require_platform_admin
from app.db.models import PlatformAdmin
from app.db.session import get_db
from app.schemas.refunds import QueryRefundIn, RefundOut, SubmitRefundIn
from app.services import platform_operations
from app.services.payment_provider import PaymentProvider
from app.services.payment_orders import PaymentOrderNotFoundError, get_order
from app.services.refunds import (
    RefundAuthorizationError,
    RefundConflictError,
    RefundError,
    RefundNotFoundError,
    get_refund_summary,
)
from app.services.wechat_pay import (
    WechatPayConfigurationError,
    WechatPayProtocolError,
    get_wechat_pay_provider,
)
from app.services.wechat_refunds import (
    SubmitWechatRefundCommand,
    apply_wechat_refund_event,
    query_and_reconcile_wechat_refund,
    submit_wechat_refund,
)

router = APIRouter(tags=["refunds"])


def get_refund_provider() -> PaymentProvider:
    try:
        return get_wechat_pay_provider()
    except WechatPayConfigurationError as error:
        raise HTTPException(status_code=503, detail="refund_provider_unavailable") from error


def _factory(db: Session):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def _out(summary) -> RefundOut:
    values = asdict(summary)
    values.pop("term_grant_id", None)
    return RefundOut(**values)


def _raise_control_error(error: Exception) -> None:
    if isinstance(error, platform_operations.PlatformOperationsNotFoundError):
        raise HTTPException(status_code=404, detail="tenant_not_found") from error
    if isinstance(error, platform_operations.PlatformOperationsConflictError):
        raise HTTPException(
            status_code=409, detail="operation_idempotency_conflict"
        ) from error
    if isinstance(error, platform_operations.PlatformOperationsValidationError):
        raise HTTPException(status_code=422, detail="invalid_control_operation") from error
    if isinstance(error, RefundNotFoundError):
        raise HTTPException(status_code=404, detail="refund_not_found") from error
    if isinstance(error, PaymentOrderNotFoundError):
        raise HTTPException(status_code=404, detail="payment_order_not_found") from error
    if isinstance(error, RefundAuthorizationError):
        raise HTTPException(status_code=403, detail="refund_not_authorized") from error
    if isinstance(error, RefundConflictError):
        raise HTTPException(status_code=409, detail="refund_conflict") from error
    if isinstance(error, RefundError):
        raise HTTPException(status_code=422, detail="invalid_refund_request") from error
    if isinstance(error, WechatPayProtocolError):
        raise HTTPException(status_code=502, detail="refund_provider_failed") from error
    raise error


@router.post(
    "/api/platform/operations/tenants/{tenant_id}/refunds",
    response_model=RefundOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_platform_refund(
    tenant_id: str,
    payload: SubmitRefundIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_refund_provider),
) -> RefundOut:
    try:
        get_order(db, tenant_id, payload.payment_order_id)
        platform_operations.authorize_platform_operation(
            db,
            tenant_id=tenant_id,
            platform_admin_id=platform_admin.id,
            action="refund.submit",
            target_id=payload.payment_order_id,
            reason_code=payload.reason_code,
            confirmation=payload.confirmation,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return _out(
            submit_wechat_refund(
                _factory(db),
                provider,
                SubmitWechatRefundCommand(
                    tenant_id=tenant_id,
                    payment_order_id=payload.payment_order_id,
                    idempotency_key=idempotency_key,
                    reason_code=payload.reason_code,
                    approved_by_platform_admin_id=platform_admin.id,
                    requested_at=datetime.now(timezone.utc),
                ),
            )
        )
    except Exception as error:
        _raise_control_error(error)
        raise


@router.post(
    "/api/platform/operations/tenants/{tenant_id}/refunds/{refund_id}/query",
    response_model=RefundOut,
)
def query_platform_refund(
    tenant_id: str,
    refund_id: str,
    payload: QueryRefundIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    platform_admin: PlatformAdmin = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_refund_provider),
) -> RefundOut:
    try:
        get_refund_summary(db, tenant_id, refund_id)
        replay = platform_operations.authorize_platform_operation(
            db,
            tenant_id=tenant_id,
            platform_admin_id=platform_admin.id,
            action="refund.query",
            target_id=refund_id,
            reason_code=payload.reason_code,
            confirmation=payload.confirmation,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return _out(
            get_refund_summary(db, tenant_id, refund_id)
            if replay
            else query_and_reconcile_wechat_refund(
                _factory(db), provider, tenant_id, refund_id
            )
        )
    except Exception as error:
        _raise_control_error(error)
        raise


@router.post("/api/refunds/wechat/notify", include_in_schema=False)
async def wechat_refund_notification(
    request: Request,
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_refund_provider),
) -> Response:
    raw_body = await request.body()
    try:
        event = provider.verify_and_parse_refund_notification(
            request.headers, raw_body
        )
        apply_wechat_refund_event(_factory(db), provider, event)
        return Response(status_code=204)
    except (RefundNotFoundError, RefundConflictError, WechatPayProtocolError):
        return JSONResponse(
            status_code=400,
            content={"code": "FAIL", "message": "invalid notification"},
        )
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"code": "FAIL", "message": "temporary processing failure"},
        )
