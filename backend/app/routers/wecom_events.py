"""
WeCom callback event handler (RND-105).

GET  /api/wecom/archive/events  — URL verification (decrypt echostr)
POST /api/wecom/archive/events  — Event signature validation (no worker trigger)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import struct
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from sqlalchemy.orm import Session

from app.db.models import TenantWecomConfig
from app.db.session import get_engine
from app.media_event_dispatch import trigger_recent_image_download
from app.settings import get_wecom_callback_settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Request-controlled callback failures are deliberately separate from server
# configuration failures.  The public endpoint must never turn malformed
# ciphertext, padding, envelopes, or XML into a 500 response.
class _CallbackInputError(ValueError):
    pass


class _CallbackConfigurationError(ValueError):
    pass


# WeCom specifies its PKCS#7 padding block size as 32 bytes, even though
# AES-CBC ciphertext itself must remain aligned to AES's 16-byte block size.
_WECOM_PKCS7_BLOCK_SIZE = 32

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _verify_signature(
    token: str, timestamp: str, nonce: str, payload: str, msg_signature: str
) -> bool:
    """Sort token, timestamp, nonce, payload; SHA1; compare to msg_signature."""
    parts = sorted([token, timestamp, nonce, payload])
    computed = _sha1("".join(parts))
    return hmac.compare_digest(computed, msg_signature)


def _decode_aes_key(encoded_key: str) -> bytes:
    """Decode the configured 43-char base64 AES key into its 32 raw bytes."""
    if len(encoded_key) != 43:
        raise _CallbackConfigurationError
    try:
        raw = base64.b64decode(encoded_key + "=", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _CallbackConfigurationError from exc
    if len(raw) != 32:
        raise _CallbackConfigurationError
    return raw


def _decrypt_echostr(echostr_b64: str, aes_key: bytes) -> bytes:
    """Decrypt request ciphertext and normalize all input failures safely."""
    try:
        ciphertext = base64.b64decode(echostr_b64, validate=True)
        if not ciphertext or len(ciphertext) % (algorithms.AES.block_size // 8):
            raise _CallbackInputError
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    except (binascii.Error, ValueError) as exc:
        raise _CallbackInputError from exc

    pad_len = plaintext[-1]
    if pad_len < 1 or pad_len > _WECOM_PKCS7_BLOCK_SIZE:
        raise _CallbackInputError
    if plaintext[-pad_len:] != bytes([pad_len]) * pad_len:
        raise _CallbackInputError
    return plaintext[:-pad_len]


def _parse_wecom_plaintext(plaintext: bytes) -> tuple[bytes, str]:
    """Parse a request-controlled WeCom plaintext envelope."""
    if len(plaintext) < 20:
        raise _CallbackInputError
    msg_len = struct.unpack("!I", plaintext[16:20])[0]
    if 20 + msg_len > len(plaintext):
        raise _CallbackInputError
    msg = plaintext[20 : 20 + msg_len]
    try:
        corp_id = plaintext[20 + msg_len :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CallbackInputError from exc
    return msg, corp_id


def _extract_encrypt(xml_body: bytes) -> str | None:
    """Extract a UTF-8 CDATA Encrypt value, rejecting malformed request bytes."""
    if b"<!DOCTYPE" in xml_body.upper():
        raise _CallbackInputError
    try:
        ElementTree.fromstring(xml_body)
    except (ElementTree.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise _CallbackInputError from exc
    match = re.search(
        rb"<Encrypt>\s*<!\[CDATA\[(.*?)\]\]>\s*</Encrypt>",
        xml_body,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return match.group(1).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CallbackInputError from exc


def _get_token() -> str:
    token = get_wecom_callback_settings().wecom_callback_token.strip()
    if not token:
        raise _CallbackConfigurationError
    return token


def _get_aes_key() -> bytes:
    raw = get_wecom_callback_settings().wecom_callback_encoding_aes_key.strip()
    if not raw:
        raise _CallbackConfigurationError
    return _decode_aes_key(raw)


def _get_corp_id() -> str:
    return get_wecom_callback_settings().wecom_corp_id.strip()


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
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GET — URL verification (WeCom 接收事件服务器)
# ---------------------------------------------------------------------------


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
    try:
        token = _get_token()
        aes_key = _get_aes_key()
    except _CallbackConfigurationError:
        _log_rejected("configuration_error")
        raise HTTPException(status_code=500, detail="Callback configuration error")
    corp_id = _get_corp_id()

    # 1. Verify signature: SHA1(sort(token, timestamp, nonce, echostr)).
    if not _verify_signature(token, timestamp, nonce, echostr, msg_signature):
        _log_rejected("invalid_signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # 2. Decrypt and parse only request-controlled data as a safe 400.
    try:
        plaintext = _decrypt_echostr(echostr, aes_key)
        msg, decrypted_corp_id = _parse_wecom_plaintext(plaintext)
        message = msg.decode("utf-8")
    except (UnicodeDecodeError, _CallbackInputError):
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")

    # 3. Verify corp_id if configured.
    if corp_id and decrypted_corp_id != corp_id:
        _log_rejected("corp_id_mismatch")
        raise HTTPException(status_code=403, detail="Corp ID mismatch")

    logger.info("wecom_callback accepted method=GET")
    return PlainTextResponse(content=message, media_type="text/plain")


# ---------------------------------------------------------------------------
# POST — Event signature validation (no worker trigger in RND-105)
# ---------------------------------------------------------------------------


@router.post("/api/wecom/archive/events")
async def wecom_callback_post(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """
    WeCom event callback — validate signature only (RND-105).

    Does NOT trigger the archive worker.
    RND-107 will trigger archive worker after valid POST event.
    """
    try:
        token = _get_token()
    except _CallbackConfigurationError:
        _log_rejected("configuration_error")
        raise HTTPException(status_code=500, detail="Callback configuration error")

    try:
        encrypt_content = _extract_encrypt(await request.body())
    except _CallbackInputError:
        _log_rejected("invalid_callback_payload")
        raise HTTPException(status_code=400, detail="Invalid callback request")
    if not encrypt_content:
        _log_rejected("missing_encrypt")
        raise HTTPException(status_code=400, detail="Missing <Encrypt> element")

    if not _verify_signature(token, timestamp, nonce, encrypt_content, msg_signature):
        _log_rejected("invalid_signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # This is intentionally independent of RND-107's archive-worker trigger:
    # a dispatcher failure must never change WeCom's callback acknowledgement.
    tenant_id = _active_tenant_for_corp(_get_corp_id())
    if tenant_id is not None:
        try:
            trigger_recent_image_download(tenant_id, triggered_by="callback")
        except Exception:
            pass

    logger.info("wecom_callback accepted method=POST")
    return PlainTextResponse(content="ok", media_type="text/plain")
