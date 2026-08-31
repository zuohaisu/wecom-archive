"""Tenant-scoped private-key providers used by the decrypt CLI.

``local_file`` resolves the PEM path registered for the selected tenant's
``KeyVersion``. ``kms_envelope`` stores a Fernet envelope in that same column;
the decrypted PEM exists only in process memory. Neither provider accepts a
global private-key fallback.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from app.crypto import decrypt_value, encrypt_value
from app.db.models import KeyVersion


class KeyProviderError(RuntimeError):
    """A key could not be obtained without exposing sensitive material."""


class KeyProvider(ABC):
    """Resolve a private key for one tenant-scoped WeCom key version."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _key_version(self, tenant_id: str, publickey_ver: int) -> KeyVersion | None:
        return (
            self._session.query(KeyVersion)
            .filter(
                KeyVersion.tenant_id == tenant_id,
                KeyVersion.publickey_ver == publickey_ver,
                KeyVersion.is_active == True,  # noqa: E712
            )
            .one_or_none()
        )

    @abstractmethod
    def get_private_key(self, tenant_id: str, publickey_ver: int) -> rsa.RSAPrivateKey:
        """Return the matching private key, or raise KeyProviderError."""


class LocalFileKeyProvider(KeyProvider):
    """Load a PEM file registered for one tenant-scoped key version."""

    @staticmethod
    def _load_pem_file(path: str) -> rsa.RSAPrivateKey:
        try:
            with open(path, "rb") as key_file:
                return serialization.load_pem_private_key(key_file.read(), password=None)
        except (OSError, TypeError, ValueError) as exc:
            raise KeyProviderError("Private key retrieval failed") from exc

    def get_private_key(self, tenant_id: str, publickey_ver: int) -> rsa.RSAPrivateKey:
        try:
            key_version = self._key_version(tenant_id, publickey_ver)
        except Exception as exc:
            raise KeyProviderError("Private key retrieval failed") from exc
        if key_version is None or not key_version.private_key_path:
            raise KeyProviderError("Private key retrieval failed")
        return self._load_pem_file(key_version.private_key_path)


class KmsEnvelopeKeyProvider(KeyProvider):
    """Use Fernet envelopes for database-held PEMs; never writes plaintext."""

    def store_private_key(self, tenant_id: str, publickey_ver: int, pem: str) -> None:
        """Encrypt a PEM into its existing KeyVersion storage field."""
        key_version = self._key_version(tenant_id, publickey_ver)
        if key_version is None:
            raise KeyProviderError("Private key retrieval failed")
        try:
            key_version.private_key_path = encrypt_value(pem)
            self._session.flush()
        except Exception as exc:
            raise KeyProviderError("Private key retrieval failed") from exc

    def get_private_key(self, tenant_id: str, publickey_ver: int) -> rsa.RSAPrivateKey:
        key_version = self._key_version(tenant_id, publickey_ver)
        if key_version is None:
            raise KeyProviderError("Private key retrieval failed")
        try:
            pem = decrypt_value(key_version.private_key_path)
            return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        except Exception as exc:
            raise KeyProviderError("Private key retrieval failed") from exc


def get_key_provider(session: Session) -> KeyProvider:
    """Build the configured provider; local_file remains the safe default."""
    provider_name = os.environ.get("KEY_PROVIDER", "local_file").strip().lower()
    if provider_name == "local_file":
        return LocalFileKeyProvider(session)
    if provider_name == "kms_envelope":
        return KmsEnvelopeKeyProvider(session)
    raise KeyProviderError("KEY_PROVIDER must be local_file or kms_envelope")
