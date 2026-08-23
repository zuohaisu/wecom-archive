"""Platform operator account lifecycle and password-policy domain logic (RND-415)."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import hash_password, verify_password
from app.db.models import (
    PlatformAdmin,
    PlatformAdminInvitation,
    PlatformAdminPasswordHistory,
    PlatformAdminSession,
    Tenant,
)
from app.email import send_invite_email
from app.settings import get_email_settings, get_wecom_oauth_settings

_INVITATION_TTL = timedelta(hours=24)
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class PlatformAccountError(ValueError):
    """A stable, non-sensitive API error raised by account-domain helpers."""

    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: str, *, max_length: int, code: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length or "\x00" in cleaned:
        raise PlatformAccountError(code)
    return cleaned


def _normalise_email(email: str) -> str:
    normalised = _clean_text(email, max_length=320, code="invalid_operator_email").lower()
    if not _EMAIL_RE.fullmatch(normalised):
        raise PlatformAccountError("invalid_operator_email")
    return normalised


def _normalise_reason_and_note(reason_code: str, note: str) -> tuple[str, str]:
    reason = _clean_text(reason_code, max_length=64, code="invalid_reason_code")
    if not _REASON_CODE_RE.fullmatch(reason):
        raise PlatformAccountError("invalid_reason_code")
    return reason, _clean_text(note, max_length=1000, code="invalid_audit_note")


def _platform_audit_tenant_id(db: Session) -> str:
    """Return the established default-tenant audit anchor for platform actors.

    Platform admins are tenant-less while ``AuditLog.tenant_id`` deliberately
    is not. RND-413 uses the default tenant for platform login/logout; account
    security actions follow that convention and fail closed if it is absent.
    """

    tenant = db.query(Tenant).filter(Tenant.slug == "default").first()
    if tenant is None:
        raise PlatformAccountError("platform_audit_unavailable", status_code=503)
    return tenant.id


def _write_platform_audit(
    db: Session,
    *,
    action: str,
    object_type: str,
    object_id: str,
    actor_id: str,
    detail: dict,
    audit_id: str,
) -> None:
    safe_detail = {"platform_admin_id": actor_id}
    safe_detail.update(detail)
    if not write_audit(
        db,
        tenant_id=_platform_audit_tenant_id(db),
        action=action,
        object_type=object_type,
        object_id=object_id,
        admin_user_id=None,
        detail=safe_detail,
        audit_id=audit_id,
    ):
        db.rollback()
        raise PlatformAccountError("platform_audit_unavailable", status_code=503)


def _validate_new_password(
    password: str,
    *,
    current_hash: Optional[str],
    historical_hashes: list[str],
) -> None:
    """Apply the v1 policy without retaining or returning plaintext values."""

    if len(password) < 12:
        raise PlatformAccountError("weak_password")
    if not any(char.islower() for char in password):
        raise PlatformAccountError("weak_password")
    if not any(char.isupper() for char in password):
        raise PlatformAccountError("weak_password")
    if not any(char.isdigit() for char in password):
        raise PlatformAccountError("weak_password")

    candidates = ([] if current_hash is None else [current_hash]) + historical_hashes
    if any(verify_password(password, candidate) for candidate in candidates):
        raise PlatformAccountError("recent_password_reused")


def change_platform_password(
    db: Session,
    *,
    actor: PlatformAdmin,
    current_password: str,
    new_password: str,
    new_password_confirmation: str,
    revoke_other_sessions: bool,
    reason_code: str,
    note: str,
    current_session_id: Optional[str],
) -> bool:
    """Verify and rotate one operator password, preserving a five-password window."""

    if actor.password_hash is None or not verify_password(current_password, actor.password_hash):
        raise PlatformAccountError("invalid_current_password", status_code=401)
    if new_password != new_password_confirmation:
        raise PlatformAccountError("password_confirmation_mismatch")
    reason, safe_note = _normalise_reason_and_note(reason_code, note)
    history = (
        db.query(PlatformAdminPasswordHistory)
        .filter(PlatformAdminPasswordHistory.platform_admin_id == actor.id)
        .order_by(
            PlatformAdminPasswordHistory.created_at.desc(),
            PlatformAdminPasswordHistory.id.desc(),
        )
        .limit(4)
        .all()
    )
    _validate_new_password(
        new_password,
        current_hash=actor.password_hash,
        historical_hashes=[row.password_hash for row in history],
    )

    previous_hash = actor.password_hash
    actor.password_hash = hash_password(new_password)
    db.add(
        PlatformAdminPasswordHistory(
            id=str(uuid.uuid4()),
            platform_admin_id=actor.id,
            password_hash=previous_hash,
        )
    )
    if revoke_other_sessions:
        sessions = db.query(PlatformAdminSession).filter(
            PlatformAdminSession.platform_admin_id == actor.id,
            PlatformAdminSession.is_revoked.is_(False),
        )
        if current_session_id:
            sessions = sessions.filter(PlatformAdminSession.id != current_session_id)
        sessions.update({PlatformAdminSession.is_revoked: True}, synchronize_session=False)

    _write_platform_audit(
        db,
        action=AuditAction.PLATFORM_OPERATOR_PASSWORD_CHANGED,
        object_type=AuditObjectType.PLATFORM_OPERATOR,
        object_id=actor.id,
        actor_id=actor.id,
        detail={
            "reason_code": reason,
            "note": safe_note,
            "revoked_other_sessions": revoke_other_sessions,
        },
        audit_id=str(uuid.uuid4()),
    )
    try:
        db.commit()
    except Exception as error:  # pragma: no cover - driver-specific branch
        db.rollback()
        raise PlatformAccountError("platform_password_change_failed", status_code=503) from error
    return revoke_other_sessions


def _invitation_base_url() -> str:
    base = (
        get_email_settings().invite_base_url.strip()
        or get_wecom_oauth_settings().admin_domain.strip()
    )
    if not base.startswith(("https://", "http://")):
        raise PlatformAccountError("platform_invite_base_url_unavailable", status_code=503)
    return base.rstrip("/")


def invite_platform_operator(
    db: Session,
    *,
    actor: PlatformAdmin,
    name: str,
    email: str,
    reason_code: str,
    note: str,
) -> dict:
    """Create a pending super-admin and mail its one-time activation link."""

    clean_name = _clean_text(name, max_length=160, code="invalid_operator_name")
    clean_email = _normalise_email(email)
    reason, safe_note = _normalise_reason_and_note(reason_code, note)
    base_url = _invitation_base_url()
    if db.query(PlatformAdmin).filter(PlatformAdmin.email == clean_email).first() is not None:
        raise PlatformAccountError("operator_email_already_exists", status_code=409)

    raw_token = secrets.token_urlsafe(32)
    now = _now()
    operator = PlatformAdmin(
        id=str(uuid.uuid4()),
        name=clean_name,
        email=clean_email,
        password_hash=None,
        role="superadmin",
        status="pending",
    )
    invitation = PlatformAdminInvitation(
        id=str(uuid.uuid4()),
        platform_admin_id=operator.id,
        invited_by_platform_admin_id=actor.id,
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        note=safe_note,
        expires_at=now + _INVITATION_TTL,
    )
    db.add_all((operator, invitation))
    try:
        # Surface the unique-email race before attempting an external side
        # effect. The raw token never enters the database or a response.
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise PlatformAccountError("operator_email_already_exists", status_code=409) from error

    audit_id = str(uuid.uuid4())
    _write_platform_audit(
        db,
        action=AuditAction.PLATFORM_OPERATOR_INVITED,
        object_type=AuditObjectType.PLATFORM_OPERATOR_INVITATION,
        object_id=invitation.id,
        actor_id=actor.id,
        detail={
            "invited_platform_admin_id": operator.id,
            "reason_code": reason,
            "note": safe_note,
        },
        audit_id=audit_id,
    )
    accept_link = f"{base_url}/platform/accept-invite?token={raw_token}"
    if not send_invite_email(clean_email, accept_link):
        db.rollback()
        raise PlatformAccountError("platform_invitation_delivery_failed", status_code=503)
    try:
        db.commit()
    except Exception as error:  # pragma: no cover - driver-specific branch
        db.rollback()
        raise PlatformAccountError("platform_invitation_failed", status_code=503) from error
    return {
        "operator_id": operator.id,
        "email": operator.email,
        "role": "super_admin",
        "status": operator.status,
        "audit_id": audit_id,
    }


def accept_platform_operator_invitation(
    db: Session,
    *,
    token: str,
    password: str,
    password_confirmation: str,
) -> None:
    """Consume one unexpired invitation and activate its operator account."""

    if password != password_confirmation:
        raise PlatformAccountError("password_confirmation_mismatch")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    invitation = (
        db.query(PlatformAdminInvitation)
        .filter(
            PlatformAdminInvitation.token_hash == digest,
            PlatformAdminInvitation.used_at.is_(None),
            PlatformAdminInvitation.expires_at > _now(),
        )
        .first()
    )
    if invitation is None:
        raise PlatformAccountError("invalid_or_expired_invitation")
    operator = db.get(PlatformAdmin, invitation.platform_admin_id)
    if operator is None or operator.status != "pending" or operator.password_hash is not None:
        raise PlatformAccountError("invalid_or_expired_invitation")
    _validate_new_password(password, current_hash=None, historical_hashes=[])

    operator.password_hash = hash_password(password)
    operator.status = "active"
    invitation.used_at = _now()
    _write_platform_audit(
        db,
        action=AuditAction.PLATFORM_OPERATOR_INVITE_ACCEPTED,
        object_type=AuditObjectType.PLATFORM_OPERATOR,
        object_id=operator.id,
        actor_id=operator.id,
        detail={"invited_by_platform_admin_id": invitation.invited_by_platform_admin_id},
        audit_id=str(uuid.uuid4()),
    )
    try:
        db.commit()
    except Exception as error:  # pragma: no cover - driver-specific branch
        db.rollback()
        raise PlatformAccountError("platform_invitation_accept_failed", status_code=503) from error


def list_platform_operators(db: Session) -> list[dict]:
    """Return the safe, cross-account operator directory for the v1 UI."""

    operators = db.query(PlatformAdmin).order_by(PlatformAdmin.email.asc()).all()
    return [
        {
            "id": operator.id,
            "name": operator.name,
            "email": operator.email,
            "role": "super_admin",
            "status": operator.status,
            "last_login_at": operator.last_login_at,
        }
        for operator in operators
    ]
