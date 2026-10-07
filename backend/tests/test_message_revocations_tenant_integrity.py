"""
Tests for RND-201 round 3 QA fix (B4) — Alembic migration 0011:
database-enforced tenant and association integrity on
message_revocations.

Round 2 QA rejected the release because the database still accepted
tenant_id=NULL, duplicate association rows per revoke event, and rows
whose declared tenant disagreed with the tenant of the archive_messages
row(s) they reference — app-level tenant filtering alone was judged
insufficient. This migration closes all of those gaps with real
Postgres constraints (NOT NULL, a plain UNIQUE(revoke_event_message_id),
and composite foreign keys to archive_messages(tenant_id, id)).

sqlite cannot express any of migration 0011's constraint types on an
existing table via ALTER (no ALTER-time NOT NULL/UNIQUE/composite-FK
support), so — same convention as
test_message_revocations_integrity_migration.py (migration 0010) — this
entire file runs against a live, disposable Postgres database, skipping
cleanly when none is reachable.

Run (from backend/):
    pytest tests/test_message_revocations_tenant_integrity.py -v
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


def _run_alembic(backend_dir: Path, env: dict, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
    )


def _fresh_database(db_name: str):
    admin_dsn = _postgres_admin_dsn()
    target_dsn = _with_database_name(admin_dsn, db_name)
    admin_engine = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()
    return target_dsn


def _drop_database(db_name: str) -> None:
    admin_dsn = _postgres_admin_dsn()
    admin_engine = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        _force_drop_database(conn, db_name)
    admin_engine.dispose()


def _seed_tenants_and_messages(conn, spec: "list[str]") -> "dict[str, int]":
    """spec is a list of tenant_ids, one archive_messages row created per
    entry (msgid mN). Returns {"idN": generated_id}. Idempotent re-seeding
    of tenants is handled via ON CONFLICT DO NOTHING."""
    conn.execute(
        text(
            "INSERT INTO tenants (id, name, slug) VALUES "
            "('tenant-a','A','tenant-a'), ('tenant-b','B','tenant-b') "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    ids = {}
    for i, tenant_id in enumerate(spec):
        r = conn.execute(
            text(
                "INSERT INTO archive_messages "
                "(msgid, seq, publickey_ver, encrypt_random_key, encrypt_chat_msg, decrypt_status, tenant_id) "
                "VALUES (:m, :s, 1, 'k', 'c', 'success', :t) RETURNING id"
            ),
            {"m": f"m{i}-{tenant_id}", "s": i, "t": tenant_id},
        )
        ids[f"id{i}"] = r.scalar()
    return ids


# ---------------------------------------------------------------------------
# Migration behavior
# ---------------------------------------------------------------------------


def test_clean_0010_to_0011_upgrade():
    db_name = "rnd201_b4_clean_upgrade"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        up_to_0010 = _run_alembic(backend_dir, env, "upgrade", "0010")
        assert up_to_0010.returncode == 0, up_to_0010.stderr
        up_to_0011 = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert up_to_0011.returncode == 0, up_to_0011.stderr

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = 'message_revocations'::regclass"
                    )
                ).fetchall()
            }
        engine.dispose()
        assert "uq_message_revocations_revoke_event_message_id" in names
        assert "fk_message_revocations_tenant_revoke_event" in names
        assert "fk_message_revocations_tenant_original" in names
        assert "uq_message_revocations_tenant_revoke_event" not in names
    finally:
        _drop_database(db_name)


def test_full_base_to_0011_upgrade():
    db_name = "rnd201_b4_base_upgrade"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode == 0, result.stderr

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()
        assert current == "0011"
    finally:
        _drop_database(db_name)


def test_populated_0010_to_0011_preserves_valid_data():
    db_name = "rnd201_b4_populated_upgrade"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a", "tenant-a"])
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                        "VALUES ('tenant-a', :id0, 'ev-valid', 'linked', :id1)"
                    ),
                    ids,
                )
        engine.dispose()

        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode == 0, result.stderr

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT tenant_id, status, original_message_id FROM message_revocations WHERE revoke_event_msgid = 'ev-valid'")
            ).fetchone()
        engine.dispose()
        assert row.tenant_id == "tenant-a"
        assert row.status == "linked"
        assert row.original_message_id == ids["id1"]
    finally:
        _drop_database(db_name)


def test_downgrade_0011_to_0010():
    db_name = "rnd201_b4_downgrade"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0011").returncode == 0

        result = _run_alembic(backend_dir, env, "downgrade", "0010")
        assert result.returncode == 0, result.stderr

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            names = {
                r[0]
                for r in conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = 'message_revocations'::regclass")
                ).fetchall()
            }
            archive_names = {
                r[0]
                for r in conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = 'archive_messages'::regclass")
                ).fetchall()
            }
        engine.dispose()
        assert "uq_message_revocations_tenant_revoke_event" in names
        assert "message_revocations_revoke_event_message_id_fkey" in names
        assert "message_revocations_original_message_id_fkey" in names
        assert "uq_message_revocations_revoke_event_message_id" not in names
        assert "fk_message_revocations_tenant_revoke_event" not in names
        assert "fk_message_revocations_tenant_original" not in names
        assert "uq_archive_messages_tenant_id_id" not in archive_names
        # 0010's own CHECK constraints must survive the downgrade untouched.
        assert "ck_message_revocations_status_valid" in names
        assert "ck_message_revocations_linked_consistency" in names
    finally:
        _drop_database(db_name)


def test_downgrade_then_reupgrade_round_trips_cleanly():
    db_name = "rnd201_b4_roundtrip"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0011").returncode == 0
        assert _run_alembic(backend_dir, env, "downgrade", "0010").returncode == 0
        reup = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert reup.returncode == 0, reup.stderr

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            names = {
                r[0]
                for r in conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conrelid = 'message_revocations'::regclass")
                ).fetchall()
            }
        engine.dispose()
        assert current == "0011"
        assert "uq_message_revocations_revoke_event_message_id" in names
    finally:
        _drop_database(db_name)


def test_downgrade_does_not_delete_application_data():
    db_name = "rnd201_b4_downgrade_preserves_data"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0011").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a"])
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
                        "VALUES ('tenant-a', :id0, 'ev-survives-downgrade', 'pending')"
                    ),
                    ids,
                )
        engine.dispose()

        assert _run_alembic(backend_dir, env, "downgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT tenant_id, status FROM message_revocations WHERE revoke_event_msgid = 'ev-survives-downgrade'")
            ).fetchone()
        engine.dispose()
        assert row is not None
        assert row.tenant_id == "tenant-a"
        assert row.status == "pending"
    finally:
        _drop_database(db_name)


def test_deterministic_null_tenant_recovery_during_upgrade():
    db_name = "rnd201_b4_null_recovery"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a"])
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
                        "VALUES (NULL, :id0, 'ev-null-tenant', 'pending')"
                    ),
                    ids,
                )
        engine.dispose()

        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode == 0, result.stderr

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT tenant_id FROM message_revocations WHERE revoke_event_msgid = 'ev-null-tenant'")
            ).fetchone()
        engine.dispose()
        # Recovered from the revoke event's OWN archive_messages row's
        # tenant -- not defaulted, not guessed.
        assert row.tenant_id == "tenant-a"
    finally:
        _drop_database(db_name)


def test_upgrade_rejects_unrecoverable_null_tenant():
    """A NULL-tenant row whose revoke_event_message_id has no matching
    archive_messages row at all cannot be recovered -- the migration
    must abort (not silently default a tenant)."""
    db_name = "rnd201_b4_unrecoverable_null"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a"])
                # revoke_event_message_id points at a real archive_messages
                # row for the FK's sake, but we then null out that row's
                # own tenant so recovery has no source to use.
                conn.execute(
                    text("UPDATE archive_messages SET tenant_id = NULL WHERE id = :id0"), ids
                )
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
                        "VALUES (NULL, :id0, 'ev-unrecoverable', 'pending')"
                    ),
                    ids,
                )
        engine.dispose()

        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode != 0
        assert "tenant_id" in result.stderr or "tenant_id" in result.stdout

        # Migration aborted -- still at 0010, data untouched.
        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            row = conn.execute(
                text("SELECT tenant_id FROM message_revocations WHERE revoke_event_msgid = 'ev-unrecoverable'")
            ).fetchone()
        engine.dispose()
        assert current == "0010"
        assert row.tenant_id is None
    finally:
        _drop_database(db_name)


def test_upgrade_rejects_revoke_event_tenant_mismatch():
    db_name = "rnd201_b4_event_mismatch"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-b"])
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
                        "VALUES ('tenant-a', :id0, 'ev-mismatch', 'pending')"
                    ),
                    ids,
                )
        engine.dispose()

        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode != 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()
        assert current == "0010"
    finally:
        _drop_database(db_name)


def test_upgrade_rejects_original_message_tenant_mismatch():
    db_name = "rnd201_b4_original_mismatch"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a", "tenant-b"])
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                        "VALUES ('tenant-a', :id0, 'ev-orig-mismatch', 'linked', :id1)"
                    ),
                    ids,
                )
        engine.dispose()

        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode != 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        engine.dispose()
        assert current == "0010"
    finally:
        _drop_database(db_name)


def test_upgrade_rejects_duplicate_revoke_event_associations():
    """Under migration 0010's schema, the composite UNIQUE(tenant_id,
    revoke_event_message_id) constraint already forbids two rows sharing
    BOTH the same tenant_id and revoke_event_message_id -- so the
    realistic way a genuine duplicate can exist by the time 0011 runs is
    via the NULL-tenant recovery step itself: Postgres's composite
    unique constraint treats NULL as distinct from any real value, so a
    row with tenant_id=NULL and a row with tenant_id='tenant-a' can both
    legally coexist under 0010 for the SAME revoke_event_message_id.
    Once migration 0011's Step 1 recovers the NULL row to 'tenant-a'
    (its revoke event's real tenant), the two rows become a genuine,
    same-tenant duplicate for that event -- exactly what the duplicate
    check (which runs AFTER recovery) exists to catch."""
    db_name = "rnd201_b4_duplicates"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    try:
        assert _run_alembic(backend_dir, env, "upgrade", "0010").returncode == 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            with conn.begin():
                ids = _seed_tenants_and_messages(conn, ["tenant-a"])
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
                        "VALUES ('tenant-a', :id0, 'ev-dup-1', 'pending')"
                    ),
                    ids,
                )
                conn.execute(
                    text(
                        "INSERT INTO message_revocations "
                        "(tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
                        "VALUES (NULL, :id0, 'ev-dup-2', 'pending')"
                    ),
                    ids,
                )
        engine.dispose()

        result = _run_alembic(backend_dir, env, "upgrade", "0011")
        assert result.returncode != 0

        engine = create_engine(target_dsn)
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            count = conn.execute(text("SELECT COUNT(*) FROM message_revocations")).scalar()
        engine.dispose()
        assert current == "0010"
        assert count == 2  # neither duplicate silently discarded
    finally:
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# Invalid PostgreSQL insert probes (post-migration schema)
# ---------------------------------------------------------------------------


@pytest.fixture()
def head_db():
    db_name = "rnd201_b4_insert_probes"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ, DATABASE_URL=target_dsn)
    result = _run_alembic(backend_dir, env, "upgrade", "0011")
    assert result.returncode == 0, result.stderr
    engine = create_engine(target_dsn)
    with engine.connect() as conn:
        with conn.begin():
            ids = _seed_tenants_and_messages(conn, ["tenant-a", "tenant-a", "tenant-a", "tenant-b"])
    yield engine, ids
    engine.dispose()
    _drop_database(db_name)


def _expect_rejected(engine, sql: str, params: dict) -> None:
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(Exception):
                conn.execute(text(sql), params)
        finally:
            trans.rollback()


def _expect_accepted(engine, sql: str, params: dict) -> None:
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text(sql), params)


def test_insert_rejected_tenant_id_null(head_db):
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES (NULL, :id0, 'x', 'pending')",
        ids,
    )


def test_insert_rejected_duplicate_revoke_event_message_id(head_db):
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'first', 'pending')",
        ids,
    )
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'second', 'pending')",
        ids,
    )


def test_insert_rejected_tenant_a_row_referencing_tenant_b_revoke_event(head_db):
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id3, 'x', 'pending')",
        ids,
    )


def test_insert_rejected_tenant_a_row_referencing_tenant_b_original(head_db):
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
        "VALUES ('tenant-a', :id0, 'x', 'linked', :id3)",
        ids,
    )


def test_insert_rejected_linked_with_null_original(head_db):
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
        "VALUES ('tenant-a', :id0, 'x', 'linked', NULL)",
        ids,
    )


def test_insert_rejected_pending_with_non_null_original(head_db):
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
        "VALUES ('tenant-a', :id0, 'x', 'pending', :id1)",
        ids,
    )


def test_insert_rejected_missing_status_treated_as_pending_with_non_null_original(head_db):
    """"missing" is a display-only aging of a persisted "pending" row
    (see app.revoke_reconciliation.display_status) -- there is no
    distinct stored status value for it, so the DB-level test is
    identical in mechanics to the "pending" case above."""
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
        "VALUES ('tenant-a', :id0, 'x', 'pending', :id1)",
        ids,
    )


def test_insert_rejected_malformed_with_non_null_original(head_db):
    engine, ids = head_db
    _expect_rejected(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
        "VALUES ('tenant-a', :id0, 'x', 'malformed', :id1)",
        ids,
    )


# ---------------------------------------------------------------------------
# Valid PostgreSQL insert acceptance
# ---------------------------------------------------------------------------


def test_insert_accepted_same_tenant_pending(head_db):
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'ev-pending', 'pending')",
        ids,
    )


def test_insert_accepted_same_tenant_malformed(head_db):
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'ev-malformed', 'malformed')",
        ids,
    )


def test_insert_accepted_same_tenant_linked(head_db):
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
        "VALUES ('tenant-a', :id0, 'ev-linked', 'linked', :id1)",
        ids,
    )


def test_insert_accepted_same_tenant_pending_ages_to_missing_display_only(head_db):
    """"missing" is display_status()'s aging of "pending" -- the stored
    row is identical to the plain pending case; this test documents that
    mapping rather than exercising separate DB mechanics."""
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'ev-will-age-to-missing', 'pending')",
        ids,
    )


def test_insert_accepted_linked_row_preserves_original_content(head_db):
    """Confirms the composite FK doesn't interfere with reading the
    original message's real content back out -- association integrity
    is enforced without ever touching archive_messages' own columns."""
    engine, ids = head_db
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE archive_messages SET content_text = 'original preserved content' WHERE id = :id1"
                ),
                ids,
            )
            conn.execute(
                text(
                    "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status, original_message_id) "
                    "VALUES ('tenant-a', :id0, 'ev-preserve', 'linked', :id1)"
                ),
                ids,
            )
    with engine.connect() as conn:
        row = conn.execute(text("SELECT content_text FROM archive_messages WHERE id = :id1"), ids).fetchone()
    assert row.content_text == "original preserved content"


def test_insert_accepted_multiple_different_revoke_events_same_tenant(head_db):
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'ev-1', 'pending')",
        ids,
    )
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id1, 'ev-2', 'pending')",
        ids,
    )
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM message_revocations WHERE tenant_id = 'tenant-a'")
        ).scalar()
    assert count == 2


def test_insert_accepted_equivalent_rows_across_different_tenants_different_events(head_db):
    engine, ids = head_db
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-a', :id0, 'ev-tenant-a', 'pending')",
        ids,
    )
    _expect_accepted(
        engine,
        "INSERT INTO message_revocations (tenant_id, revoke_event_message_id, revoke_event_msgid, status) "
        "VALUES ('tenant-b', :id3, 'ev-tenant-b', 'pending')",
        ids,
    )
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM message_revocations")).scalar()
    assert count == 2
