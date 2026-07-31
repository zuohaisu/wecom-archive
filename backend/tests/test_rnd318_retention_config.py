"""Acceptance coverage for RND-318 tenant retention configuration."""

from __future__ import annotations

from collections.abc import Generator
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, insert, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import get_current_user
from app.db.base import Base
from app.db.models import RetentionConfig, Tenant
from app.db.session import get_db

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0027_retention_config.py"
)


@pytest.fixture()
def retention_client() -> Generator[tuple[TestClient, Session, dict[str, str]], None, None]:
    from app.main import create_app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Tenant.__table__, RetentionConfig.__table__])
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


def test_get_unconfigured_policy_is_not_a_not_found(
    retention_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, auth = retention_client
    auth["role"] = "readonlyaudit"

    response = client.get("/api/admin/retention-config")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "retention_days": None,
        "is_locked": False,
    }


def test_admin_can_create_update_and_read_policy(
    retention_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, _auth = retention_client

    created = client.put(
        "/api/admin/retention-config", json={"retention_days": 365, "lock": False}
    )
    updated = client.put(
        "/api/admin/retention-config", json={"retention_days": 730, "lock": False}
    )
    fetched = client.get("/api/admin/retention-config")

    assert created.status_code == 200
    assert updated.status_code == 200
    assert fetched.json() == {
        "configured": True,
        "retention_days": 730,
        "is_locked": False,
    }


@pytest.mark.parametrize("retention_days", [0, -1, 3651])
def test_retention_days_must_be_within_supported_range(
    retention_client: tuple[TestClient, Session, dict[str, str]], retention_days: int
) -> None:
    client, db, _auth = retention_client

    response = client.put(
        "/api/admin/retention-config",
        json={"retention_days": retention_days, "lock": False},
    )

    assert response.status_code == 422
    assert db.query(RetentionConfig).count() == 0


def test_locked_policy_cannot_be_changed_and_original_value_is_persisted(
    retention_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, db, _auth = retention_client

    assert client.put(
        "/api/admin/retention-config", json={"retention_days": 365, "lock": True}
    ).status_code == 200
    response = client.put(
        "/api/admin/retention-config", json={"retention_days": 730, "lock": False}
    )

    assert response.status_code == 423
    db.expire_all()
    config = db.query(RetentionConfig).filter_by(tenant_id="tenant-a").one()
    assert config.retention_days == 365
    assert config.is_locked is True


def test_tenant_configurations_are_isolated(
    retention_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, auth = retention_client

    assert client.put(
        "/api/admin/retention-config", json={"retention_days": 100, "lock": False}
    ).status_code == 200
    auth["tenant_id"] = "tenant-b"
    assert client.get("/api/admin/retention-config").json()["configured"] is False
    assert client.put(
        "/api/admin/retention-config", json={"retention_days": 200, "lock": False}
    ).status_code == 200
    auth["tenant_id"] = "tenant-a"

    assert client.get("/api/admin/retention-config").json()["retention_days"] == 100


def test_only_admin_or_owner_can_write_while_all_roles_can_read(
    retention_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    client, _db, auth = retention_client
    auth["role"] = "compliance"

    assert client.get("/api/admin/retention-config").status_code == 200
    assert (
        client.put(
            "/api/admin/retention-config", json={"retention_days": 365, "lock": False}
        ).status_code
        == 403
    )

    auth["role"] = "owner"
    assert (
        client.put(
            "/api/admin/retention-config", json={"retention_days": 365, "lock": False}
        ).status_code
        == 200
    )


def test_migration_upgrade_and_downgrade_are_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("rnd318_migration", _MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'retention-migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
    connection = engine.connect()
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

    migration.upgrade()
    columns = {column["name"] for column in inspect(connection).get_columns("retention_configs")}
    assert columns == {
        "id",
        "tenant_id",
        "retention_days",
        "is_locked",
        "created_at",
        "updated_at",
    }

    migration.downgrade()
    assert "retention_configs" not in inspect(connection).get_table_names()
    connection.close()


def test_database_constraints_enforce_one_valid_policy_per_tenant(
    retention_client: tuple[TestClient, Session, dict[str, str]],
) -> None:
    _client, db, _auth = retention_client
    db.execute(
        insert(RetentionConfig.__table__).values(
            id="retention-a", tenant_id="tenant-a", retention_days=365, is_locked=False
        )
    )
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(
            insert(RetentionConfig.__table__).values(
                id="retention-a-duplicate",
                tenant_id="tenant-a",
                retention_days=365,
                is_locked=False,
            )
        )
        db.commit()
    db.rollback()

    with pytest.raises(IntegrityError):
        db.execute(
            insert(RetentionConfig.__table__).values(
                id="retention-invalid", tenant_id="tenant-b", retention_days=3651, is_locked=False
            )
        )
        db.commit()
    db.rollback()
