"""RND-371 avatar cache, isolation, and fail-safe contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import AdminUser, Contact, ExternalContact
from app.services import avatar_sync
from app.wecom_contacts import MemberProfile


class _MemoryStorage:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def save_bytes(self, storage_ref: str, data: bytes) -> str:
        self.data[storage_ref] = data
        return storage_ref

    def read_bytes(self, storage_ref: str) -> bytes:
        if storage_ref not in self.data:
            raise FileNotFoundError()
        return self.data[storage_ref]

    def delete(self, storage_ref: str) -> bool:
        self.deleted.append(storage_ref)
        self.data.pop(storage_ref, None)
        return True


@pytest.fixture()
def avatar_db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Contact.__table__.create(engine)
    ExternalContact.__table__.create(engine)
    AdminUser.__table__.create(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture()
def storage(monkeypatch: pytest.MonkeyPatch) -> _MemoryStorage:
    value = _MemoryStorage()
    monkeypatch.setattr(avatar_sync, "get_configured_write_backend_name", lambda: "local")
    monkeypatch.setattr(avatar_sync, "get_media_storage_provider", lambda _backend: value)
    return value


def test_external_avatar_is_cached_under_a_stable_tenant_key_and_never_returns_source_url(
    avatar_db: Session, storage: _MemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = ExternalContact(tenant_id="tenant-a", external_userid="wm-customer")
    avatar_db.add(contact)
    avatar_db.flush()
    source_url = "https://wx.qlogo.cn/mmhead/example/0"
    monkeypatch.setattr(
        avatar_sync,
        "_download_avatar",
        lambda value: (b"\xff\xd8\xffavatar", ".jpg", "image/jpeg") if value == source_url else None,
    )

    assert avatar_sync.sync_external_contact_avatar(
        contact, "tenant-a", {"external_contact": {"avatar": source_url}}
    )
    first_ref = contact.avatar_storage_ref
    first_synced = contact.avatar_synced_at
    assert first_ref == "tenants/tenant-a/avatars/external-wm-customer.jpg"
    assert contact.avatar_status == "ready"
    assert source_url not in (contact.avatar_storage_ref or "")
    assert source_url not in (contact.avatar_source or "")

    # Repeated provider detail events are idempotent: same identity/key, no
    # duplicate storage object or parallel contact identity.
    assert avatar_sync.sync_external_contact_avatar(
        contact, "tenant-a", {"external_contact": {"avatar": source_url}}
    )
    assert contact.avatar_storage_ref == first_ref
    assert len(storage.data) == 1
    presentation = avatar_sync.external_avatar_presentation(contact)
    assert presentation.url == f"/api/admin/avatars/external/{contact.id}"
    assert source_url not in (presentation.url or "")
    assert presentation.synced_at >= first_synced


def test_invalid_or_untrusted_avatar_source_clears_cache_without_network(
    avatar_db: Session, storage: _MemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = Contact(
        tenant_id="tenant-a",
        wecom_userid="staff-a",
        avatar_storage_backend="local",
        avatar_storage_ref="tenants/tenant-a/avatars/internal-staff-a.jpg",
        avatar_content_type="image/jpeg",
        avatar_status="ready",
    )
    avatar_db.add(contact)
    avatar_db.flush()
    storage.data[contact.avatar_storage_ref] = b"old"
    monkeypatch.setattr(
        avatar_sync,
        "_download_avatar",
        lambda _value: pytest.fail("untrusted URL must not be fetched"),
    )

    assert not avatar_sync.sync_avatar_from_source(
        contact,
        tenant_id="tenant-a",
        identity_kind="internal",
        stable_identity_id="staff-a",
        source="wecom_member",
        source_url="https://localhost/latest/meta-data",
    )
    assert contact.avatar_status == "invalid"
    assert contact.avatar_storage_ref is None
    assert storage.deleted == ["tenants/tenant-a/avatars/internal-staff-a.jpg"]


def test_inactive_member_is_never_exposed_as_an_internal_avatar(
    avatar_db: Session, storage: _MemoryStorage
) -> None:
    assert not avatar_sync.sync_internal_contact_avatar(
        avatar_db,
        "tenant-a",
        "staff-left",
        MemberProfile(name="Former employee", avatar_url="https://wx.qlogo.cn/old", active=False),
    )
    contact = avatar_db.query(Contact).one()
    assert contact.avatar_status == "inactive"
    assert avatar_sync.internal_avatar_presentations(
        avatar_db, "tenant-a", {"staff-left"}
    )["staff-left"].url is None


def test_avatar_endpoint_fails_closed_for_cross_tenant_and_disabled_users(
    avatar_db: Session, storage: _MemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app
    from app.routers import avatars as avatar_router

    monkeypatch.setattr(
        avatar_router, "get_media_storage_provider_for_backend", lambda _backend: storage
    )
    source = Contact(
        tenant_id="tenant-a",
        wecom_userid="staff-a",
        avatar_storage_backend="local",
        avatar_storage_ref="tenants/tenant-a/avatars/internal-staff-a.jpg",
        avatar_content_type="image/jpeg",
        avatar_status="ready",
        avatar_synced_at=datetime.now(timezone.utc),
    )
    other = ExternalContact(
        tenant_id="tenant-b",
        external_userid="wm-other",
        avatar_storage_backend="local",
        avatar_storage_ref="tenants/tenant-b/avatars/external-wm-other.jpg",
        avatar_content_type="image/jpeg",
        avatar_status="ready",
        avatar_synced_at=datetime.now(timezone.utc),
    )
    avatar_db.add_all([source, other])
    avatar_db.flush()
    storage.data[source.avatar_storage_ref] = b"\xff\xd8\xffimage-a"
    storage.data[other.avatar_storage_ref] = b"\xff\xd8\xffimage-b"

    def override_db():
        yield avatar_db

    app.dependency_overrides[get_current_user] = lambda: (SimpleNamespace(role="admin"), "tenant-a")
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            good = client.get(f"/api/admin/avatars/internal/{source.id}")
            assert good.status_code == 200
            assert good.content == b"\xff\xd8\xffimage-a"
            assert good.headers["cache-control"] == "private, no-cache, max-age=0"
            assert (
                client.get(
                    f"/api/admin/avatars/internal/{source.id}",
                    headers={"If-None-Match": good.headers["etag"]},
                ).status_code
                == 304
            )
            assert client.get(f"/api/admin/avatars/external/{other.id}").status_code == 404

            avatar_db.add(
                AdminUser(
                    id="disabled-user",
                    tenant_id="tenant-a",
                    wecom_user_id="staff-a",
                    role="admin",
                    status="disabled",
                )
            )
            avatar_db.commit()
            assert client.get(f"/api/admin/avatars/internal/{source.id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_member_profile_reads_avatar_but_not_a_browser_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def json(self):
            return {
                "errcode": 0,
                "name": "Alice",
                "avatar": "https://wx.qlogo.cn/mmhead/alice/0",
                "status": 1,
            }

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr("app.wecom_contacts.httpx.Client", lambda **_kwargs: _Client())
    profile = __import__("app.wecom_contacts", fromlist=["fetch_member_profile"]).fetch_member_profile(
        "token", "staff-a"
    )
    assert profile == MemberProfile(
        name="Alice", avatar_url="https://wx.qlogo.cn/mmhead/alice/0", active=True
    )


def test_upgrade_wecom_http_only_touches_allowlisted_hosts() -> None:
    # WeCom's externalcontact/get returns external avatar URLs over plain
    # http on the same CDN; only those allow-listed hosts may be upgraded.
    assert (
        avatar_sync._upgrade_wecom_http("http://wx.qlogo.cn/mmhead/a/0")
        == "https://wx.qlogo.cn/mmhead/a/0"
    )
    assert (
        avatar_sync._upgrade_wecom_http("http://qlogo.cn/mmhead/a/0")
        == "https://qlogo.cn/mmhead/a/0"
    )
    assert (
        avatar_sync._upgrade_wecom_http("http://wework.qpic.cn/a/0")
        == "https://wework.qpic.cn/a/0"
    )
    # Already-https URLs and any non-allow-listed host stay untouched.
    assert (
        avatar_sync._upgrade_wecom_http("https://wx.qlogo.cn/mmhead/a/0")
        == "https://wx.qlogo.cn/mmhead/a/0"
    )
    assert (
        avatar_sync._upgrade_wecom_http("http://example.com/avatar.png")
        == "http://example.com/avatar.png"
    )
    assert (
        avatar_sync._upgrade_wecom_http("http://evil-qlogo.cn/avatar.png")
        == "http://evil-qlogo.cn/avatar.png"
    )
    assert (
        avatar_sync._upgrade_wecom_http("http://qlogo.cn.evil.com/avatar.png")
        == "http://qlogo.cn.evil.com/avatar.png"
    )


def test_external_http_avatar_from_wecom_cdn_is_upgraded_to_https(
    avatar_db: Session, storage: _MemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = ExternalContact(tenant_id="tenant-a", external_userid="wm-http")
    avatar_db.add(contact)
    avatar_db.flush()
    seen: list[str] = []
    source_url = "http://wx.qlogo.cn/mmhead/example/0"
    monkeypatch.setattr(
        avatar_sync,
        "_download_avatar",
        lambda value: (
            seen.append(value),
            (b"\xff\xd8\xffavatar", ".jpg", "image/jpeg"),
        )[1],
    )

    assert avatar_sync.sync_external_contact_avatar(
        contact, "tenant-a", {"external_contact": {"avatar": source_url}}
    )
    assert seen == ["https://wx.qlogo.cn/mmhead/example/0"]
    assert contact.avatar_status == "ready"
    assert contact.avatar_storage_ref == "tenants/tenant-a/avatars/external-wm-http.jpg"


def test_http_avatar_from_untrusted_host_stays_invalid_without_network(
    avatar_db: Session, storage: _MemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact = Contact(
        tenant_id="tenant-a",
        wecom_userid="staff-a",
        avatar_storage_backend="local",
        avatar_storage_ref="tenants/tenant-a/avatars/internal-staff-a.jpg",
        avatar_content_type="image/jpeg",
        avatar_status="ready",
    )
    avatar_db.add(contact)
    avatar_db.flush()
    storage.data[contact.avatar_storage_ref] = b"old"
    monkeypatch.setattr(
        avatar_sync,
        "_download_avatar",
        lambda _value: pytest.fail("untrusted URL must not be fetched"),
    )

    assert not avatar_sync.sync_avatar_from_source(
        contact,
        tenant_id="tenant-a",
        identity_kind="internal",
        stable_identity_id="staff-a",
        source="wecom_member",
        source_url="http://example.com/avatar.png",
    )
    assert contact.avatar_status == "invalid"
    assert contact.avatar_storage_ref is None
    assert storage.deleted == ["tenants/tenant-a/avatars/internal-staff-a.jpg"]
