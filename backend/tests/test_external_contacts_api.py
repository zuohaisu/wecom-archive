"""Contracts for the tenant-scoped external-contact list API (RND-288)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Contact, ExternalContact


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Contact.__table__.create(engine)
    ExternalContact.__table__.create(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture()
def client(db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[get_current_user] = lambda: (
        SimpleNamespace(role="admin"),
        "tenant-a",
    )
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _external_contact(
    db: Session,
    external_userid: str,
    *,
    tenant_id: str = "tenant-a",
    company: str | None = None,
    tags: list[str] | None = None,
    owner_wecom_userid: str | None = None,
    last_interaction_at: datetime | None = None,
) -> None:
    db.add(
        ExternalContact(
            tenant_id=tenant_id,
            external_userid=external_userid,
            name=f"Name {external_userid}",
            company=company,
            tags=json.dumps(tags or [], ensure_ascii=False),
            source="campaign",
            owner_wecom_userid=owner_wecom_userid,
            last_interaction_at=last_interaction_at,
            message_count=7,
        )
    )
    db.commit()


def test_list_filters_exact_tags_and_resolves_owner_display_names(client: TestClient, db: Session) -> None:
    db.add(Contact(tenant_id="tenant-a", wecom_userid="staff-zhang", name="张三"))
    db.commit()
    _external_contact(
        db,
        "external-vip",
        company="Acme Ltd",
        tags=["VIP", "重点客户"],
        owner_wecom_userid="staff-zhang",
    )
    _external_contact(
        db,
        "external-vip2026",
        company="Other Corp",
        tags=["VIP2026"],
        owner_wecom_userid="staff-missing",
    )
    _external_contact(
        db,
        "external-basic",
        company="Acme Services",
        tags=["普通"],
        owner_wecom_userid="staff-zhang",
    )

    response = client.get("/api/admin/external-contacts")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    items = {item["external_userid"]: item for item in payload["items"]}
    assert items["external-vip"]["tags"] == ["VIP", "重点客户"]
    assert items["external-vip"]["owner_display_name"] == "张三"
    assert items["external-vip"]["owner_display_name"] != "tenant-a"
    # A missing Contact name must fall back to the raw owner ID, not tenant_id.
    assert items["external-vip2026"]["owner_display_name"] == "staff-missing"
    assert items["external-vip2026"]["owner_display_name"] != "tenant-a"

    assert {
        item["external_userid"]
        for item in client.get("/api/admin/external-contacts?company=Acme").json()["items"]
    } == {"external-vip", "external-basic"}
    assert [
        item["external_userid"]
        for item in client.get("/api/admin/external-contacts?tags=VIP").json()["items"]
    ] == ["external-vip"]
    assert [
        item["external_userid"]
        for item in client.get(
            "/api/admin/external-contacts?owner_wecom_userid=staff-missing"
        ).json()["items"]
    ] == ["external-vip2026"]


def test_pagination_and_tenant_isolation(client: TestClient, db: Session) -> None:
    _external_contact(
        db,
        "external-newest",
        last_interaction_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    _external_contact(
        db,
        "external-older",
        last_interaction_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _external_contact(db, "external-other-tenant", tenant_id="tenant-b")

    first_page = client.get("/api/admin/external-contacts?offset=0&limit=1").json()
    assert first_page["total"] == 2
    assert first_page["has_more"] is True
    assert [item["external_userid"] for item in first_page["items"]] == ["external-newest"]

    second_page = client.get("/api/admin/external-contacts?offset=1&limit=1").json()
    assert second_page["has_more"] is False
    assert [item["external_userid"] for item in second_page["items"]] == ["external-older"]
    assert client.get("/api/admin/external-contacts?offset=99").json()["items"] == []
    assert "external-other-tenant" not in {
        item["external_userid"]
        for item in client.get("/api/admin/external-contacts").json()["items"]
    }
