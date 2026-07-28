"""
Session lifecycle automation (RND-279 / F0-4).

- ``touch_last_active`` maintains ``AdminUser.last_active_at`` on authenticated
  activity, separately from ``last_login_at``.
- ``cleanup_expired_sessions`` physically removes expired or revoked sessions.

The touch function uses the request's database session and never opens a
separate connection, preserving the small engine pool for request traffic.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import AdminSession, AdminUser
from app.db.session import get_engine

logger = logging.getLogger(__name__)

# Avoid updating ``updated_at`` for every authenticated request.
LAST_ACTIVE_TOUCH_INTERVAL_SECONDS = 300


def touch_last_active(user: AdminUser, db: Session, now: datetime | None = None) -> None:
    """Touch ``last_active_at`` only when the five-minute window has elapsed.

    The conditional update is race-safe: even when concurrent requests see a
    stale model value, only a row whose stored timestamp is old enough is
    changed. The request's own session is committed so the activity record is
    independent of later route-level rollbacks.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=LAST_ACTIVE_TOUCH_INTERVAL_SECONDS)
    last_active_at = user.last_active_at
    if last_active_at is not None:
        # ORM rows contain datetime values. Treat an unexpected value as
        # ineligible for maintenance rather than breaking authentication.
        if not isinstance(last_active_at, datetime):
            return
        if last_active_at.tzinfo is None:
            last_active_at = last_active_at.replace(tzinfo=timezone.utc)
        if last_active_at >= cutoff:
            return

    result = db.execute(
        update(AdminUser)
        .where(AdminUser.id == user.id)
        .where(AdminUser.last_active_at.is_(None) | (AdminUser.last_active_at <= cutoff))
        .values(last_active_at=now)
    )
    db.commit()
    if result.rowcount:
        # Keep this request's already-loaded model in sync, which also avoids
        # a second UPDATE if the dependency is evaluated again in the request.
        user.last_active_at = now


def cleanup_expired_sessions(db: Session, now: datetime | None = None) -> int:
    """Delete expired or revoked ``admin_sessions`` rows and return the count."""
    now = now or datetime.now(timezone.utc)
    deleted = (
        db.query(AdminSession)
        .filter(
            (AdminSession.expires_at <= now) | (AdminSession.is_revoked.is_(True))
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        logger.info("cleanup_expired_sessions: removed %d row(s)", deleted)
    return deleted


if __name__ == "__main__":
    engine = get_engine()
    with Session(engine) as db:
        count = cleanup_expired_sessions(db)
    print(f"cleanup_expired_sessions: removed {count} expired/revoked session(s)")
