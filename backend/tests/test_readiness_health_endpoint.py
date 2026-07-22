"""
Tests for RND-227 P0-C — readiness health endpoints.

Before this ticket, /health returned a static {"status": "ok"} regardless
of database state — a deploy could restart the service ahead of
`alembic upgrade head` and this endpoint would still report 200 while the
app 500s on UndefinedColumn. These tests assert the new endpoints
actually reflect DB/schema state and never leak internals.

/health/live is a true liveness probe (process-only, no DB touch).
/health/ready and /health (kept as an alias, for backward compatibility)
both perform the real DB-connectivity + schema-revision check.

Run (from backend/):
    pytest tests/test_readiness_health_endpoint.py -v
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    # app.db.session caches a module-level engine singleton keyed on
    # first access -- reset it so each test gets its own fresh engine
    # bound to the DATABASE_URL this test set.
    import app.db.session as session_module

    session_module._engine = None
    from app.main import app

    return TestClient(app)


def test_health_live_never_touches_the_database(client, monkeypatch):
    # Point DATABASE_URL at something that would blow up if touched, to
    # prove liveness genuinely never opens a DB connection.
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@127.0.0.1:1/nope")
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_503_when_schema_unmigrated(client):
    # sqlite :memory: has no alembic_version table at all -- the
    # canonical "migration never ran" case this ticket exists to catch.
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_health_alias_matches_health_ready_when_unhealthy(client):
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_health_ready_503_when_database_unreachable(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@127.0.0.1:1/nope")
    import app.db.session as session_module

    session_module._engine = None
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_health_response_never_leaks_exception_text_or_connection_string(client, monkeypatch):
    secret_marker = "supersecretpassword"
    monkeypatch.setenv("DATABASE_URL", f"postgresql://mockuser:{secret_marker}@127.0.0.1:1/nope")
    import app.db.session as session_module

    session_module._engine = None
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert secret_marker not in response.text
    assert "Traceback" not in response.text
    assert "postgresql://" not in response.text


# ---------------------------------------------------------------------------
# Live-Postgres: end-to-end healthy and revision-mismatch cases
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


def _fresh_database(db_name: str) -> str:
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


pytestmark_pg = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable local Postgres for live readiness test"
)


@pytestmark_pg
def test_health_200_when_database_is_migrated_to_head(monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    db_name = "rnd227_health_ready"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr

        monkeypatch.setenv("DATABASE_URL", target_dsn)
        import app.db.session as session_module

        session_module._engine = None
        from app.main import app

        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        _drop_database(db_name)


@pytestmark_pg
def test_health_503_when_database_is_behind_head(monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    db_name = "rnd227_health_behind"
    target_dsn = _fresh_database(db_name)
    backend_dir = Path(__file__).resolve().parent.parent
    try:
        env = dict(os.environ, DATABASE_URL=target_dsn)
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0005"],
            cwd=str(backend_dir), env=env, capture_output=True, text=True, timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr

        monkeypatch.setenv("DATABASE_URL", target_dsn)
        import app.db.session as session_module

        session_module._engine = None
        from app.main import app

        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
    finally:
        _drop_database(db_name)
