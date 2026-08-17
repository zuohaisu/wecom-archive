"""Ordered per-tenant callback credential resolution (RND-387).

The archive callback can authenticate against the environment-scoped
single-corp deployment (the unchanged legacy path) or any active
``TenantWecomConfig`` row that carries its own stored callback Token /
EncodingAESKey (RND-386 T1).  Ordering matters:

1. the env-scoped candidate comes first — the single-corp fast path with
   zero-regression for existing deployments;
2. then rows whose corp_id matches the plaintext outer ``ToUserName`` of the
   request (WeCom writes the receiver corp_id there in plaintext);
3. then any remaining active rows with stored callback credentials, as a
   defensive fallback for receivers the outer XML does not name.

The authoritative tenant decision is made only after decryption, by
comparing the plaintext receiver id inside the envelope with the candidate's
corp_id.  Nothing in this module reads request ciphertext or logs
credentials.
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
from app.settings import get_wecom_callback_settings


@dataclass(frozen=True)
class CallbackCredentialCandidate:
    """One decryptable credential set; never contains request data."""

    token: str
    aes_key: bytes
    corp_id: str | None
    tenant_id: str | None


@dataclass(frozen=True)
class CallbackResolution:
    """Ordered candidates plus a safe signal for malformed env settings."""

    candidates: tuple[CallbackCredentialCandidate, ...] = ()
    env_configuration_error: bool = False


def _env_candidate() -> tuple[CallbackCredentialCandidate | None, bool]:
    """The env-scoped candidate, or a safe configuration-error signal.

    Absent settings are not an error (a multi-tenant deployment may rely on
    stored credentials alone); a partially configured or undecodable env is,
    so the caller can reproduce the legacy 500 configuration_error when
    nothing else matches.
    """
    settings = get_wecom_callback_settings()
    token = settings.wecom_callback_token.strip()
    raw_aes = settings.wecom_callback_encoding_aes_key.strip()
    if not token and not raw_aes:
        return None, False
    if not token or not raw_aes:
        return None, True
    try:
        aes_key = decode_aes_key(raw_aes)
    except CallbackConfigurationError:
        return None, True
    corp_id = settings.wecom_corp_id.strip()
    return (
        CallbackCredentialCandidate(
            token=token,
            aes_key=aes_key,
            corp_id=corp_id or None,
            tenant_id=None,
        ),
        False,
    )


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
    """Ordered candidates: env first, then corp-matched rows, then the rest.

    ``db=None`` (or any DB failure) degrades to the env candidate only —
    callback authentication must never expose database detail, mirroring the
    fail-closed behavior of ``wecom_events._active_tenant_for_corp``.
    """
    env_candidate, env_error = _env_candidate()
    candidates: list[CallbackCredentialCandidate] = []
    if env_candidate is not None:
        candidates.append(env_candidate)
    if db is None:
        return CallbackResolution(
            candidates=tuple(candidates), env_configuration_error=env_error
        )
    try:
        rows = (
            db.query(TenantWecomConfig)
            .filter(TenantWecomConfig.is_active.is_(True))
            .order_by(TenantWecomConfig.created_at)
            .all()
        )
    except Exception:  # noqa: BLE001 -- fail closed to the env path
        return CallbackResolution(
            candidates=tuple(candidates), env_configuration_error=env_error
        )

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
    return CallbackResolution(
        candidates=tuple(candidates), env_configuration_error=env_error
    )
