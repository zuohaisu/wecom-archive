"""Acceptance coverage for RND-304 tenant onboarding completion status."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import get_current_user
from app.db.base import Base
from app.db.models import Tenant
from app.db.session import get_db

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0029_tenant_onboarding_completed.py"
)


@pytest.fixture()
def onboarding_client() -> Generator[tuple[TestClient, Session, dict[str, str]], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Tenant.__table__])
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    db.add_all(
        [
            Tenant(id="tenant-a", name="Tenant A", slug="tenant-a"),
            Tenant(id="tenant-b", name="Tenant B", slug="tenant-b"),
        ]
    )
    db.commit()

    auth = {"role": "admin", "tenant_id": "tenant-a"}
    from app.main import create_app

    app = create_app()

    def override_current_user():
        return SimpleNamespace(role=auth["role"]), auth["tenant_id"]

    def override_db() -> Generator[Session, None, None]:
        request_db = session_factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db, auth
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def test_status_defaults_to_first_run_for_all_authenticated_roles(
    onboarding_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, auth = onboarding_client
    auth["role"] = "compliance"

    response = client.get("/api/onboarding/status")

    assert response.status_code == 200
    assert response.json() == {"first_run": True}


def test_complete_marks_onboarding_done_and_preserves_first_timestamp(
    onboarding_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, db, _auth = onboarding_client

    first = client.post("/api/onboarding/complete")
    db.expire_all()
    first_completed_at = db.get(Tenant, "tenant-a").onboarding_completed_at
    assert first.status_code == 200
    assert first.json() == {"first_run": False}
    assert first_completed_at is not None

    with patch("app.routers.onboarding.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = first_completed_at + timedelta(days=1)
        second = client.post("/api/onboarding/complete")

    db.expire_all()
    assert second.status_code == 200
    assert second.json() == {"first_run": False}
    assert db.get(Tenant, "tenant-a").onboarding_completed_at == first_completed_at
    assert client.get("/api/onboarding/status").json() == {"first_run": False}


def test_completion_is_scoped_to_authenticated_tenant_only(
    onboarding_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, auth = onboarding_client

    assert client.post("/api/onboarding/complete?tenant_id=tenant-b").status_code == 200
    auth["tenant_id"] = "tenant-b"

    assert client.get("/api/onboarding/status?tenant_id=tenant-a").json() == {"first_run": True}


def test_only_admin_or_owner_can_complete_onboarding(
    onboarding_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, auth = onboarding_client
    auth["role"] = "readonlyaudit"

    assert client.post("/api/onboarding/complete").status_code == 403
    auth["role"] = "owner"
    assert client.post("/api/onboarding/complete").status_code == 200


def test_migration_upgrade_and_downgrade_are_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("rnd304_migration", _MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'onboarding-migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
    connection = engine.connect()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

    migration.upgrade()
    columns = {column["name"] for column in inspect(connection).get_columns("tenants")}
    assert "onboarding_completed_at" in columns

    migration.downgrade()
    columns = {column["name"] for column in inspect(connection).get_columns("tenants")}
    assert "onboarding_completed_at" not in columns
    connection.close()
