"""Durable callback/decrypt dispatch contracts for external-contact refreshes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ExternalContactRefreshTask
from app.services import external_contact_refresh_trigger as trigger


@pytest.fixture()
def queue_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ExternalContactRefreshTask.__table__.create(engine)
    return engine


def test_callback_dispatch_persists_and_coalesces_without_network(
    queue_engine, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(trigger, "get_engine", lambda: queue_engine)
    monkeypatch.setattr(trigger, "_DEFAULT_SIGNAL_PATH", str(tmp_path / "refresh.trigger"))

    assert trigger.dispatch_external_contact_refresh("tenant-a", "corp-a", "wm-001") is (
        trigger.ExternalContactRefreshDispatch.ACCEPTED
    )
    assert trigger.dispatch_external_contact_refresh("tenant-a", "corp-a", "wm-001") is (
        trigger.ExternalContactRefreshDispatch.COALESCED
    )

    with Session(queue_engine) as session:
        tasks = session.query(ExternalContactRefreshTask).all()
        assert len(tasks) == 1
        assert tasks[0].tenant_id == "tenant-a"
        assert tasks[0].state == "pending"
        assert tasks[0].source == "callback"
    assert (tmp_path / "refresh.trigger").exists()


def test_new_inbound_event_reactivates_existing_task(queue_engine) -> None:
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with Session(queue_engine) as session:
        first = trigger.enqueue_external_contact_refresh(
            session, "tenant-a", "wm-001", source="callback", now=now
        )
        session.commit()
        task = session.query(ExternalContactRefreshTask).one()
        task.next_attempt_at = datetime(2026, 8, 6, tzinfo=timezone.utc)
        task.last_error_class = "unavailable"
        session.commit()

        outcome = trigger.enqueue_external_contact_refresh(
            session,
            "tenant-a",
            "wm-001",
            source="inbound-direct-message",
            now=now,
        )
        session.commit()

        refreshed = session.query(ExternalContactRefreshTask).one()
        assert first is trigger.ExternalContactRefreshDispatch.ACCEPTED
        assert outcome is trigger.ExternalContactRefreshDispatch.COALESCED
        assert refreshed.source == "inbound-direct-message"
        assert refreshed.next_attempt_at.replace(tzinfo=timezone.utc) == now
        assert refreshed.last_error_class is None


def test_invalid_identifier_never_creates_task(queue_engine) -> None:
    with Session(queue_engine) as session:
        result = trigger.enqueue_external_contact_refresh(
            session, "tenant-a", "", source="callback"
        )
        session.commit()
        assert result is trigger.ExternalContactRefreshDispatch.FAILED
        assert session.query(ExternalContactRefreshTask).count() == 0
