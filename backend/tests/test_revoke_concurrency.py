"""
Tests for RND-201 round 2 QA fix — concurrent idempotency
(app.revoke_reconciliation.reconcile_revoke_event / _try_link).

QA observed: two workers racing to persist the same MessageRevocation
row (SELECT-then-INSERT, no conflict handling) could raise IntegrityError,
which — if allowed to propagate to the session's outer transaction —
leaves the session in SQLAlchemy's "pending rollback" state
(PendingRollbackError on any further use), silently losing the rest of
that worker run's work.

Two kinds of coverage here:

  - Deterministic, dialect-agnostic tests that force the exact race
    window (existence check says "nothing there yet", but a conflicting
    row is already committed by the time the INSERT runs) via a targeted
    monkeypatch of the existence check. These always run, everywhere,
    and pin the exact code path QA found.

  - A real two-thread test against a live Postgres database (skipped
    cleanly when none is reachable, same convention as
    test_revoke_association_migration.py), genuinely racing two
    sessions/transactions against the same row to prove the fix holds
    under actual concurrent commits, not just a simulated window.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import PendingRollbackError

import app.revoke_reconciliation as revoke_reconciliation
from app.db.models import ArchiveMessage, MessageRevocation
from app.revoke_reconciliation import reconcile_revoke_event
from tests.test_reachability_audit import _TENANT_A, _insert_message, db  # noqa: F401 -- pytest fixture, must be imported to be discovered


def _revoke_structured_content(pre_msgid: "str | None") -> dict:
    if not pre_msgid:
        return {"fields": None, "raw": {}, "parse_warnings": ["missing_pre_msgid"]}
    return {"fields": {"pre_msgid": pre_msgid}, "raw": {"pre_msgid": pre_msgid}, "parse_warnings": []}


def _insert_revoke_event(db, *, pre_msgid, tenant_id=_TENANT_A, msgtime=5000, **kwargs):
    return _insert_message(
        db,
        msgtype="revoke",
        structured_content=_revoke_structured_content(pre_msgid),
        tenant_id=tenant_id,
        msgtime=msgtime,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Deterministic conflict-window simulation
# ---------------------------------------------------------------------------


def test_conflicting_insert_is_recovered_without_raising(db, monkeypatch) -> None:
    """Simulates the exact race: another process already committed the
    MessageRevocation row for this revoke event between our existence
    check and our own INSERT. reconcile_revoke_event() must never raise
    IntegrityError (or anything else) -- it must recover and return the
    already-existing row."""
    original = _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, seq=2)

    # A "concurrent worker" wins the race and commits first.
    winner = MessageRevocation(
        tenant_id=revoke_event.tenant_id,
        revoke_event_message_id=revoke_event.id,
        revoke_event_msgid=revoke_event.msgid,
        revoke_event_msgtime=revoke_event.msgtime,
        target_msgid=original.msgid,
        status="pending",
    )
    db.add(winner)
    db.commit()

    # Force our own call down the "nothing here yet" path so it attempts
    # the INSERT anyway, hitting the real UniqueConstraint.
    real_select = revoke_reconciliation._select_existing_revocation
    call_count = {"n": 0}

    def _lie_once(session, tenant_id, revoke_event_message_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_select(session, tenant_id, revoke_event_message_id)

    monkeypatch.setattr(revoke_reconciliation, "_select_existing_revocation", _lie_once)

    result = reconcile_revoke_event(db, revoke_event)  # must not raise

    assert result is not None
    assert result.id == winner.id
    rows = (
        db.query(MessageRevocation)
        .filter(MessageRevocation.revoke_event_message_id == revoke_event.id)
        .all()
    )
    assert len(rows) == 1  # never duplicated


def test_session_remains_usable_after_conflict_recovery(db, monkeypatch) -> None:
    """The core PendingRollbackError regression: after a conflict is
    recovered, the session must still be able to commit and be reused
    for completely unrelated work -- proving only the SAVEPOINT for the
    failed insert was rolled back, not the whole transaction."""
    original = _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, seq=2)

    winner = MessageRevocation(
        tenant_id=revoke_event.tenant_id,
        revoke_event_message_id=revoke_event.id,
        revoke_event_msgid=revoke_event.msgid,
        revoke_event_msgtime=revoke_event.msgtime,
        target_msgid=original.msgid,
        status="pending",
    )
    db.add(winner)
    db.commit()

    real_select = revoke_reconciliation._select_existing_revocation
    call_count = {"n": 0}

    def _lie_once(session, tenant_id, revoke_event_message_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_select(session, tenant_id, revoke_event_message_id)

    monkeypatch.setattr(revoke_reconciliation, "_select_existing_revocation", _lie_once)
    reconcile_revoke_event(db, revoke_event)
    monkeypatch.undo()

    # The session must not raise PendingRollbackError on further use --
    # this is the exact failure QA observed.
    try:
        another = _insert_message(
            db, msgtype="text", content_text="unrelated work continues", seq=3, tenant_id=_TENANT_A
        )
        db.commit()
    except PendingRollbackError:
        pytest.fail("session was left in a poisoned PendingRollbackError state")

    assert another.content_text == "unrelated work continues"


def test_worker_processes_the_next_revoke_event_after_a_conflict(db, monkeypatch) -> None:
    """Simulates the real decrypt-sweep shape: after one revoke event hits
    the conflict-recovery path, the SAME session must correctly process a
    completely different revoke event right after, in the same sweep."""
    _insert_message(db, msgid="orig-1", msgtype="text", content_text="a", seq=1, tenant_id=_TENANT_A)
    conflicted_event = _insert_revoke_event(db, pre_msgid="orig-1", seq=2)

    winner = MessageRevocation(
        tenant_id=conflicted_event.tenant_id,
        revoke_event_message_id=conflicted_event.id,
        revoke_event_msgid=conflicted_event.msgid,
        revoke_event_msgtime=conflicted_event.msgtime,
        target_msgid="orig-1",
        status="pending",
    )
    db.add(winner)
    db.commit()

    real_select = revoke_reconciliation._select_existing_revocation
    call_count = {"n": 0}

    def _lie_once(session, tenant_id, revoke_event_message_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_select(session, tenant_id, revoke_event_message_id)

    monkeypatch.setattr(revoke_reconciliation, "_select_existing_revocation", _lie_once)
    reconcile_revoke_event(db, conflicted_event)
    monkeypatch.undo()

    # A second, unrelated revoke event processed in the same session/sweep.
    original_2 = _insert_message(db, msgid="orig-2", msgtype="text", content_text="b", seq=3, tenant_id=_TENANT_A)
    next_event = _insert_revoke_event(db, pre_msgid="orig-2", seq=4)
    result = reconcile_revoke_event(db, next_event)
    db.commit()
    db.refresh(original_2)

    assert result.status == "linked"
    assert original_2.is_revoked is True


def test_duplicate_revoke_replay_after_conflict_recovery_stays_idempotent(db, monkeypatch) -> None:
    """Replaying the SAME revoke event again (e.g. a retried backfill
    pass) after a conflict was already recovered must still be a pure
    no-op -- no duplicate row, no state change."""
    original = _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    revoke_event = _insert_revoke_event(db, pre_msgid=original.msgid, seq=2)

    winner = MessageRevocation(
        tenant_id=revoke_event.tenant_id,
        revoke_event_message_id=revoke_event.id,
        revoke_event_msgid=revoke_event.msgid,
        revoke_event_msgtime=revoke_event.msgtime,
        target_msgid=original.msgid,
        status="pending",
    )
    db.add(winner)
    db.commit()

    real_select = revoke_reconciliation._select_existing_revocation
    call_count = {"n": 0}

    def _lie_once(session, tenant_id, revoke_event_message_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_select(session, tenant_id, revoke_event_message_id)

    monkeypatch.setattr(revoke_reconciliation, "_select_existing_revocation", _lie_once)
    first_result = reconcile_revoke_event(db, revoke_event)
    db.commit()
    monkeypatch.undo()

    # Plain replay -- normal (non-conflict) path this time.
    second_result = reconcile_revoke_event(db, revoke_event)
    db.commit()

    assert first_result.id == second_result.id
    assert (
        db.query(MessageRevocation)
        .filter(MessageRevocation.revoke_event_message_id == revoke_event.id)
        .count()
        == 1
    )


# ---------------------------------------------------------------------------
# Real two-thread race against a live Postgres database
# ---------------------------------------------------------------------------


def _postgres_admin_dsn() -> str:
    return os.environ.get(
        "RND201_TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@/postgres?host=/tmp",
    )


def _with_database_name(dsn: str, db_name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{db_name}", parsed.query, ""))


def _force_drop_database(conn, db_name: str) -> None:
    """DROP DATABASE fails with ObjectInUse if any connection is still
    attached -- this test deliberately opens several short-lived engines
    (setup, two racing workers, verify), and pool teardown via
    engine.dispose() is not always instantaneous from Postgres's point of
    view. Terminate any straggler backends first so cleanup is reliable
    rather than flaky."""
    conn.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :db AND pid <> pg_backend_pid()"
        ),
        {"db": db_name},
    )
    conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


def _postgres_reachable() -> bool:
    try:
        from sqlalchemy import create_engine as _create_engine

        engine = _create_engine(_postgres_admin_dsn())
        with engine.connect():
            pass
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable local Postgres for concurrency test")
def test_two_real_concurrent_sessions_racing_on_the_same_revoke_event() -> None:
    """Two genuinely concurrent threads, each with its own Session/
    connection to the same live Postgres database, both call
    reconcile_revoke_event() for the SAME revoke_message at (as close to)
    the same instant as a threading.Barrier can arrange. Exactly one
    MessageRevocation row must exist afterward, neither thread may raise,
    and the original must end up correctly linked."""
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import Session as _Session

    admin_dsn = _postgres_admin_dsn()
    db_name = "rnd201_concurrency_pytest"
    backend_dir = Path(__file__).resolve().parent.parent
    target_dsn = _with_database_name(admin_dsn, db_name)

    admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr

        setup_engine = _create_engine(target_dsn)
        setup_session = _Session(setup_engine)
        setup_session.execute(
            text(
                "INSERT INTO tenants (id, name, slug, is_active) "
                "VALUES (:id, 'Tenant A', 'tenant-a', true)"
            ),
            {"id": _TENANT_A},
        )
        setup_session.commit()
        original = ArchiveMessage(
            msgid="race-original", seq=1, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="text",
            content_text="hi", tenant_id=_TENANT_A,
        )
        setup_session.add(original)
        setup_session.commit()
        revoke_event = ArchiveMessage(
            msgid="race-revoke", seq=2, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="revoke",
            structured_content=_revoke_structured_content("race-original"),
            msgtime=5000, tenant_id=_TENANT_A,
        )
        setup_session.add(revoke_event)
        setup_session.commit()
        revoke_event_id = revoke_event.id
        setup_session.close()
        setup_engine.dispose()

        errors: list = []
        barrier = threading.Barrier(2)

        def _worker():
            engine = _create_engine(target_dsn)
            session = _Session(engine)
            try:
                msg = session.query(ArchiveMessage).filter(ArchiveMessage.id == revoke_event_id).one()
                barrier.wait(timeout=10)
                reconcile_revoke_event(session, msg)
                session.commit()
            except Exception as exc:  # pragma: no cover -- captured for the assertion below
                errors.append(exc)
            finally:
                session.close()
                engine.dispose()

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"worker thread(s) raised: {errors}"

        verify_engine = _create_engine(target_dsn)
        verify_session = _Session(verify_engine)
        revocations = (
            verify_session.query(MessageRevocation)
            .filter(MessageRevocation.revoke_event_message_id == revoke_event_id)
            .all()
        )
        assert len(revocations) == 1
        assert revocations[0].status == "linked"
        refreshed_original = (
            verify_session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "race-original").one()
        )
        assert refreshed_original.is_revoked is True
        verify_session.close()
        verify_engine.dispose()
    finally:
        admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            _force_drop_database(conn, db_name)
        admin_engine.dispose()


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable local Postgres for concurrency test")
def test_two_different_revoke_events_racing_to_link_the_same_original_earliest_wins() -> None:
    """RND-201 round 3 (B4) re-verification: two DIFFERENT revoke events
    (distinct revoke_event_message_id, so migration 0011's new
    UNIQUE(revoke_event_message_id) never conflicts between them) racing
    in real, concurrent threads/sessions to link the SAME original
    message. Both must link successfully (neither left dangling), and
    revoked_at on the original must deterministically end up as the
    EARLIEST of the two revoke events' own msgtime -- proving
    _apply_revocation_to_original's database-side conditional UPDATE
    (not a Python read-compare-write) is genuinely race-free under
    concurrent commits, not just correct in single-threaded tests."""
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import Session as _Session

    admin_dsn = _postgres_admin_dsn()
    db_name = "rnd201_concurrency_earliest_wins_pytest"
    backend_dir = Path(__file__).resolve().parent.parent
    target_dsn = _with_database_name(admin_dsn, db_name)

    admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr

        setup_engine = _create_engine(target_dsn)
        setup_session = _Session(setup_engine)
        setup_session.execute(
            text("INSERT INTO tenants (id, name, slug, is_active) VALUES (:id, 'Tenant A', 'tenant-a', true)"),
            {"id": _TENANT_A},
        )
        setup_session.commit()
        original = ArchiveMessage(
            msgid="race-original-2", seq=1, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="text",
            content_text="hi", tenant_id=_TENANT_A,
        )
        setup_session.add(original)
        setup_session.commit()
        # Two distinct revoke events for the SAME original -- msgtime
        # 9000 is deliberately NOT the one processed first below;
        # earliest-wins must hold regardless of thread scheduling/commit
        # order, not merely processing order.
        later_event = ArchiveMessage(
            msgid="race-revoke-later", seq=2, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="revoke",
            structured_content=_revoke_structured_content("race-original-2"),
            msgtime=9000, tenant_id=_TENANT_A,
        )
        earlier_event = ArchiveMessage(
            msgid="race-revoke-earlier", seq=3, publickey_ver=1, encrypt_random_key="k",
            encrypt_chat_msg="c", decrypt_status="success", msgtype="revoke",
            structured_content=_revoke_structured_content("race-original-2"),
            msgtime=3000, tenant_id=_TENANT_A,
        )
        setup_session.add_all([later_event, earlier_event])
        setup_session.commit()
        later_id, earlier_id = later_event.id, earlier_event.id
        setup_session.close()
        setup_engine.dispose()

        errors: list = []
        barrier = threading.Barrier(2)

        def _worker(event_id):
            engine = _create_engine(target_dsn)
            session = _Session(engine)
            try:
                msg = session.query(ArchiveMessage).filter(ArchiveMessage.id == event_id).one()
                barrier.wait(timeout=10)
                reconcile_revoke_event(session, msg)
                session.commit()
            except Exception as exc:  # pragma: no cover -- captured for the assertion below
                errors.append(exc)
            finally:
                session.close()
                engine.dispose()

        threads = [
            threading.Thread(target=_worker, args=(later_id,)),
            threading.Thread(target=_worker, args=(earlier_id,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"worker thread(s) raised: {errors}"

        verify_engine = _create_engine(target_dsn)
        verify_session = _Session(verify_engine)
        revocations = (
            verify_session.query(MessageRevocation)
            .filter(MessageRevocation.revoke_event_message_id.in_([later_id, earlier_id]))
            .all()
        )
        assert len(revocations) == 2
        assert {r.status for r in revocations} == {"linked"}  # neither left dangling

        refreshed_original = (
            verify_session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "race-original-2").one()
        )
        assert refreshed_original.is_revoked is True
        # Earliest of the two (msgtime=3000) must win, regardless of
        # which thread's transaction actually committed first.
        from datetime import datetime, timezone

        actual = refreshed_original.revoked_at
        if actual.tzinfo is None:
            actual = actual.replace(tzinfo=timezone.utc)
        assert actual.astimezone(timezone.utc) == datetime.fromtimestamp(3000 / 1000.0, tz=timezone.utc)
        verify_session.close()
        verify_engine.dispose()
    finally:
        admin_engine = _create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            _force_drop_database(conn, db_name)
        admin_engine.dispose()
