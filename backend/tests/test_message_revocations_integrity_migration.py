"""
Tests for RND-201 round 2 QA fix — Alembic migration 0010 (database-level
integrity constraints on message_revocations).

sqlite does not support adding a CHECK constraint to an existing table
via ALTER TABLE (only at CREATE TABLE time), so unlike the other
migration test files in this suite, this one is exercised entirely
against a live Postgres database (same skip-gated convention as
test_revoke_association_migration.py's live-Postgres tests) rather than
a hand-rolled sqlite pre-migration schema.

Run (from backend/):
    pytest tests/test_message_revocations_integrity_migration.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def _postgres_admin_dsn() -> str:
    return os.environ.get(
        "RND201_TEST_DATABASE_URL",
        f"postgresql://{os.environ.get('USER', 'postgres')}@/postgres?host=/tmp",
    )


def _with_database_name(dsn: str, db_name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{db_name}", parsed.query, ""))


def _postgres_reachable() -> bool:
    try:
        engine = create_engine(_postgres_admin_dsn())
        with engine.connect():
            pass
        return True
    except Exception:
        return False


def _force_drop_database(conn, db_name: str) -> None:
    conn.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :db AND pid <> pg_backend_pid()"
        ),
        {"db": db_name},
    )
    conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable local Postgres for live migration test"
)


@pytest.fixture()
def migrated_db():
    """Pinned to revision 0010 explicitly, not "head" -- this file tests
    migration 0010's own CHECK constraints in isolation. Migration 0011
    (RND-201 round 3 / B4) later makes message_revocations.tenant_id
    NOT NULL and adds composite foreign keys, which none of this file's
    fixtures/inserts satisfy (they predate tenant-aware association
    rows) -- see test_message_revocations_tenant_integrity.py for 0011's
    own focused tests against its actual schema."""
    admin_dsn = _postgres_admin_dsn()
    db_name = "rnd201_r2_integrity_pytest"
    backend_dir = Path(__file__).resolve().parent.parent
    target_dsn = _with_database_name(admin_dsn, db_name)

    admin_engine = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    env = dict(os.environ, DATABASE_URL=target_dsn)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0010"],
        cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr

    yield target_dsn, backend_dir, env

    admin_engine = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
    admin_engine.dispose()


def _seed_archive_messages(engine, count: int) -> list:
    """message_revocations.revoke_event_message_id/original_message_id
    are real foreign keys to archive_messages.id -- every insert in this
    file needs valid targets so a FOREIGN KEY violation (evaluated
    alongside the CHECK constraints) never masquerades as a CHECK
    constraint rejection in these tests. Returns the generated ids."""
    ids = []
    with engine.connect() as conn:
        with conn.begin():
            for i in range(count):
                result = conn.execute(
                    text(
                        "INSERT INTO archive_messages "
                        "(msgid, seq, publickey_ver, encrypt_random_key, encrypt_chat_msg, decrypt_status) "
                        "VALUES (:msgid, :seq, 1, 'k', 'c', 'success') RETURNING id"
                    ),
                    {"msgid": f"seed-msg-{i}", "seq": i},
                )
                ids.append(result.scalar())
    return ids


def test_constraints_exist_after_upgrade(migrated_db) -> None:
    target_dsn, _backend_dir, _env = migrated_db
    engine = create_engine(target_dsn)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_name = 'message_revocations' AND constraint_type = 'CHECK'"
            )
        ).fetchall()
    names = {r[0] for r in rows}
    assert "ck_message_revocations_status_valid" in names
    assert "ck_message_revocations_linked_consistency" in names
    engine.dispose()


def test_invalid_status_value_is_rejected(migrated_db) -> None:
    target_dsn, _backend_dir, _env = migrated_db
    engine = create_engine(target_dsn)
    [msg_id] = _seed_archive_messages(engine, 1)
    with engine.connect() as conn:
        with conn.begin(), pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status) "
                    "VALUES (:mid, 'x', 'bogus_status')"
                ),
                {"mid": msg_id},
            )
    engine.dispose()


def test_linked_without_original_message_id_is_rejected(migrated_db) -> None:
    target_dsn, _backend_dir, _env = migrated_db
    engine = create_engine(target_dsn)
    [msg_id] = _seed_archive_messages(engine, 1)
    with engine.connect() as conn:
        with conn.begin(), pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                    "VALUES (:mid, 'y', 'linked', NULL)"
                ),
                {"mid": msg_id},
            )
    engine.dispose()


def test_pending_with_original_message_id_is_rejected(migrated_db) -> None:
    target_dsn, _backend_dir, _env = migrated_db
    engine = create_engine(target_dsn)
    msg_id, original_id = _seed_archive_messages(engine, 2)
    with engine.connect() as conn:
        with conn.begin(), pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                    "VALUES (:mid, 'z', 'pending', :oid)"
                ),
                {"mid": msg_id, "oid": original_id},
            )
    engine.dispose()


def test_valid_pending_and_linked_rows_are_accepted(migrated_db) -> None:
    """Sanity check the constraints aren't overly strict -- the exact
    shapes app.revoke_reconciliation actually writes must be accepted."""
    target_dsn, _backend_dir, _env = migrated_db
    engine = create_engine(target_dsn)
    pending_id, malformed_id, linked_id, original_id = _seed_archive_messages(engine, 4)
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status) "
                    "VALUES (:mid, 'pending-event', 'pending')"
                ),
                {"mid": pending_id},
            )
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status) "
                    "VALUES (:mid, 'malformed-event', 'malformed')"
                ),
                {"mid": malformed_id},
            )
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                    "VALUES (:mid, 'linked-event', 'linked', :oid)"
                ),
                {"mid": linked_id, "oid": original_id},
            )
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM message_revocations")).scalar()
        assert count == 3
    engine.dispose()


def test_downgrade_removes_only_the_two_new_constraints(migrated_db) -> None:
    target_dsn, backend_dir, env = migrated_db
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0009"],
        cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(target_dsn)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_name = 'message_revocations' AND constraint_type = 'CHECK'"
            )
        ).fetchall()
        names = {r[0] for r in rows}
        assert "ck_message_revocations_status_valid" not in names
        assert "ck_message_revocations_linked_consistency" not in names

    # A row that would have violated the dropped constraints must now be
    # insertable -- proves the constraints are actually gone, not just
    # renamed/hidden.
    [msg_id] = _seed_archive_messages(engine, 1)
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO message_revocations "
                    "(revoke_event_message_id, revoke_event_msgid, status) "
                    "VALUES (:mid, 'post-downgrade-event', 'bogus_status')"
                ),
                {"mid": msg_id},
            )
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM message_revocations WHERE status = 'bogus_status'")
        ).scalar()
        assert count == 1

    # Clean up the intentionally-invalid row before re-upgrading -- ADD
    # CONSTRAINT validates existing rows, so leaving it in place would
    # (correctly) block re-adding the constraint. That data-migration
    # scenario is out of scope here; this block only verifies the
    # migration chain itself still runs after downgrade.
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM message_revocations WHERE status = 'bogus_status'"))
    engine.dispose()

    # Table itself (and its other constraints, e.g. the UniqueConstraint
    # from migration 0009) must still be present and functional. Re-
    # upgrades to 0010 specifically (not "head") -- this file's scope is
    # 0010 alone; see migrated_db()'s docstring.
    reup = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0010"],
        cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
    )
    assert reup.returncode == 0, reup.stderr
