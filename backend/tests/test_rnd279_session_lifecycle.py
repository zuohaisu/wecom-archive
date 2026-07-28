"""Tests for RND-279 session lifecycle automation."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import get_session_ttl_hours
from app.db.models import AdminSession, AdminUser, Tenant
from app.session_lifecycle import cleanup_expired_sessions, touch_last_active

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 8), ("12", 12), ("0", 1), ("99999", 8760), ("-3", 1)],
)
def test_session_ttl_hours_uses_default_and_clamps(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("SESSION_TTL_HOURS", raising=False)
    else:
        monkeypatch.setenv("SESSION_TTL_HOURS", raw)
    assert get_session_ttl_hours() == expected


def test_session_ttl_hours_invalid_value_warns_and_uses_default(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SESSION_TTL_HOURS", "abc")
    assert get_session_ttl_hours() == 8
    assert "SESSION_TTL_HOURS='abc' not an int" in caplog.text


class _FakeSession:
    def __init__(self) -> None:
        self.executed = []
        self.commits = 0

    def execute(self, statement):
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)

    def commit(self) -> None:
        self.commits += 1


def test_touch_last_active_is_throttled_without_a_second_update() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user = SimpleNamespace(id="user-1", last_active_at=None)
    db = _FakeSession()

    touch_last_active(user, db, now=now)
    assert len(db.executed) == 1
    assert db.commits == 1
    assert user.last_active_at == now

    touch_last_active(user, db, now=now)
    assert len(db.executed) == 1
    assert db.commits == 1


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_cleanup_expired_sessions_removes_only_expired_or_revoked_rows() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    session_ids = [str(uuid.uuid4()) for _ in range(3)]
    now = datetime.now(timezone.utc)

    with Session(engine) as db:
        try:
            db.add(Tenant(id=tenant_id, name="RND-279 test", slug=f"rnd279-{tenant_id}"))
            db.add(
                AdminUser(
                    id=user_id,
                    tenant_id=tenant_id,
                    wecom_user_id=f"rnd279-{user_id}",
                )
            )
            db.add_all(
                [
                    AdminSession(
                        id=session_ids[0], admin_user_id=user_id, tenant_id=tenant_id,
                        wecom_user_id="valid", expires_at=now + timedelta(hours=1),
                    ),
                    AdminSession(
                        id=session_ids[1], admin_user_id=user_id, tenant_id=tenant_id,
                        wecom_user_id="expired", expires_at=now - timedelta(seconds=1),
                    ),
                    AdminSession(
                        id=session_ids[2], admin_user_id=user_id, tenant_id=tenant_id,
                        wecom_user_id="revoked", expires_at=now + timedelta(hours=1),
                        is_revoked=True,
                    ),
                ]
            )
            db.commit()

            assert cleanup_expired_sessions(db, now=now) == 2
            assert db.get(AdminSession, session_ids[0]) is not None
            assert db.get(AdminSession, session_ids[1]) is None
            assert db.get(AdminSession, session_ids[2]) is None
        finally:
            db.rollback()
            db.query(AdminSession).filter(AdminSession.tenant_id == tenant_id).delete()
            db.query(AdminUser).filter(AdminUser.id == user_id).delete()
            db.query(Tenant).filter(Tenant.id == tenant_id).delete()
            db.commit()


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_touch_last_active_is_throttled_and_preserves_last_login_at() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    login_at = datetime(2025, 12, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with Session(engine) as db:
        try:
            db.add(Tenant(id=tenant_id, name="RND-279 touch", slug=f"rnd279-{tenant_id}"))
            user = AdminUser(
                id=user_id,
                tenant_id=tenant_id,
                wecom_user_id=f"rnd279-{user_id}",
                last_login_at=login_at,
            )
            db.add(user)
            db.commit()

            touch_last_active(user, db, now=now)
            assert user.last_active_at == now
            assert user.last_login_at == login_at

            touch_last_active(user, db, now=now)
            assert user.last_active_at == now

            later = now + timedelta(seconds=400)
            touch_last_active(user, db, now=later)
            assert user.last_active_at == later
            assert user.last_login_at == login_at
        finally:
            db.rollback()
            db.query(AdminSession).filter(AdminSession.tenant_id == tenant_id).delete()
            db.query(AdminUser).filter(AdminUser.id == user_id).delete()
            db.query(Tenant).filter(Tenant.id == tenant_id).delete()
            db.commit()
