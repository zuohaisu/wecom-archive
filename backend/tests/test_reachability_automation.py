"""RND-339 lifecycle, frozen-watermark, and snapshot monotonicity tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_reachability_audit import _insert_message, _make_session

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)

_SCHEMA = """
CREATE TABLE reachability_audit_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 status TEXT NOT NULL, source TEXT NOT NULL, algorithm_version TEXT NOT NULL,
 scope_from DATETIME NOT NULL, scope_to DATETIME NOT NULL, scope_max_message_id INTEGER NOT NULL,
 matching_count INTEGER NOT NULL, checked_count INTEGER NOT NULL, reachable_count INTEGER NOT NULL,
 unreachable_count INTEGER NOT NULL, reason_counts JSON NOT NULL, safe_error_code TEXT,
 started_at DATETIME, completed_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_reachability_audit_runs_tenant_checking ON reachability_audit_runs(tenant_id) WHERE status = 'checking';
CREATE TABLE reachability_findings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 archive_message_id INTEGER NOT NULL, reason_code TEXT NOT NULL, status TEXT NOT NULL,
 first_seen DATETIME NOT NULL, last_seen DATETIME NOT NULL, resolved_at DATETIME,
 occurrence_count INTEGER NOT NULL, first_run_id INTEGER NOT NULL, last_run_id INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(tenant_id, archive_message_id, reason_code, algorithm_version)
);
"""


@pytest.fixture()
def db() -> Session:
    session = _make_session()
    for statement in _SCHEMA.strip().split(";"):
        if statement.strip():
            session.execute(text(statement))
    session.commit()
    yield session
    session.close()


def _candidate(db: Session, *, tenant: str = TENANT_A, when: datetime = NOW, sender=None, status="success"):
    return _insert_message(db, tenant_id=tenant, sender=sender, msgtime=int(when.timestamp() * 1000), decrypt_status=status)


def _run(db: Session, mode: str):
    from app.services.reachability_automation_service import create_automation_run
    run, created, ids = create_automation_run(db, TENANT_A, mode, now=NOW)
    assert run is not None and created
    return run, ids


def test_finding_model_and_migration_have_only_internal_references() -> None:
    from pathlib import Path
    from app.db.models import ReachabilityFinding

    columns = {column.name for column in ReachabilityFinding.__table__.columns}
    assert {"public_id", "tenant_id", "archive_message_id", "first_run_id", "last_run_id"} <= columns
    forbidden = {"content", "payload", "sender", "recipient", "room", "msgid", "path", "error_text"}
    assert not any(any(token in column for token in forbidden) for column in columns)
    migration = (Path(__file__).resolve().parents[1] / "alembic/versions/0033_reachability_findings.py").read_text()
    assert 'down_revision: Union[str, None] = "0032"' in migration
    assert "fk_reachability_findings_tenant_message" in migration
    assert "uq_reachability_findings_public_id" in migration


def test_incremental_is_idempotent_and_advances_only_after_complete(db: Session) -> None:
    from app.db.models import ReachabilityFinding
    from app.services.reachability_automation_service import execute_automation_run, run_automation

    first = _candidate(db)
    run, ids = _run(db, "incremental")
    assert ids == [first.id]
    assert execute_automation_run(db, TENANT_A, run.public_id, ids) == "completed"
    finding = db.query(ReachabilityFinding).one()
    assert finding.status == "active" and finding.occurrence_count == 1
    # No new candidates is deliberately not a "healthy" run or watermark move.
    assert run_automation(db, TENANT_A, "incremental") == "no_data"
    assert db.query(ReachabilityFinding).one().occurrence_count == 1


def test_partial_incremental_retries_original_interval_without_duplicate_finding(db: Session, monkeypatch) -> None:
    from app.db.models import ReachabilityFinding
    import app.services.reachability_automation_service as automation

    message = _candidate(db)
    failed, ids = _run(db, "incremental")
    monkeypatch.setattr(automation, "_sample_for_message", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    assert automation.execute_automation_run(db, TENANT_A, failed.public_id, ids) == "error"
    monkeypatch.undo()
    retry, retry_ids = _run(db, "incremental")
    assert retry_ids == [message.id]
    assert automation.execute_automation_run(db, TENANT_A, retry.public_id, retry_ids) == "completed"
    assert db.query(ReachabilityFinding).count() == 1
    assert db.query(ReachabilityFinding).one().occurrence_count == 1


def test_reconcile_catches_late_decrypt_and_only_complete_run_resolves(db: Session, monkeypatch) -> None:
    from app.db.models import ReachabilityFinding
    import app.services.reachability_automation_service as automation

    late = _candidate(db, when=NOW - timedelta(days=6), status="pending")
    _candidate(db)
    run, ids = _run(db, "incremental")
    assert automation.execute_automation_run(db, TENANT_A, run.public_id, ids) == "completed"
    db.query(type(late)).filter(type(late).id == late.id).update({"decrypt_status": "success"})
    db.commit()
    # Its id is behind the last frozen max, so the next incremental cannot see it.
    assert automation.run_automation(db, TENANT_A, "incremental") == "no_data"
    reconcile, reconcile_ids = _run(db, "reconcile")
    assert late.id in reconcile_ids
    assert automation.execute_automation_run(db, TENANT_A, reconcile.public_id, reconcile_ids) == "completed"
    assert db.query(ReachabilityFinding).filter(ReachabilityFinding.archive_message_id == late.id).one().status == "active"

    # A failed reconcile cannot close anything, even if a mock would call it reachable.
    failed, failed_ids = _run(db, "reconcile")
    monkeypatch.setattr(automation, "_sample_for_message", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    assert automation.execute_automation_run(db, TENANT_A, failed.public_id, failed_ids) == "error"
    assert db.query(ReachabilityFinding).filter(ReachabilityFinding.status == "active").count() >= 1
    monkeypatch.undo()


def test_reason_change_and_recovery_resolve_only_in_complete_reconcile(db: Session, monkeypatch) -> None:
    from app.db.models import ReachabilityFinding
    import app.services.reachability_automation_service as automation

    message = _candidate(db)
    incremental, ids = _run(db, "incremental")
    assert automation.execute_automation_run(db, TENANT_A, incremental.public_id, ids) == "completed"
    old_reason = db.query(ReachabilityFinding).one().reason_code
    changed, changed_ids = _run(db, "reconcile")
    monkeypatch.setattr(automation, "_sample_for_message", lambda *args: {"reachability_status": "unreachable_other", "reason_code": "conversation_membership_lookup_errored"})
    assert automation.execute_automation_run(db, TENANT_A, changed.public_id, changed_ids) == "completed"
    rows = db.query(ReachabilityFinding).filter(ReachabilityFinding.archive_message_id == message.id).all()
    assert {row.status for row in rows} == {"active", "resolved"}
    assert any(row.reason_code == old_reason and row.status == "resolved" for row in rows)
    monkeypatch.setattr(automation, "_sample_for_message", lambda *args: {"reachability_status": "reachable_direct", "reason_code": "ok"})
    recovery, recovery_ids = _run(db, "reconcile")
    assert automation.execute_automation_run(db, TENANT_A, recovery.public_id, recovery_ids) == "completed"
    assert all(row.status == "resolved" for row in db.query(ReachabilityFinding).all())
    # Terminal runs are not re-applied, so reconciliation retry is idempotent.
    before = [(row.status, row.occurrence_count) for row in db.query(ReachabilityFinding).all()]
    assert automation.execute_automation_run(db, TENANT_A, recovery.public_id, recovery_ids) == "not_active"
    assert before == [(row.status, row.occurrence_count) for row in db.query(ReachabilityFinding).all()]


def test_tenant_scope_and_local_evidence_snapshot_monotonicity(db: Session) -> None:
    from app.db.models import ReachabilityAuditRun, ReachabilityFinding
    from app.services.reachability_automation_service import execute_automation_run
    from app.services.reachability_check_service import create_or_reuse_run, latest_snapshot

    _candidate(db, tenant=TENANT_A)
    _candidate(db, tenant=TENANT_B)
    full, _ = create_or_reuse_run(db, TENANT_A, now=NOW)
    full.status, full.checked_count, full.reachable_count = "completed", 1, 1
    full.reason_counts, full.completed_at = {"ok": 1}, NOW
    db.commit()
    assert latest_snapshot(db, TENANT_A, now=NOW)["state"] == "healthy"
    # An incremental negative observation immediately invalidates healthy.
    incremental, created = create_or_reuse_run(
        db, TENANT_A, now=NOW + timedelta(minutes=1), source="incremental",
        scope_from=NOW - timedelta(days=7), scope_to=NOW, scope_max_message_id=1, matching_count=1,
    )
    assert created
    assert execute_automation_run(db, TENANT_A, incremental.public_id, [1]) == "completed"
    assert latest_snapshot(db, TENANT_A, now=NOW)["state"] == "attention"
    # Tenant B has no A finding and is unaffected.
    assert db.query(ReachabilityFinding).filter(ReachabilityFinding.tenant_id == TENANT_B).count() == 0
    assert db.query(ReachabilityAuditRun).filter(ReachabilityAuditRun.tenant_id == TENANT_B).count() == 0
