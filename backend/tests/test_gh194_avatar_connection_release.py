"""GH-194: avatar storage waits must not hold shared DB pool connections."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, create_engine, insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.db.base import Base
from app.db.models import AdminSession, AdminUser, Contact, Tenant


_SESSION_ID = "synthetic-session"
_TENANT_ID = "synthetic-tenant"
_ADMIN_ID = "synthetic-admin"
_JPEG = b"\xff\xd8\xffsynthetic-jpeg"


class _BlockingStorage:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._release = threading.Event()
        self.refs: list[str] = []

    def read_bytes(self, storage_ref: str) -> bytes:
        with self._condition:
            self.refs.append(storage_ref)
            self._condition.notify_all()
        if not self._release.wait(timeout=10):
            raise TimeoutError("test storage release was not signaled")
        return _JPEG

    def wait_for_reads(self, count: int, timeout: float = 3) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: len(self.refs) >= count, timeout)

    def release(self) -> None:
        self._release.set()


def _make_synthetic_app(engine):
    """Use the real app routes/dependencies with a deliberately partial schema."""
    from app.main import create_app

    alembic_version = Table(
        "alembic_version",
        MetaData(),
        Column("version_num", String(32), primary_key=True),
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            Contact.__table__,
            alembic_version,
        ],
    )
    with engine.begin() as connection:
        connection.execute(
            insert(alembic_version),
            [{"version_num": rev} for rev in _repository_heads()],
        )

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(
            Tenant(
                id=_TENANT_ID,
                name="Synthetic tenant",
                slug="synthetic-tenant",
                lifecycle_status="active",
                lifecycle_revision=1,
            )
        )
        db.add(
            AdminUser(
                id=_ADMIN_ID,
                tenant_id=_TENANT_ID,
                wecom_user_id="synthetic-owner",
                name="Synthetic owner",
                role="owner",
                status="active",
                last_active_at=now,
            )
        )
        db.add(
            AdminSession(
                id=_SESSION_ID,
                admin_user_id=_ADMIN_ID,
                tenant_id=_TENANT_ID,
                wecom_user_id="synthetic-owner",
                expires_at=now + timedelta(hours=1),
                is_revoked=False,
                session_scope="admin",
            )
        )
        for avatar_id in range(1, 4):
            db.add(
                Contact(
                    id=avatar_id,
                    tenant_id=_TENANT_ID,
                    wecom_userid=f"synthetic-staff-{avatar_id}",
                    avatar_storage_backend="local",
                    avatar_storage_ref=f"synthetic/avatar-{avatar_id}.jpg",
                    avatar_content_type="image/jpeg",
                    avatar_status="ready",
                    avatar_synced_at=now,
                )
            )
        db.commit()

    # This regression targets shared archive/admin routes; use the default
    # selfhost surface so its TestClient does not start the cloud telemetry worker.
    return create_app(edition="selfhost")


def _repository_heads() -> frozenset[str]:
    from app.db.schema_check import repository_head_revisions

    return repository_head_revisions()


def test_blocked_avatar_reads_release_pool_for_auth_and_readiness(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six blocked reads coexist with real auth and /health/ready requests.

    The file-backed QueuePool matches the production 3+2 capacity. A synthetic
    alembic_version at repository HEAD gives readiness its healthy schema
    baseline without applying migrations or using any production service.
    """
    import app.db.session as session_module
    from app.routers import avatars as avatar_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'gh194.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=3,
        max_overflow=2,
        pool_timeout=0.25,
    )
    app = _make_synthetic_app(engine)
    storage = _BlockingStorage()
    active_sessions = []
    sessions_lock = threading.Lock()

    class _TrackedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.gh194_closed = False
            with sessions_lock:
                active_sessions.append(self)

        def close(self):
            self.gh194_closed = True
            super().close()

    monkeypatch.setattr(session_module, "Session", _TrackedSession)
    monkeypatch.setattr(session_module, "_engine", engine)
    monkeypatch.setattr(
        avatar_router,
        "get_media_storage_provider_for_backend",
        lambda _backend: storage,
    )

    def request(path: str):
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set("session_id", _SESSION_ID)
            return client.get(path)

    avatar_ids = (1, 2, 1, 3, 2, 1)
    paths = [f"/api/admin/avatars/internal/{avatar_id}" for avatar_id in avatar_ids]
    futures = []
    with ThreadPoolExecutor(max_workers=10) as workers:
        try:
            futures.extend(workers.submit(request, path) for path in paths[:5])
            assert storage.wait_for_reads(5), "five avatar reads did not reach storage"

            # A sixth avatar request must also reach the blocked provider rather
            # than waiting for one of the first five to return a DB connection.
            futures.append(workers.submit(request, paths[5]))
            assert storage.wait_for_reads(6), "the additional avatar read starved"
            assert engine.pool.checkedout() == 0
            with sessions_lock:
                open_request_sessions = [
                    session for session in active_sessions if not session.gh194_closed
                ]
                assert len(open_request_sessions) == 6
                assert all(not session.in_transaction() for session in open_request_sessions)

            protected_future = workers.submit(
                request, "/api/admin/avatars/internal/999"
            )
            auth_future = workers.submit(request, "/api/auth/me")
            ready_future = workers.submit(request, "/health/ready")
            protected_response = protected_future.result(timeout=1.5)
            auth_response = auth_future.result(timeout=1.5)
            ready_response = ready_future.result(timeout=1.5)
            # The absent profile's 404 proves require_role/get_current_user
            # completed successfully rather than timing out as unauthorized.
            assert protected_response.status_code == 404
            assert auth_response.status_code == 200
            assert auth_response.json()["authenticated"] is True
            assert ready_response.status_code == 200
            assert ready_response.json() == {"status": "ok"}
            assert engine.pool.checkedout() == 0
        finally:
            storage.release()
            for future in futures:
                future.result(timeout=3)

    responses = [future.result(timeout=1) for future in futures]
    assert [response.status_code for response in responses] == [200] * 6
    assert [response.content for response in responses] == [_JPEG] * 6
    assert len(storage.refs) == 6
    assert len(set(storage.refs)) == 3
    assert engine.pool.checkedout() == 0
    engine.dispose()
