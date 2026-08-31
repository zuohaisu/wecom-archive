"""Ordered tenant-scoped archive callback credential resolution.

Every candidate is an active ``TenantWecomConfig`` row carrying its stored
callback Token / EncodingAESKey. Rows whose corp_id matches the plaintext
outer ``ToUserName`` are tried first, followed by other active rows as a
defensive fallback for receivers the outer XML does not name.

The authoritative tenant decision is made only after decryption, by comparing
the plaintext receiver id inside the envelope with the candidate's corp_id.
Nothing in this module reads request ciphertext or logs credentials.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.crypto import FieldDecryptionError
from app.db.models import TenantWecomConfig
from app.services.wecom_callback_crypto import (
    CallbackConfigurationError,
    decode_aes_key,
)


@dataclass(frozen=True)
class CallbackCredentialCandidate:
    """One decryptable credential set; never contains request data."""

    token: str
    aes_key: bytes
    corp_id: str
    tenant_id: str


@dataclass(frozen=True)
class CallbackResolution:
    """Ordered candidates resolved exclusively from active tenant rows."""

    candidates: tuple[CallbackCredentialCandidate, ...] = ()


def _row_candidate(row: TenantWecomConfig) -> CallbackCredentialCandidate | None:
    """A per-tenant candidate, or None when its stored credentials are
    absent or undecryptable.  Decrypt failures never abort resolution."""
    if not row.has_callback_credentials:
        return None
    try:
        token = row.decrypted_callback_token
        aes_key = decode_aes_key(row.decrypted_callback_encoding_aes_key)
    except (CallbackConfigurationError, FieldDecryptionError):
        return None
    return CallbackCredentialCandidate(
        token=token,
        aes_key=aes_key,
        corp_id=row.corp_id,
        tenant_id=row.tenant_id,
    )


def callback_candidates(
    db: Session | None, outer_corp_id: str | None
) -> CallbackResolution:
    """Order tenant candidates by plaintext corp match, then active fallback.

    ``db=None`` (or a DB failure) fails closed with no candidates. Callback
    authentication must never derive a tenant from global configuration.
    """
    candidates: list[CallbackCredentialCandidate] = []
    if db is None:
        return CallbackResolution()
    try:
        rows = (
            db.query(TenantWecomConfig)
            .filter(TenantWecomConfig.is_active.is_(True))
            .order_by(TenantWecomConfig.created_at)
            .all()
        )
    except Exception:  # noqa: BLE001 -- fail closed without a global fallback
        return CallbackResolution()

    def _append_if_usable(row: TenantWecomConfig) -> None:
        candidate = _row_candidate(row)
        if candidate is not None:
            candidates.append(candidate)

    for row in rows:
        if outer_corp_id and row.corp_id == outer_corp_id:
            _append_if_usable(row)
    for row in rows:
        if not (outer_corp_id and row.corp_id == outer_corp_id):
            _append_if_usable(row)
    return CallbackResolution(candidates=tuple(candidates))
