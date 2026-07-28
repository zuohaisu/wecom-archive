"""
WeCom callback event handler (RND-105).

GET  /api/wecom/archive/events  — URL verification (decrypt echostr)
POST /api/wecom/archive/events  — Event signature validation (no worker trigger)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import struct

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
    """Decode 43-char base64 AES key (+ '=' padding), expect 32 bytes."""
    if len(encoded_key) != 43:
        raise ValueError("Encoding AES key must be exactly 43 characters")
    raw = base64.b64decode(encoded_key + "=")
    if len(raw) != 32:
        raise ValueError("Decoded AES key is not 32 bytes")
    return raw


def _decrypt_echostr(echostr_b64: str, aes_key: bytes) -> bytes:
    """Base64-decode echostr, AES-CBC decrypt (IV = first 16 bytes of key),
    then remove PKCS#7 padding."""
    ciphertext = base64.b64decode(echostr_b64)
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # Remove PKCS#7 padding
    pad_len = plaintext[-1]
    if pad_len < 1 or pad_len > 32:
        raise ValueError("Invalid PKCS#7 padding byte")
    if plaintext[-pad_len:] != bytes([pad_len] * pad_len):
        raise ValueError("Invalid PKCS#7 padding block")
    return plaintext[:-pad_len]


def _parse_wecom_plaintext(plaintext: bytes) -> tuple[bytes, str]:
    """Parse WeCom plaintext: 16 random bytes + 4-byte BE msg_len + msg + corp_id."""
    if len(plaintext) < 20:
        raise ValueError("Plaintext too short for WeCom message envelope")
    msg_len = struct.unpack("!I", plaintext[16:20])[0]
    if 20 + msg_len > len(plaintext):
        raise ValueError("msg_len exceeds plaintext length")
    msg = plaintext[20 : 20 + msg_len]
    corp_id = plaintext[20 + msg_len :].decode("utf-8")
    return msg, corp_id


def _extract_encrypt(xml_body: bytes) -> str | None:
    """Extract <Encrypt> CDATA content from a WeCom XML callback body."""
    match = re.search(
        rb"<Encrypt>\s*<!\[CDATA\[(.*?)\]\]>\s*</Encrypt>",
        xml_body,
        re.DOTALL,
    )
    if match:
        return match.group(1).decode("utf-8")
    return None


def _get_token() -> str:
    token = get_wecom_callback_settings().wecom_callback_token.strip()
    if not token:
        raise HTTPException(status_code=500, detail="Callback token not configured")
    return token


def _get_aes_key() -> bytes:
    raw = get_wecom_callback_settings().wecom_callback_encoding_aes_key.strip()
    if not raw:
        raise HTTPException(status_code=500, detail="Callback encoding AES key not configured")
    try:
        return _decode_aes_key(raw)
    except Exception:
        raise HTTPException(status_code=500, detail="Callback AES key invalid")


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
    token = _get_token()
    aes_key = _get_aes_key()
    corp_id = _get_corp_id()

    # 1. Verify signature: SHA1(sort(token, timestamp, nonce, echostr))
    if not _verify_signature(token, timestamp, nonce, echostr, msg_signature):
        logger.warning("wecom_callback_get rejected: invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # 2. Decrypt echostr → extract plaintext message
    try:
        plaintext = _decrypt_echostr(echostr, aes_key)
        msg, decrypted_corp_id = _parse_wecom_plaintext(plaintext)
    except Exception:
        logger.warning("wecom_callback_get rejected: echostr decryption failed")
        raise HTTPException(status_code=500, detail="Echostr decryption failed")

    # 3. Verify corp_id if configured
    if corp_id and decrypted_corp_id != corp_id:
        logger.warning("wecom_callback_get rejected: corp_id mismatch")
        raise HTTPException(status_code=403, detail="Corp ID mismatch")

    logger.info("wecom_callback_get accepted")
    return PlainTextResponse(content=msg.decode("utf-8"), media_type="text/plain")


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
    token = _get_token()

    xml_body = await request.body()

    encrypt_content = _extract_encrypt(xml_body)
    if not encrypt_content:
        logger.warning("wecom_callback_post rejected: missing Encrypt")
        raise HTTPException(status_code=400, detail="Missing <Encrypt> element")

    if not _verify_signature(token, timestamp, nonce, encrypt_content, msg_signature):
        logger.warning("wecom_callback_post rejected: invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # This is intentionally independent of RND-107's archive-worker trigger:
    # a dispatcher failure must never change WeCom's callback acknowledgement.
    tenant_id = _active_tenant_for_corp(_get_corp_id())
    if tenant_id is not None:
        try:
            trigger_recent_image_download(tenant_id, triggered_by="callback")
        except Exception:
            pass

    logger.info("wecom_callback_post accepted")
    return PlainTextResponse(content="ok", media_type="text/plain")
