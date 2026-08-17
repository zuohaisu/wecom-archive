"""Independent WeCom service-provider instruction callback (RND-350)."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_platform_admin
from app.crypto import (
    FieldEncryptionConfigurationError,
    validate_field_encryption_configuration,
)
from app.db.session import get_db
from app.services.wecom_callback_crypto import (
    CallbackConfigurationError,
    CallbackInputError,
    decode_aes_key,
    decrypt_envelope,
    extract_encrypt,
    parse_plaintext_envelope,
    verify_signature,
)
from app.services.wecom_suite_ticket import (
    SUITE_TICKET_MAX_BYTES,
    store_suite_ticket,
    suite_ticket_freshness,
)
from app.settings import get_wecom_third_party_settings

logger = logging.getLogger(__name__)
router = APIRouter()

_PATH = "/api/wecom/third-party/instructions"
_MAX_BODY_BYTES = 64 * 1024
_MAX_CLOCK_SKEW_SECONDS = 5 * 60
_REJECTION_REASONS = frozenset(
    {
        "configuration_error",
        "corp_id_mismatch",
        "invalid_callback_payload",
        "invalid_signature",
        "missing_encrypt",
        "stale_request",
        "storage_unavailable",
        "suite_id_mismatch",
    }
)


def _log_rejected(reason: str) -> None:
    assert reason in _REJECTION_REASONS
    logger.info("wecom_provider_instruction rejected reason=%s", reason)


def _callback_configuration() -> tuple[str, str, str, bytes]:
    settings = get_wecom_third_party_settings()
    suite_id = settings.wecom_third_party_suite_id.strip()
    corp_id = settings.wecom_third_party_corp_id.strip()
    token = settings.wecom_third_party_instruction_token.strip()
    encoded_key = settings.wecom_third_party_instruction_encoding_aes_key.strip()
    if (
        not suite_id
        or len(suite_id) > 128
        or not corp_id
        or len(corp_id) > 128
        or not token
        or len(token) > 32
        or not token.isascii()
        or not token.isalnum()
        or not encoded_key
    ):
        raise CallbackConfigurationError
    return suite_id, corp_id, token, decode_aes_key(encoded_key)


def _current_callback_timestamp(raw: str) -> int:
    if not raw.isdigit() or len(raw) > 20:
        raise CallbackInputError
    value = int(raw)
    if value <= 0 or abs(int(time.time()) - value) > _MAX_CLOCK_SKEW_SECONDS:
        raise CallbackInputError
    return value


def _parse_instruction_message(message: bytes) -> tuple[str, str, int, str | None]:
    if len(message) > _MAX_BODY_BYTES or b"<!DOCTYPE" in message.upper():
        raise CallbackInputError
    try:
        root = ElementTree.fromstring(message)
    except (ElementTree.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise CallbackInputError from exc
    if root.tag != "xml":
        raise CallbackInputError

    def text_for(tag: str) -> str:
        value = root.findtext(tag)
        return value.strip() if isinstance(value, str) else ""

    suite_id = text_for("SuiteId")
    info_type = text_for("InfoType")
    source_timestamp_raw = text_for("TimeStamp")
    if (
        not suite_id
        or len(suite_id) > 128
        or not info_type
        or len(info_type) > 64
        or not source_timestamp_raw.isdigit()
        or len(source_timestamp_raw) > 20
    ):
        raise CallbackInputError
    source_timestamp = int(source_timestamp_raw)
    if source_timestamp <= 0:
        raise CallbackInputError
    ticket = text_for("SuiteTicket") if info_type == "suite_ticket" else None
    if info_type == "suite_ticket" and (
        not ticket or len(ticket.encode("utf-8")) > SUITE_TICKET_MAX_BYTES
    ):
        raise CallbackInputError
    return suite_id, info_type, source_timestamp, ticket


@router.get(_PATH)
def verify_provider_instruction_url(
    msg_signature: str = Query(..., min_length=1, max_length=128),
    timestamp: str = Query(..., min_length=1, max_length=20),
    nonce: str = Query(..., min_length=1, max_length=128),
    echostr: str = Query(..., min_length=1, max_length=16384),
):
    """Verify the dedicated provider instruction URL without touching state."""
    try:
        _suite_id, corp_id, token, aes_key = _callback_configuration()
    except CallbackConfigurationError:
        _log_rejected("configuration_error")
        raise HTTPException(status_code=503, detail="Callback unavailable")
    try:
        _current_callback_timestamp(timestamp)
    except CallbackInputError:
        _log_rejected("stale_request")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if not verify_signature(token, timestamp, nonce, echostr, msg_signature):
        _log_rejected("invalid_signature")
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        message, receiver_id = parse_plaintext_envelope(
            decrypt_envelope(echostr, aes_key)
        )
        plaintext = message.decode("utf-8")
    except (CallbackInputError, UnicodeDecodeError):
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if receiver_id != corp_id:
        _log_rejected("corp_id_mismatch")
        raise HTTPException(status_code=403, detail="Corp ID mismatch")
    logger.info("wecom_provider_instruction accepted method=GET")
    return PlainTextResponse(plaintext, media_type="text/plain")


@router.post(_PATH)
async def receive_provider_instruction(
    request: Request,
    msg_signature: str = Query(..., min_length=1, max_length=128),
    timestamp: str = Query(..., min_length=1, max_length=20),
    nonce: str = Query(..., min_length=1, max_length=128),
    db: Session = Depends(get_db),
):
    """Validate, decrypt and atomically retain a newest suite_ticket event."""
    try:
        suite_id, corp_id, token, aes_key = _callback_configuration()
        validate_field_encryption_configuration()
    except (CallbackConfigurationError, FieldEncryptionConfigurationError):
        _log_rejected("configuration_error")
        raise HTTPException(status_code=503, detail="Callback unavailable")
    try:
        query_timestamp = _current_callback_timestamp(timestamp)
    except CallbackInputError:
        _log_rejected("stale_request")
        raise HTTPException(status_code=400, detail="Invalid callback request")

    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > _MAX_BODY_BYTES):
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    try:
        encrypted = extract_encrypt(body)
    except CallbackInputError:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if not encrypted:
        _log_rejected("missing_encrypt")
        raise HTTPException(status_code=400, detail="Missing Encrypt element")
    if not verify_signature(token, timestamp, nonce, encrypted, msg_signature):
        _log_rejected("invalid_signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        message, receiver_id = parse_plaintext_envelope(
            decrypt_envelope(encrypted, aes_key)
        )
        event_suite_id, info_type, source_timestamp, ticket = (
            _parse_instruction_message(message)
        )
    except CallbackInputError:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if receiver_id != corp_id:
        _log_rejected("corp_id_mismatch")
        raise HTTPException(status_code=403, detail="Corp ID mismatch")
    if event_suite_id != suite_id:
        _log_rejected("suite_id_mismatch")
        raise HTTPException(status_code=403, detail="Suite ID mismatch")
    if abs(source_timestamp - query_timestamp) > _MAX_CLOCK_SKEW_SECONDS:
        _log_rejected("stale_request")
        raise HTTPException(status_code=400, detail="Invalid callback request")

    if info_type != "suite_ticket":
        logger.info("wecom_provider_instruction ignored event=unsupported")
        return PlainTextResponse("success", media_type="text/plain")
    try:
        outcome = store_suite_ticket(
            db,
            suite_id=suite_id,
            ticket=ticket or "",
            source_timestamp=source_timestamp,
        )
    except (FieldEncryptionConfigurationError, SQLAlchemyError, ValueError):
        db.rollback()
        _log_rejected("storage_unavailable")
        raise HTTPException(status_code=503, detail="Callback unavailable")
    logger.info("wecom_provider_instruction accepted method=POST outcome=%s", outcome.value)
    return PlainTextResponse("success", media_type="text/plain")


@router.get("/api/platform/wecom/third-party/suite-ticket-status")
def provider_suite_ticket_status(
    response: Response,
    _admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Expose only age/state for alerting; never return suite or ticket data."""
    response.headers["Cache-Control"] = "no-store"
    suite_id = get_wecom_third_party_settings().wecom_third_party_suite_id.strip()
    if not suite_id:
        return {
            "received": False,
            "state": "unconfigured",
            "last_received_at": None,
            "age_seconds": None,
            "requires_alert": True,
        }
    return asdict(suite_ticket_freshness(db, suite_id))
