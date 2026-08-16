"""RND-259 acceptance coverage: paid tenant branding, assets, domains and Host gates."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from io import BytesIO
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.db.base import Base
from app.db.models import (
    AdminSession,
    AdminUser,
    AuditLog,
    BillingPlan,
    PlanEntitlement,
    Subscription,
    Tenant,
    TenantBranding,
)
from app.db.session import get_db
from app.routers.branding import router as branding_router
from app.routers.platform import managed_branding_domain_metrics
from app.services.branding import (
    BrandingHostMiddleware,
    configure_domain,
    record_certificate_status,
    utc_now,
)
from app.services.entitlements import CUSTOM_BRANDING, CUSTOM_DOMAIN


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _png(red: int, green: int, blue: int) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), (red, green, blue)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture()
def branding_client() -> Generator[tuple[TestClient, Session, list[tuple[AdminUser, str]]], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Tenant.__table__,
        TenantBranding.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        AdminUser.__table__,
        AdminSession.__table__,
        AuditLog.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    now = utc_now()
    paid_plan = BillingPlan(
        id="plan-paid",
        code="paid-branding",
        display_name="Paid branding",
        is_active=True,
        amount_cents=1,
        currency="CNY",
        billing_period_months=12,
        storage_quota_bytes=1,
    )
    free_plan = BillingPlan(
        id="plan-free",
        code="free",
        display_name="Free",
        is_active=True,
        amount_cents=0,
        currency="CNY",
        billing_period_months=12,
        storage_quota_bytes=1,
    )
    tenants = [
        Tenant(id="tenant-a", name="A", slug="a"),
        Tenant(id="tenant-b", name="B", slug="b"),
        Tenant(id="tenant-c", name="C", slug="c"),
    ]
    users = [
        AdminUser(id="user-a", tenant_id="tenant-a", wecom_user_id="a", role="owner", status="active"),
        AdminUser(id="user-b", tenant_id="tenant-b", wecom_user_id="b", role="owner", status="active"),
        AdminUser(id="user-c", tenant_id="tenant-c", wecom_user_id="c", role="owner", status="active"),
    ]
    db.add_all(
        tenants
        + [paid_plan, free_plan]
        + [
            PlanEntitlement(id="cap-brand", plan_id="plan-paid", capability=CUSTOM_BRANDING, is_enabled=True),
            PlanEntitlement(id="cap-domain", plan_id="plan-paid", capability=CUSTOM_DOMAIN, is_enabled=True),
        ]
        + [
            Subscription(id="sub-a", tenant_id="tenant-a", plan_id="plan-paid", status="active", starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1), source="test", revision=1, renewal_count=0),
            Subscription(id="sub-b", tenant_id="tenant-b", plan_id="plan-paid", status="active", starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1), source="test", revision=1, renewal_count=0),
            Subscription(id="sub-c", tenant_id="tenant-c", plan_id="plan-free", status="active", starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1), source="test", revision=1, renewal_count=0),
        ]
        + users
        + [
            AdminSession(id="session-a", admin_user_id="user-a", tenant_id="tenant-a", wecom_user_id="a", expires_at=now + timedelta(hours=1)),
            AdminSession(id="session-b", admin_user_id="user-b", tenant_id="tenant-b", wecom_user_id="b", expires_at=now + timedelta(hours=1)),
        ]
    )
    db.commit()

    app = FastAPI()
    app.add_middleware(BrandingHostMiddleware)
    app.include_router(branding_router)
    current: list[tuple[AdminUser, str]] = [(users[0], "tenant-a")]

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: current[0]
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db, current
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def test_no_entitlement_is_rejected_by_the_api_not_just_hidden_ui(branding_client) -> None:
    client, _db, current = branding_client
    current[0] = (AdminUser(id="user-c", tenant_id="tenant-c", wecom_user_id="c", role="owner", status="active"), "tenant-c")

    status_response = client.get("/api/branding")
    upload_response = client.put("/api/branding/logo", content=_png(1, 2, 3), headers={"Content-Type": "image/png"})
    domain_response = client.post("/api/branding/domain", json={"hostname": "archive.c.example"})

    assert status_response.status_code == 200
    assert status_response.json()["custom_branding_entitled"] is False
    assert status_response.json()["upgrade_required"] is True
    assert upload_response.status_code == domain_response.status_code == 403
    assert upload_response.json()["detail"] == "custom_branding_upgrade_required"
    assert domain_response.json()["detail"] == "custom_domain_upgrade_required"


def test_logo_and_optional_favicon_are_real_mime_checked_and_tenant_scoped(branding_client) -> None:
    client, db, _current = branding_client
    logo_a = _png(200, 1, 2)
    logo_b = _png(3, 200, 4)

    assert client.put("/api/branding/logo", content=logo_a, headers={"Content-Type": "image/png"}).status_code == 200
    assert client.put("/api/branding/favicon", content=logo_a, headers={"Content-Type": "image/png"}).status_code == 200
    # Declared MIME cannot bypass actual file content or format checks.
    assert client.put("/api/branding/logo", content=logo_a, headers={"Content-Type": "image/jpeg"}).status_code == 422
    assert client.put("/api/branding/favicon", content=b"<svg><script>alert(1)</script></svg>", headers={"Content-Type": "image/svg+xml"}).status_code == 422

    asset_a = client.get("/api/branding/logo", cookies={"session_id": "session-a"})
    favicon_a = client.get("/api/branding/favicon", cookies={"session_id": "session-a"})
    asset_b = client.get("/api/branding/logo", cookies={"session_id": "session-b"})
    assert asset_a.status_code == favicon_a.status_code == asset_b.status_code == 200
    assert asset_a.content != asset_b.content
    assert favicon_a.headers["content-type"].startswith("image/png")
    assert favicon_a.headers["cache-control"] == "private, no-store, max-age=0"
    assert favicon_a.headers["x-content-type-options"] == "nosniff"

    # Configure B directly to prove host-scoped assets never use A's object.
    config_b, _token_b = configure_domain(db, "tenant-b", "archive.b.example.com")
    config_b.domain_state = "active"
    config_b.domain_enabled = True
    config_b.certificate_status = "issued"
    config_b.certificate_expires_at = utc_now() + timedelta(days=30)
    config_b.logo_content = logo_b
    config_b.logo_mime_type = "image/png"
    db.commit()
    custom_b = client.get("/api/branding/logo", headers={"Host": "archive.b.example.com"})
    assert custom_b.status_code == 200
    assert custom_b.content == logo_b
    assert client.get("/api/branding/logo", headers={"Host": "unknown.example"}).status_code == 421


def test_domain_token_is_one_time_hash_only_and_activation_needs_dns_and_tls(branding_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, db, _current = branding_client
    saved = client.post("/api/branding/domain", json={"hostname": "Archive.A.Example.COM"})
    assert saved.status_code == 200
    body = saved.json()
    raw_token = body.pop("verification_token")
    assert body["custom_domain"] == "archive.a.example.com"
    assert body["verification_record_name"] == "_crowntime-verify.archive.a.example.com"
    assert raw_token not in client.get("/api/branding").text
    config = db.query(TenantBranding).filter_by(tenant_id="tenant-a").one()
    assert config.verification_token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token not in str(db.query(AuditLog.detail).all())
    metrics = managed_branding_domain_metrics(db=db)
    assert metrics.pending_verification_count == 1
    assert metrics.pending_certificate_count == 0
    assert metrics.certificate_failure_count == 0

    duplicate = client.post("/api/branding/domain", json={"hostname": "archive.a.example.com"})
    assert duplicate.status_code == 200  # same tenant may intentionally rotate a pending TXT value
    current_token = duplicate.json()["verification_token"]
    db.rollback()
    other_tenant = configure_domain(db, "tenant-b", "archive.b.example.com")
    db.commit()
    assert other_tenant[0].custom_domain == "archive.b.example.com"
    collision = client.post("/api/branding/domain", json={"hostname": "archive.b.example.com"})
    assert collision.status_code == 409

    class DohResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"Answer": [{"data": '"' + current_token + '"'}]}

    monkeypatch.setattr("app.services.branding.httpx.get", lambda *args, **kwargs: DohResponse())
    verified = client.post("/api/branding/domain/verify")
    assert verified.status_code == 200
    assert verified.json()["domain_state"] == "verified"
    assert verified.json()["certificate_status"] == "pending"
    assert client.post("/api/branding/domain/enable").status_code == 409

    record_certificate_status(
        db,
        "tenant-a",
        status="issued",
        expires_at=utc_now() + timedelta(days=30),
    )
    db.commit()
    enabled = client.post("/api/branding/domain/enable")
    assert enabled.status_code == 200
    assert enabled.json()["effective_domain_active"] is True
    assert client.get("/api/branding/logo", headers={"Host": "archive.a.example.com"}).status_code == 200

    # Expiry keeps paid capabilities available during the seven-day grace
    # period; only grace exhaustion stops the custom host. Configuration stays
    # intact and the platform hostname remains a recovery path throughout.
    subscription = db.query(Subscription).filter_by(id="sub-a").one()
    subscription.ends_at = utc_now() - timedelta(seconds=1)
    db.commit()
    assert client.get("/api/branding/logo", headers={"Host": "archive.a.example.com"}).status_code == 200

    subscription.grace_ends_at = utc_now() - timedelta(seconds=1)
    subscription.ends_at = subscription.grace_ends_at - timedelta(days=7)
    subscription.starts_at = subscription.ends_at - timedelta(days=1)
    db.commit()
    assert client.get("/api/branding/logo", headers={"Host": "archive.a.example.com"}).status_code == 421
    assert client.get("/api/branding/logo", headers={"Host": "testserver"}, cookies={"session_id": "session-a"}).status_code == 200


def test_templates_use_host_scoped_favicon_and_optional_fallback() -> None:
    from pathlib import Path

    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    for path in templates.glob("*.html"):
        content = path.read_text(encoding="utf-8")
        if "rel=\"icon\"" in content:
            assert "/api/branding/favicon" in content
            assert "/web/static/brand/favicon" not in content
