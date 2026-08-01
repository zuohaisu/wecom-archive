"""Persistence helpers for application-level configuration values."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.config.schema import CONFIG_REGISTRY
from app.db.models import AppConfigStore


def get_raw(db: Session, key: str) -> Optional[AppConfigStore]:
    """Return the stored value for ``key`` without decrypting it."""
    return db.get(AppConfigStore, key)


def upsert(
    db: Session,
    key: str,
    value: Optional[str],
    *,
    is_secret: bool,
    requires_restart: bool,
    updated_by: Optional[str],
) -> AppConfigStore:
    """Create or update a configuration row without committing the session."""
    stored = get_raw(db, key)
    if stored is None:
        spec = CONFIG_REGISTRY[key]
        stored = AppConfigStore(
            key=key,
            group=spec.group.value,
            value_type=spec.value_type,
        )
        db.add(stored)

    stored.value = value
    stored.is_secret = is_secret
    stored.requires_restart = requires_restart
    stored.updated_by = updated_by
    db.flush()
    return stored


def list_all(db: Session) -> list[AppConfigStore]:
    """Return all stored configuration rows in deterministic key order."""
    return db.query(AppConfigStore).order_by(AppConfigStore.key).all()
