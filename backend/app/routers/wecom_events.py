"""
WeCom callback event handler (RND-105).

GET  /api/wecom/archive/events  — URL verification (decrypt echostr)
POST /api/wecom/archive/events  — Verified/decrypted event dispatch to archive worker
"""

from __future__ import annotations

import logging
from xml.etree import ElementTree

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db.models import TenantWecomConfig
from app.db.session import get_engine
from app.services.archive_worker_trigger import (
    ArchiveWorkerDispatch,
    dispatch_archive_worker,
)
from app.services.tenant_callback_resolution import (
    CallbackCredentialCandidate,
    CallbackResolution,
    callback_candidates,
)
from app.services.wecom_callback_crypto import (
    CallbackInputError as _CallbackInputError,
    decrypt_envelope as _decrypt_echostr,
    extract_encrypt as _extract_encrypt,
    extract_outer_corp_id as _extract_outer_corp_id,
    parse_plaintext_envelope as _parse_wecom_plaintext,
    verify_signature as _verify_signature,
)
from app.services.external_contact_refresh_trigger import (
    dispatch_external_contact_refresh,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_REJECTION_REASONS = frozenset(
    {
        "configuration_error",
        "corp_id_mismatch",
        "invalid_callback_payload",
        "invalid_signature",
        "missing_encrypt",
    }
)


def _log_rejected(reason: str) -> None:
    """Emit a searchable fixed event without request or configuration data."""
    assert reason in _REJECTION_REASONS
    logger.info("wecom_callback rejected reason=%s", reason)


def _parse_external_contact_change_event(message: bytes) -> str | None:
    """Return an ExternalUserID only for a valid change_external_contact event.

    The encrypted plaintext is still request-controlled input. Unknown valid
    event types are intentionally ignored by this feature, while malformed
    XML is rejected as an invalid callback instead of leaking parser detail.
    """
    if b"<!DOCTYPE" in message.upper():
        raise _CallbackInputError
    try:
        root = ElementTree.fromstring(message)
    except (ElementTree.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise _CallbackInputError from exc
    if root.tag != "xml":
        raise _CallbackInputError

    def text_for(tag: str) -> str:
        value = root.findtext(tag)
        return value.strip() if isinstance(value, str) else ""

    event_name = text_for("Event")
    change_type = text_for("ChangeType")
    if event_name != "change_external_contact" and change_type != "change_external_contact":
        return None

    external_userid = text_for("ExternalUserID")
    # WeCom external_userid is bounded by the same persisted model limit.
    # A malformed event is acknowledged without a targeted refresh; the
    # periodic full sync remains the durable recovery path.
    if not external_userid or len(external_userid) > 64:
        return None
    return external_userid


def _active_tenant_for_corp(corp_id: str) -> str | None:
    """Resolve callback configuration to an active tenant without exposing DB errors."""
    if not corp_id:
        return None
    try:
        with Session(get_engine()) as db:
            row = (
                db.query(TenantWecomConfig)
                .filter(TenantWecomConfig.corp_id == corp_id, TenantWecomConfig.is_active.is_(True))
                .first()
            )
            return row.tenant_id if row is not None else None
    except Exception:  # noqa: BLE001 -- callback must not expose tenant lookup detail
        return None


def _resolve_callback_candidates(outer_corp_id: str | None) -> CallbackResolution:
    """Ordered env-first candidates; DB resolution must never break the env path."""
    try:
        with Session(get_engine()) as db:
            return callback_candidates(db, outer_corp_id)
    except Exception:  # noqa: BLE001 -- fail closed to the env candidate only
        return callback_candidates(None, outer_corp_id)


# ---------------------------------------------------------------------------
# GET — URL verification (WeCom 接收事件服务器)
# ---------------------------------------------------------------------------


def _authenticate_callback(
    resolution: CallbackResolution,
    timestamp: str,
    nonce: str,
    payload: str,
    msg_signature: str,
) -> tuple[CallbackCredentialCandidate, str, str]:
    """Decrypt ``payload`` with the first candidate that authenticates it.

    A candidate authenticates when its token verifies the signature, its AES
    key decrypts the envelope, and the plaintext receiver id matches its
    corp_id (or it configures no corp_id — the legacy env behavior).  Returns
    ``(candidate, message, decrypted_corp_id)``; when no candidate matches,
    the failure maps to the fixed safe status codes and never leaks request
    or configuration data.
    """
    signature_matched = False
    payload_invalid = False
    for candidate in resolution.candidates:
        if not _verify_signature(
            candidate.token, timestamp, nonce, payload, msg_signature
        ):
            continue
        signature_matched = True
        try:
            plaintext = _decrypt_echostr(payload, candidate.aes_key)
            message, decrypted_corp_id = _parse_wecom_plaintext(plaintext)
            message_text = message.decode("utf-8")
        except (UnicodeDecodeError, _CallbackInputError):
            payload_invalid = True
            continue
        if candidate.corp_id and decrypted_corp_id != candidate.corp_id:
            continue
        return candidate, message_text, decrypted_corp_id

    if payload_invalid:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if resolution.env_configuration_error:
        _log_rejected("configuration_error")
        raise HTTPException(status_code=500, detail="Callback configuration error")
    if signature_matched:
        _log_rejected("corp_id_mismatch")
        raise HTTPException(status_code=403, detail="Corp ID mismatch")
    _log_rejected("invalid_signature")
    raise HTTPException(status_code=403, detail="Invalid signature")


@router.get("/api/wecom/archive/events")
def wecom_callback_get(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """
    WeCom URL verification callback (encrypted / safe mode).

    Called by WeCom when configuring the event server URL.
    Decrypts the echostr and returns the plaintext message.
    """
    _candidate, message, _decrypted_corp_id = _authenticate_callback(
        _resolve_callback_candidates(None),
        timestamp,
        nonce,
        echostr,
        msg_signature,
    )
    logger.info("wecom_callback accepted method=GET")
    return PlainTextResponse(content=message, media_type="text/plain")


# ---------------------------------------------------------------------------
# POST — Event signature/decryption and worker dispatch (RND-107/RND-170)
# ---------------------------------------------------------------------------


@router.post("/api/wecom/archive/events")
async def wecom_callback_post(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """
    WeCom event callback — verify/decrypt the request, then queue work.

    RND-107 keeps the acknowledgement independent from worker completion;
    RND-170 additionally queues one bounded targeted profile refresh for an
    external-contact change event.  The plaintext outer ToUserName narrows
    the stored per-tenant credential candidates before decryption.
    """
    try:
        body = await request.body()
        outer_corp_id = _extract_outer_corp_id(body)
    except _CallbackInputError:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    try:
        encrypt_content = _extract_encrypt(body)
    except _CallbackInputError:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if not encrypt_content:
        _log_rejected("missing_encrypt")
        raise HTTPException(status_code=400, detail="Missing <Encrypt> element")

    candidate, message, decrypted_corp_id = _authenticate_callback(
        _resolve_callback_candidates(outer_corp_id),
        timestamp,
        nonce,
        encrypt_content,
        msg_signature,
    )

    # The decrypted plaintext is still request-controlled input; a malformed
    # event is rejected without leaking parser detail.
    try:
        external_userid = _parse_external_contact_change_event(message.encode("utf-8"))
    except _CallbackInputError:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")

    # The callback must resolve to one active tenant before the shared worker
    # may run.  The resolved id stays in-process and is never put in
    # responses, logs, or child-process arguments.
    tenant_id = candidate.tenant_id or _active_tenant_for_corp(decrypted_corp_id)
    if tenant_id is None:
        logger.error("wecom_callback archive_worker=unavailable")
        raise HTTPException(status_code=503, detail="Callback worker unavailable")

    matched_corp_id = candidate.corp_id or decrypted_corp_id
    if external_userid:
        refresh_dispatch = dispatch_external_contact_refresh(
            tenant_id, matched_corp_id, external_userid
        )
        logger.info("wecom_callback external_contact_refresh=%s", refresh_dispatch.value)

    dispatch = dispatch_archive_worker(trigger_source="callback", tenant_id=tenant_id)
    if dispatch is ArchiveWorkerDispatch.FAILED:
        raise HTTPException(status_code=503, detail="Callback worker unavailable")

    # Media is deliberately not parsed or downloaded in this HTTP handler.
    # The shared archive entrypoint wakes the generic media worker only after
    # sync/decrypt has committed any newly actionable media metadata.
    logger.info("wecom_callback accepted method=POST")
    return PlainTextResponse(content="ok", media_type="text/plain")
