"""Minimal Fernet helpers for configuration secrets stored at rest.

Keys are read from ``SETTINGS_ENCRYPTION_KEY`` for every operation. Key
rotation (e.g. MultiFernet) is intentionally not implemented here.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken

_SETTINGS_ENCRYPTION_KEY_ENV = "SETTINGS_ENCRYPTION_KEY"


class SettingsEncryptionConfigurationError(RuntimeError):
    """Raised when settings encryption cannot be configured safely."""


class SettingsDecryptionError(RuntimeError):
    """Raised when a stored settings value cannot be decrypted."""


def _fernet() -> Fernet:
    """Return the configured Fernet instance, or fail closed without a key."""
    key = os.environ.get(_SETTINGS_ENCRYPTION_KEY_ENV, "").strip()
    if not key:
        raise SettingsEncryptionConfigurationError(
            "SETTINGS_ENCRYPTION_KEY must be configured for settings encryption"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SettingsEncryptionConfigurationError(
            "SETTINGS_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def encrypt_value(plain: str) -> str:
    """Encrypt a settings value using the configured Fernet key.

    Missing or invalid configuration raises rather than returning plaintext.
    """
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(cipher: str) -> str:
    """Decrypt a Fernet-protected settings value without exposing it on error."""
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise SettingsDecryptionError("Unable to decrypt settings value") from exc


def is_encrypted(value: str) -> bool:
    """Heuristically identify a Fernet token by its decoded version byte.

    This is not cryptographic verification and does not prove the token can be
    decrypted by the configured key. A plaintext that happens to begin with
    ``gAAAAA`` and decodes to Fernet's ``0x80`` version byte can be
    misclassified. That trade-off is acceptable for configuration secret
    values, whose known formats do not begin with that Fernet token prefix.
    """
    if not isinstance(value, str) or not value.startswith("gAAAAA"):
        return False
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
    except (ValueError, UnicodeEncodeError):
        return False
    return decoded[:1] == b"\x80"


def mask(value: str) -> str:
    """Return a fixed mask prefix and, only for long values, their last four characters.

    Values of four characters or fewer are represented by ``"****"`` so no
    source character is exposed. Longer values are represented as
    ``"****" + value[-4:]`` (for example, ``"****ab12"``).
    """
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"
