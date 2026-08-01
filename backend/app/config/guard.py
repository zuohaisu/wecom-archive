"""Initialization and settings-session guards for deployment bootstrap."""

from __future__ import annotations

from typing import Tuple

from fastapi import Depends
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_auth_mode, get_current_user
from app.config.repository import get_raw
from app.config.resolver import resolve
from app.db.models import AdminUser
from app.settings import get_auth_settings


_WECOM_INITIALIZATION_KEYS = (
    "wecom_corp_id",
    "wecom_agent_id",
    "wecom_oauth_secret",
)


def get_bootstrap_config_value(db: Session, key: str, fallback: str) -> str:
    """Read a bootstrap-only DB value, retaining legacy env fallback safely."""
    try:
        stored = get_raw(db, key)
    except SQLAlchemyError:
        return fallback
    return stored.value if isinstance(getattr(stored, "value", None), str) else fallback


def is_initialized(db: Session) -> bool:
    """Return whether the active authentication mode has its required setup."""
    if get_auth_mode() == "password":
        value = get_bootstrap_config_value(
            db, "admin_password_hash", get_auth_settings().admin_password_hash
        )
        return bool(value.strip())
    return all((resolve(db, key) or "").strip() for key in _WECOM_INITIALIZATION_KEYS)


def require_settings_admin(
    current: Tuple[AdminUser, str] = Depends(get_current_user),
) -> Tuple[AdminUser, str]:
    """Require the existing authenticated admin session; MVP has no separate role gate."""
    return current
