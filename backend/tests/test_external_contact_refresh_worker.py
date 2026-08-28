"""Small-batch, durable external-contact refresh worker tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import ExternalContactRefreshTask
from app.services import external_contact_refresh_worker as worker
from app.services.external_contact_sync import RefreshResult


@pytest.fixture()
def queue_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ExternalContactRefreshTask.__table__.create(engine)
    return engine


def _task(external_userid: str, now: datetime) -> ExternalContactRefreshTask:
    return ExternalContactRefreshTask(
        tenant_id="tenant-a",
        external_userid=external_userid,
        source="inbound-direct-message",
        state="pending",
        next_attempt_at=now,
    )


def test_worker_deletes_successful_tasks_one_at_a_time(
    queue_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with Session(queue_engine) as session:
        session.add_all([_task("wm-001", now), _task("wm-002", now)])
        session.commit()
        calls: list[tuple[str, bool]] = []

        def _success(_session, _tenant, _corp, _secret, external_userid, **kwargs):
            calls.append((external_userid, kwargs["update_interaction_stats"]))
            return RefreshResult(found=True, inserted=True)

        monkeypatch.setattr(worker, "refresh_external_contact", _success)
        summary = worker.run_external_contact_refresh_queue(
            session, "tenant-a", "corp-a", "secret", limit=1, now=now
        )

        assert summary.selected == 1
        assert summary.refreshed == 1
        assert summary.unavailable == 0
        assert calls == [("wm-001", False)]
        assert session.query(ExternalContactRefreshTask).count() == 1


def test_worker_backoffs_unavailable_task_without_dropping_it(
    queue_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with Session(queue_engine) as session:
        session.add(_task("wm-001", now))
        session.commit()
        monkeypatch.setattr(
            worker,
            "refresh_external_contact",
            lambda *_args, **_kwargs: RefreshResult(found=False),
        )

        summary = worker.run_external_contact_refresh_queue(
            session, "tenant-a", "corp-a", "secret", now=now
        )

        task = session.query(ExternalContactRefreshTask).one()
        assert (summary.selected, summary.refreshed, summary.unavailable) == (1, 0, 1)
        assert task.attempt_count == 1
        assert task.last_error_class == "unavailable"
        assert task.next_attempt_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 8, 5, 0, 5, tzinfo=timezone.utc
        )
