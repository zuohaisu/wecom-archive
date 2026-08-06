"""Minimal Fernet field-encryption helpers for credentials stored at rest.

Keys are read from ``FIELD_ENCRYPTION_KEY`` for every operation. Key rotation
(e.g. MultiFernet) is intentionally not implemented here.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken

_FIELD_ENCRYPTION_KEY_ENV = "FIELD_ENCRYPTION_KEY"


class FieldEncryptionConfigurationError(RuntimeError):
    """Raised when field encryption cannot be configured safely."""


class FieldDecryptionError(RuntimeError):
    """Raised when a stored field value cannot be decrypted."""


def _fernet() -> Fernet:
    """Return the configured Fernet instance, or fail closed without a key."""
    key = os.environ.get(_FIELD_ENCRYPTION_KEY_ENV, "").strip()
    if not key:
        raise FieldEncryptionConfigurationError(
            "FIELD_ENCRYPTION_KEY must be configured for field encryption"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise FieldEncryptionConfigurationError(
            "FIELD_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def validate_field_encryption_configuration() -> None:
    """Fail closed unless the configured Fernet key can be initialized."""
    _fernet()


def encrypt_value(plain: str) -> str:
    """Encrypt a text field using the configured Fernet key.

    Missing or invalid configuration raises rather than returning plaintext.
    """
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(cipher: str) -> str:
    """Decrypt a Fernet-protected text field without exposing its value on error."""
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise FieldDecryptionError("Unable to decrypt field value") from exc


def is_encrypted(value: str) -> bool:
    """Heuristically identify a Fernet token by its decoded version byte.

    This is not cryptographic verification and does not prove the token can be
    decrypted by the configured key. A plaintext that happens to begin with
    ``gAAAAA`` and decodes to Fernet's ``0x80`` version byte can be
    misclassified. That trade-off is acceptable for WeCom ``app_secret``
    values: their known format does not begin with that Fernet token prefix.
    """
    if not isinstance(value, str) or not value.startswith("gAAAAA"):
        return False
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
    except (ValueError, UnicodeEncodeError):
        return False
    return decoded[:1] == b"\x80"
