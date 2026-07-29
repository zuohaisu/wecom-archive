"""RND-292: media-library downloads are tenant-scoped and audit-logged."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.db.models import AuditLog, MediaFile
from tests.test_reachability_audit import (
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,  # noqa: F401 - imported fixture
)


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db.session import get_db

    db.execute(
        text(
            """CREATE TABLE audit_logs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                admin_user_id TEXT, action TEXT NOT NULL,
                object_type TEXT NOT NULL, object_id TEXT,
                detail JSON, created_at TEXT NOT NULL
            )"""
        )
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _authed(tenant_id: str, user_id: str = "admin-a") -> None:
    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: (
        MagicMock(id=user_id, role="admin"),
        tenant_id,
    )


def _insert_media(db, message_id: int, tenant_id: str, **kwargs) -> MediaFile:
    media = MediaFile(
        tenant_id=tenant_id,
        archive_message_id=message_id,
        sdkfileid=kwargs.pop("sdkfileid", f"sdk-{message_id}"),
        file_type=kwargs.pop("file_type", "image"),
        download_status=kwargs.pop("download_status", "downloaded"),
        **kwargs,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def test_download_returns_attachment_and_appends_one_audit_row(client, db, monkeypatch, tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    image = media_root / "photo.jpg"
    image.write_bytes(b"jpeg-download")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    message = _insert_message(
        db, msgid="download-a", msgtype="image", roomid="room-a", tenant_id=_TENANT_A
    )
    media = _insert_media(db, message.id, _TENANT_A, local_path=str(image))
    _authed(_TENANT_A, "admin-download")

    first = client.get(f"/api/admin/media/{media.id}/download")
    second = client.get(f"/api/admin/media/{media.id}/download")

    assert first.status_code == second.status_code == 200
    assert first.content == b"jpeg-download"
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["content-disposition"] == f'attachment; filename="media-{media.id}.jpg"'
    rows = db.query(AuditLog).filter(AuditLog.action == "media.download").all()
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {_TENANT_A}
    assert {row.admin_user_id for row in rows} == {"admin-download"}
    assert {row.object_id for row in rows} == {str(media.id)}
    assert all(row.object_type == "media_file" and row.created_at is not None for row in rows)


def test_missing_or_cross_tenant_download_does_not_write_success_audit(client, db, monkeypatch, tmp_path):
    media_root = tmp_path / "media"
    media_root.mkdir()
    image = media_root / "tenant-b.jpg"
    image.write_bytes(b"tenant-b-bytes")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(media_root))
    message = _insert_message(
        db, msgid="download-b", msgtype="image", roomid="room-b", tenant_id=_TENANT_B
    )
    media = _insert_media(db, message.id, _TENANT_B, local_path=str(image))
    _authed(_TENANT_A)

    assert client.get(f"/api/admin/media/{media.id}/download").status_code == 404
    assert client.get("/api/admin/media/999999/download").status_code == 404
    assert db.query(AuditLog).filter(AuditLog.action == "media.download").count() == 0


def test_unauthenticated_download_is_rejected_without_audit(client, db):
    assert client.get("/api/admin/media/1/download").status_code == 401
    assert db.query(AuditLog).count() == 0


def test_qiniu_download_is_proxied_and_does_not_expose_provider_credentials(client, db, monkeypatch):
    import app.services.media_access as media_access

    message = _insert_message(
        db, msgid="download-qiniu", msgtype="image", roomid="room-a", tenant_id=_TENANT_A
    )
    media = _insert_media(
        db,
        message.id,
        _TENANT_A,
        storage_backend="qiniu_kodo",
        storage_ref="tenants/tenant-a/images/1.jpg",
        local_path=None,
    )

    class FakeQiniuProvider:
        def supports_local_path(self):
            return False

        def read_bytes(self, storage_ref):
            assert storage_ref == "tenants/tenant-a/images/1.jpg"
            return b"qiniu-proxied-bytes"

    monkeypatch.setattr(media_access, "resolve_downloadable_media_file_state", lambda *_: "servable")
    monkeypatch.setattr(media_access, "get_media_storage_provider", lambda _: FakeQiniuProvider())
    _authed(_TENANT_A)

    response = client.get(f"/api/admin/media/{media.id}/download")

    assert response.status_code == 200
    assert response.content == b"qiniu-proxied-bytes"
    assert response.headers["content-disposition"] == f'attachment; filename="media-{media.id}.jpg"'
    joined_response = response.text + "\n" + "\n".join(response.headers.values())
    assert "access-key" not in joined_response.lower()
    assert "secret-key" not in joined_response.lower()
    audit = db.query(AuditLog).filter(AuditLog.action == "media.download").one()
    assert audit.tenant_id == _TENANT_A
    assert audit.object_id == str(media.id)
