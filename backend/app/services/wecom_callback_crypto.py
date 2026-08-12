"""Shared WeCom callback signature and AES-envelope primitives.

The archive callback and the service-provider instruction callback use the
same wire-level cryptography but intentionally keep different routes and
configuration.  This module contains no environment or database access, so a
caller cannot accidentally reuse one callback's credentials for the other.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class CallbackInputError(ValueError):
    """Request-controlled callback content could not be validated."""


class CallbackConfigurationError(ValueError):
    """Server-side callback cryptographic configuration is invalid."""


_WECOM_PKCS7_BLOCK_SIZE = 32


def verify_signature(
    token: str,
    timestamp: str,
    nonce: str,
    payload: str,
    msg_signature: str,
) -> bool:
    """Verify SHA1(sort(token, timestamp, nonce, encrypted payload))."""
    computed = hashlib.sha1(
        "".join(sorted([token, timestamp, nonce, payload])).encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(computed, msg_signature)


def decode_aes_key(encoded_key: str) -> bytes:
    """Decode WeCom's configured 43-character EncodingAESKey."""
    if len(encoded_key) != 43:
        raise CallbackConfigurationError
    try:
        raw = base64.b64decode(encoded_key + "=", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CallbackConfigurationError from exc
    if len(raw) != 32:
        raise CallbackConfigurationError
    return raw


def decrypt_envelope(ciphertext_b64: str, aes_key: bytes) -> bytes:
    """Decrypt and unpad a WeCom callback ciphertext."""
    try:
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
        if not ciphertext or len(ciphertext) % (algorithms.AES.block_size // 8):
            raise CallbackInputError
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    except (binascii.Error, ValueError) as exc:
        raise CallbackInputError from exc

    pad_len = plaintext[-1]
    if pad_len < 1 or pad_len > _WECOM_PKCS7_BLOCK_SIZE:
        raise CallbackInputError
    if plaintext[-pad_len:] != bytes([pad_len]) * pad_len:
        raise CallbackInputError
    return plaintext[:-pad_len]


def parse_plaintext_envelope(plaintext: bytes) -> tuple[bytes, str]:
    """Return the inner message and verified receiver identifier."""
    if len(plaintext) < 20:
        raise CallbackInputError
    message_length = struct.unpack("!I", plaintext[16:20])[0]
    if 20 + message_length > len(plaintext):
        raise CallbackInputError
    message = plaintext[20 : 20 + message_length]
    try:
        receiver_id = plaintext[20 + message_length :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CallbackInputError from exc
    return message, receiver_id


def extract_encrypt(xml_body: bytes) -> str | None:
    """Extract an outer XML CDATA Encrypt value without entity expansion."""
    if b"<!DOCTYPE" in xml_body.upper():
        raise CallbackInputError
    try:
        ElementTree.fromstring(xml_body)
    except (ElementTree.ParseError, UnicodeDecodeError, ValueError) as exc:
        raise CallbackInputError from exc
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
        raise CallbackInputError from exc
