"""Offline API coverage for RND-286 admin user lifecycle endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user, hash_password
from app.db.base import Base
from app.db.models import (
    AdminAccessRequest,
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    PasswordResetToken,
    Tenant,
)
from app.db.session import get_db


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AdminUser.__table__,
            AdminSession.__table__,
            PasswordResetToken.__table__,
            AdminAccessRequest.__table__,
            AdminLoginIdentity.__table__,
        ],
    )
    session = Session(engine)
    for tenant_id, slug in (("tenant-a", "default"), ("tenant-b", "tenant-b")):
        session.add(Tenant(id=tenant_id, slug=slug, name=tenant_id))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def admin(db: Session) -> AdminUser:
    user = AdminUser(
        id="admin-a",
        tenant_id="tenant-a",
        wecom_user_id="admin-a",
        name="Admin A",
        email="admin-a@example.test",
        role="admin",
        status="active",
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def client(db: Session, admin: AdminUser):
    from app.main import app

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: (admin, admin.tenant_id)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _add_user(
    db: Session,
    *,
    user_id: str | None = None,
    tenant_id: str = "tenant-a",
    email: str | None = "user@example.test",
    role: str = "admin",
    status: str = "active",
    password: str | None = None,
) -> AdminUser:
    user = AdminUser(
        id=user_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        wecom_user_id=f"wecom-{uuid.uuid4()}",
        name="Target User",
        email=email,
        password_hash=hash_password(password) if password else None,
        role=role,
        status=status,
    )
    db.add(user)
    db.commit()
    return user


def test_patch_status_toggles_user_and_disabled_user_cannot_password_login(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _add_user(db, email="target@example.test", password="correct-password")

    disabled = client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("ADMIN_USERNAME", "bootstrap-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("bootstrap-password"))
    rejected_login = client.post(
        "/api/auth/password/login",
        json={"username": target.email, "password": "correct-password"},
    )
    assert rejected_login.status_code == 401

    enabled = client.patch(f"/api/admin/users/{target.id}", json={"status": "active"})
    assert enabled.status_code == 200
    assert enabled.json() == {
        "id": target.id,
        "email": target.email,
        "name": target.name,
        "role": "admin",
        "status": "active",
    }


def test_status_transition_audits_actor_target_and_skips_noop(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock
    import app.routers.users as users_module

    target = _add_user(db)
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    assert client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"}).status_code == 200
    call = audit_writer.call_args.kwargs
    assert call["action"] == "user.disabled"
    assert call["tenant_id"] == admin.tenant_id
    assert call["admin_user_id"] == admin.id
    assert call["object_id"] == target.id

    audit_writer.reset_mock()
    assert client.patch(f"/api/admin/users/{target.id}", json={"status": "disabled"}).status_code == 200
    audit_writer.assert_not_called()

    assert client.patch(f"/api/admin/users/{target.id}", json={"status": "active"}).status_code == 200
    assert audit_writer.call_args.kwargs["action"] == "user.enabled"


def test_patch_rejects_invalid_status_cross_tenant_missing_and_self_disable(
    client: TestClient, db: Session, admin: AdminUser
) -> None:
    other_tenant = _add_user(db, tenant_id="tenant-b")

    invalid = client.patch(f"/api/admin/users/{admin.id}", json={"status": "foo"})
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid_status"}

    cross_tenant = client.patch(
        f"/api/admin/users/{other_tenant.id}", json={"status": "disabled"}
    )
    missing = client.patch("/api/admin/users/missing", json={"status": "disabled"})
    assert cross_tenant.status_code == missing.status_code == 404
    assert cross_tenant.json() == missing.json() == {"detail": "User not found"}

    self_disable = client.patch(f"/api/admin/users/{admin.id}", json={"status": "disabled"})
    self_enable = client.patch(f"/api/admin/users/{admin.id}", json={"status": "active"})
    assert self_disable.status_code == 400
    assert self_disable.json() == {"detail": "cannot_disable_self"}
    assert self_enable.status_code == 200


def test_patch_role_changes_a_tenant_user_and_audits_actor_target(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock
    import app.routers.users as users_module

    target = _add_user(db, role="readonlyaudit")
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    changed = client.patch(
        f"/api/admin/users/{target.id}/role", json={"role": "compliance"}
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "compliance"
    call = audit_writer.call_args.kwargs
    assert call["action"] == "user.role_changed"
    assert call["tenant_id"] == admin.tenant_id
    assert call["admin_user_id"] == admin.id
    assert call["object_id"] == target.id
    assert call["detail"] == {"previous_role": "readonlyaudit", "new_role": "compliance"}

    audit_writer.reset_mock()
    unchanged = client.patch(
        f"/api/admin/users/{target.id}/role", json={"role": "compliance"}
    )
    assert unchanged.status_code == 200
    audit_writer.assert_not_called()


def test_role_update_rejects_invalid_cross_tenant_and_self_changes(
    client: TestClient, db: Session, admin: AdminUser
) -> None:
    target = _add_user(db)
    other_tenant = _add_user(db, tenant_id="tenant-b")

    invalid = client.patch(f"/api/admin/users/{target.id}/role", json={"role": "unknown"})
    cross_tenant = client.patch(f"/api/admin/users/{other_tenant.id}/role", json={"role": "legal"})
    self_change = client.patch(f"/api/admin/users/{admin.id}/role", json={"role": "legal"})

    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid_role"}
    assert cross_tenant.status_code == 404
    assert cross_tenant.json() == {"detail": "User not found"}
    assert self_change.status_code == 400
    assert self_change.json() == {"detail": "cannot_change_own_role"}


def test_only_owner_can_manage_owner_role_or_assign_owner(
    client: TestClient, db: Session, admin: AdminUser
) -> None:
    owner = _add_user(db, role="owner")
    target = _add_user(db, role="legal")

    cannot_change_owner = client.patch(
        f"/api/admin/users/{owner.id}/role", json={"role": "admin"}
    )
    cannot_assign_owner = client.patch(
        f"/api/admin/users/{target.id}/role", json={"role": "owner"}
    )
    cannot_disable_owner = client.patch(
        f"/api/admin/users/{owner.id}", json={"status": "disabled"}
    )
    assert cannot_change_owner.status_code == 403
    assert cannot_assign_owner.status_code == 403
    assert cannot_disable_owner.status_code == 403

    admin.role = "owner"
    db.commit()
    changed = client.patch(f"/api/admin/users/{target.id}/role", json={"role": "owner"})
    assert changed.status_code == 200
    assert changed.json()["role"] == "owner"


def test_reset_password_creates_token_and_sends_email(client: TestClient, db: Session) -> None:
    target = _add_user(db, email="reset@example.test")

    with patch("app.routers.users.send_password_reset_email", return_value=True) as send_email:
        response = client.post(f"/api/admin/users/{target.id}/reset-password")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    token = db.query(PasswordResetToken).filter_by(admin_user_id=target.id).one()
    assert token.used is False
    assert token.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    send_email.assert_called_once()
    assert target.email == send_email.call_args.args[0]
    assert "token=" in send_email.call_args.args[1]
    assert token.token not in response.text


def test_reset_password_rejects_ineligible_or_cross_tenant_user(
    client: TestClient, db: Session
) -> None:
    disabled = _add_user(db, status="disabled")
    no_email = _add_user(db, email=None)
    other_tenant = _add_user(db, tenant_id="tenant-b")

    disabled_response = client.post(f"/api/admin/users/{disabled.id}/reset-password")
    no_email_response = client.post(f"/api/admin/users/{no_email.id}/reset-password")
    cross_tenant_response = client.post(
        f"/api/admin/users/{other_tenant.id}/reset-password"
    )

    assert disabled_response.status_code == 409
    assert disabled_response.json() == {"detail": "user_not_active"}
    assert no_email_response.status_code == 400
    assert no_email_response.json() == {"detail": "user_has_no_email"}
    assert cross_tenant_response.status_code == 404
    assert cross_tenant_response.json() == {"detail": "User not found"}


@pytest.mark.parametrize("role", ["readonlyaudit", "compliance", "legal"])
def test_lifecycle_routes_require_admin_or_owner(
    client: TestClient, admin: AdminUser, db: Session, role: str
) -> None:
    target = _add_user(db)
    admin.role = role
    db.commit()

    for method, path in (
        ("patch", f"/api/admin/users/{target.id}"),
        ("patch", f"/api/admin/users/{target.id}/role"),
        ("post", f"/api/admin/users/{target.id}/reset-password"),
    ):
        body = {"role": "legal"} if path.endswith("/role") else {"status": "active"}
        response = getattr(client, method)(path, json=body if method == "patch" else None)
        assert response.status_code == 403
        assert response.json() == {"detail": "Insufficient role for this operation"}


def test_lifecycle_routes_require_authentication(db: Session, admin: AdminUser) -> None:
    from app.main import app

    target = _add_user(db)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            for method, path in (
                ("patch", f"/api/admin/users/{target.id}"),
                ("patch", f"/api/admin/users/{target.id}/role"),
                ("post", f"/api/admin/users/{target.id}/reset-password"),
            ):
                body = {"role": "legal"} if path.endswith("/role") else {"status": "active"}
                response = getattr(test_client, method)(
                    path, json=body if method == "patch" else None
                )
                assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# RND-321 — access-request review endpoints
# ---------------------------------------------------------------------------


def _add_access_request(
    db: Session,
    *,
    request_id: str | None = None,
    tenant_id: str = "tenant-a",
    subject: str = "wecom-unbound-001",
    display_name: str = "Unbound Employee",
    email_hint: str | None = None,
    status: str = "pending",
) -> AdminAccessRequest:
    request = AdminAccessRequest(
        id=request_id or str(uuid.uuid4()),
        tenant_id=tenant_id,
        provider="wecom",
        subject=subject,
        display_name=display_name,
        email_hint=email_hint,
        status=status,
    )
    db.add(request)
    db.commit()
    return request


def test_list_access_requests_defaults_to_pending_and_shows_suspected_match(
    client: TestClient, db: Session
) -> None:
    matching_account = _add_user(db, email="match@example.test")
    _add_access_request(db, email_hint="match@example.test")
    resolved = _add_access_request(
        db, subject="wecom-resolved", status="resolved", display_name="Already Handled"
    )

    response = client.get("/api/admin/access-requests")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["display_name"] == "Unbound Employee"
    assert items[0]["suspected_match"] == {"id": matching_account.id, "name": matching_account.name}
    # Never present anywhere in the response — reviewers act on the
    # request id, not the raw WeCom identity value.
    assert "subject" not in items[0]
    assert "wecom-unbound-001" not in response.text

    resolved_view = client.get("/api/admin/access-requests", params={"status": "resolved"})
    assert [item["id"] for item in resolved_view.json()["items"]] == [resolved.id]


def test_list_access_requests_shows_the_same_suspected_match_for_every_request_sharing_it(
    client: TestClient, db: Session
) -> None:
    """Two unrelated employees both listing the same (possibly stale or
    shared) email must each independently see the hint — the match is
    computed per request, not deduplicated or hidden after the first."""
    matching_account = _add_user(db, email="shared@example.test")
    first = _add_access_request(
        db, subject="wecom-dup-1", display_name="First Applicant", email_hint="shared@example.test"
    )
    second = _add_access_request(
        db, subject="wecom-dup-2", display_name="Second Applicant", email_hint="shared@example.test"
    )

    response = client.get("/api/admin/access-requests")
    items = {item["id"]: item for item in response.json()["items"]}
    assert items[first.id]["suspected_match"] == {"id": matching_account.id, "name": matching_account.name}
    assert items[second.id]["suspected_match"] == {"id": matching_account.id, "name": matching_account.name}
    # Both remain independently pending and independently resolvable —
    # showing the same hint twice must not merge or block either request.
    assert items[first.id]["status"] == "pending"
    assert items[second.id]["status"] == "pending"


def test_list_access_requests_suspected_match_is_none_when_two_accounts_share_the_email(
    client: TestClient, db: Session
) -> None:
    """Email reuse across accounts is real — two accounts sharing an email
    must never surface one of them as an arbitrary "suspected" pick.
    Multiple candidates is a conflict for the reviewer to judge, not
    something the API resolves by choosing one (QA-repro, RND-321)."""
    _add_user(db, email="shared@example.test")
    _add_user(db, email="shared@example.test")
    request = _add_access_request(db, email_hint="shared@example.test")

    response = client.get("/api/admin/access-requests")
    items = {item["id"]: item for item in response.json()["items"]}
    assert items[request.id]["suspected_match"] is None
    assert items[request.id]["suspected_match_status"] == "multiple"


def test_list_access_requests_suspected_match_status_is_single_or_none(
    client: TestClient, db: Session
) -> None:
    matching_account = _add_user(db, email="solo@example.test")
    with_match = _add_access_request(db, subject="wecom-solo", email_hint="solo@example.test")
    without_match = _add_access_request(db, subject="wecom-none", email_hint="nobody@example.test")

    response = client.get("/api/admin/access-requests")
    items = {item["id"]: item for item in response.json()["items"]}
    assert items[with_match.id]["suspected_match_status"] == "single"
    assert items[with_match.id]["suspected_match"] == {
        "id": matching_account.id,
        "name": matching_account.name,
    }
    assert items[without_match.id]["suspected_match_status"] == "none"
    assert items[without_match.id]["suspected_match"] is None


def test_link_access_request_binds_existing_account_and_syncs_compat_field(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import app.routers.users as users_module

    target = _add_user(db, role="compliance")
    request = _add_access_request(db, subject="wecom-link-001")
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": target.id},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "compliance"

    db.refresh(request)
    db.refresh(target)
    assert request.status == "resolved"
    assert request.resolution == "linked"
    assert request.resolved_admin_user_id == target.id
    assert request.resolved_by_admin_user_id == admin.id
    assert target.wecom_user_id == "wecom-link-001"

    identity = (
        db.query(AdminLoginIdentity)
        .filter_by(tenant_id="tenant-a", provider="wecom", subject="wecom-link-001")
        .one()
    )
    assert identity.admin_user_id == target.id

    call = audit_writer.call_args.kwargs
    assert call["action"] == "user.access_request_linked"
    assert call["object_id"] == request.id
    assert call["detail"] == {"linked_admin_user_id": target.id}


def test_link_access_request_rejects_missing_wrong_tenant_and_already_resolved(
    client: TestClient, db: Session
) -> None:
    target = _add_user(db)
    other_tenant_target = _add_user(db, tenant_id="tenant-b")
    pending = _add_access_request(db, subject="wecom-a")
    other_tenant_request = _add_access_request(db, tenant_id="tenant-b", subject="wecom-b")
    already_resolved = _add_access_request(db, subject="wecom-c", status="resolved")

    missing = client.post(
        "/api/admin/access-requests/missing/link", json={"admin_user_id": target.id}
    )
    cross_tenant_request = client.post(
        f"/api/admin/access-requests/{other_tenant_request.id}/link",
        json={"admin_user_id": target.id},
    )
    cross_tenant_target = client.post(
        f"/api/admin/access-requests/{pending.id}/link",
        json={"admin_user_id": other_tenant_target.id},
    )
    resolved_conflict = client.post(
        f"/api/admin/access-requests/{already_resolved.id}/link",
        json={"admin_user_id": target.id},
    )

    assert missing.status_code == 404
    assert cross_tenant_request.status_code == 404
    assert cross_tenant_target.status_code == 404
    assert resolved_conflict.status_code == 409


def test_link_access_request_rejects_account_that_already_has_an_identity(
    client: TestClient, db: Session
) -> None:
    target = _add_user(db)
    db.add(
        AdminLoginIdentity(
            id=str(uuid.uuid4()),
            tenant_id="tenant-a",
            provider="wecom",
            subject="already-bound-elsewhere",
            admin_user_id=target.id,
        )
    )
    db.commit()
    request = _add_access_request(db, subject="wecom-new-scan")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": target.id},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "account_already_has_login_identity"}


def _add_legacy_orphan(
    db: Session,
    *,
    account_id: str = "legacy-orphan",
    tenant_id: str = "tenant-a",
    wecom_user_id: str = "wecom-colliding-subject",
    name: str = "Legacy Orphan",
    invite_status: str | None = "access_requested",
) -> AdminUser:
    """A pre-RND-321 orphaned account: created by the old, superseded
    access_requested flow, deliberately excluded from migration 0034's
    identity backfill, and — since QA's real-Postgres round trip found the
    migration mutating it made upgrade/downgrade irreversible — never
    touched by the migration at all. Its legacy wecom_user_id survives
    exactly as it was; colliding with a fresh request for the same subject
    is handled at request-resolution time instead (this module)."""
    account = AdminUser(
        id=account_id,
        tenant_id=tenant_id,
        wecom_user_id=wecom_user_id,
        name=name,
        role="readonlyaudit",
        status="disabled",
        invite_status=invite_status,
    )
    db.add(account)
    db.commit()
    return account


def test_link_access_request_returns_structured_conflict_when_subject_collides_with_a_legacy_account(
    client: TestClient, db: Session
) -> None:
    """QA-repro (RND-321): resolving a fresh request for the same subject
    as an orphaned legacy account must surface a controlled, actionable
    conflict — not a plain 409 the caller can't act on, and no mutation
    unless a release is explicitly requested."""
    legacy_orphan = _add_legacy_orphan(db)
    legit_target = _add_user(db, role="compliance")
    request = _add_access_request(db, subject="wecom-colliding-subject")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": legit_target.id},
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error": "legacy_identity_conflict",
            "conflicting_account": {"id": legacy_orphan.id, "name": "Legacy Orphan"},
        }
    }

    db.refresh(request)
    db.refresh(legacy_orphan)
    db.refresh(legit_target)
    assert request.status == "pending"
    # Nothing mutated on a plain conflict preview — not the legacy row,
    # not the target, no identity created.
    assert legacy_orphan.wecom_user_id == "wecom-colliding-subject"
    assert legit_target.wecom_user_id != "wecom-colliding-subject"
    assert (
        db.query(AdminLoginIdentity)
        .filter_by(tenant_id="tenant-a", provider="wecom", subject="wecom-colliding-subject")
        .first()
        is None
    )


def test_link_access_request_denies_release_to_a_non_owner(
    client: TestClient, db: Session
) -> None:
    """Only owner may release a legacy account's stale identity claim —
    the shared `client` fixture authenticates as admin."""
    legacy_orphan = _add_legacy_orphan(db)
    legit_target = _add_user(db, role="compliance")
    request = _add_access_request(db, subject="wecom-colliding-subject")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": legit_target.id, "release_conflicting_legacy_account": True},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "owner_required_for_legacy_release"}

    db.refresh(request)
    db.refresh(legacy_orphan)
    assert request.status == "pending"
    assert legacy_orphan.wecom_user_id == "wecom-colliding-subject"


def test_link_access_request_owner_can_release_legacy_identity_and_complete_link(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import app.routers.users as users_module

    admin.role = "owner"
    db.commit()
    legacy_orphan = _add_legacy_orphan(db)
    legit_target = _add_user(db, role="compliance")
    request = _add_access_request(db, subject="wecom-colliding-subject")
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": legit_target.id, "release_conflicting_legacy_account": True},
    )
    assert response.status_code == 200

    db.refresh(request)
    db.refresh(legacy_orphan)
    db.refresh(legit_target)
    assert request.status == "resolved"
    assert legit_target.wecom_user_id == "wecom-colliding-subject"
    # Released, never deleted: the legacy row survives with a fresh,
    # non-colliding compat value; its role/status/name are untouched.
    assert legacy_orphan.wecom_user_id != "wecom-colliding-subject"
    assert legacy_orphan.status == "disabled"
    assert legacy_orphan.role == "readonlyaudit"
    assert legacy_orphan.name == "Legacy Orphan"

    detail = audit_writer.call_args.kwargs["detail"]
    assert detail["linked_admin_user_id"] == legit_target.id
    assert detail["released_legacy_account_id"] == legacy_orphan.id
    # The raw WeCom subject never appears in the audit detail.
    assert "wecom-colliding-subject" not in str(detail)


def test_link_access_request_refuses_release_of_an_account_that_is_not_a_confirmed_legacy_orphan(
    client: TestClient, db: Session, admin: AdminUser
) -> None:
    """Defensive rail: release only ever acts on a confirmed pre-RND-321
    orphan (invite_status='access_requested' and no existing login
    identity). Any other shape of collision — which the current design
    should never produce — must surface as an error, not be silently
    acted on."""
    admin.role = "owner"
    db.commit()
    not_an_orphan = _add_legacy_orphan(
        db, account_id="not-orphan", invite_status=None
    )
    legit_target = _add_user(db, role="compliance")
    request = _add_access_request(db, subject="wecom-colliding-subject")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": legit_target.id, "release_conflicting_legacy_account": True},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "legacy_release_not_permitted"}

    db.refresh(not_an_orphan)
    assert not_an_orphan.wecom_user_id == "wecom-colliding-subject"


def test_link_access_request_to_the_colliding_account_itself_is_not_a_conflict(
    client: TestClient, db: Session
) -> None:
    """An owner explicitly choosing to re-link a request back to the very
    same legacy account that already holds that subject is a no-op on the
    compat field, not a collision with itself."""
    legacy_orphan = _add_legacy_orphan(db)
    request = _add_access_request(db, subject="wecom-colliding-subject")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": legacy_orphan.id},
    )
    assert response.status_code == 200

    db.refresh(request)
    db.refresh(legacy_orphan)
    assert request.status == "resolved"
    assert legacy_orphan.wecom_user_id == "wecom-colliding-subject"
    identity = (
        db.query(AdminLoginIdentity)
        .filter_by(tenant_id="tenant-a", provider="wecom", subject="wecom-colliding-subject")
        .one()
    )
    assert identity.admin_user_id == legacy_orphan.id


def test_link_access_request_denies_admin_touching_an_owner_target(
    client: TestClient, db: Session
) -> None:
    owner_target = _add_user(db, role="owner")
    request = _add_access_request(db)

    response = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": owner_target.id},
    )
    assert response.status_code == 403


def test_create_account_from_access_request_binds_new_account(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import app.routers.users as users_module

    request = _add_access_request(
        db, subject="wecom-create-001", display_name="Brand New", email_hint="new@example.test"
    )
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    response = client.post(
        f"/api/admin/access-requests/{request.id}/create-account",
        json={"role": "legal", "status": "active"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "legal"
    assert body["status"] == "active"
    assert body["email"] == "new@example.test"

    new_user = db.query(AdminUser).filter_by(id=body["id"]).one()
    assert new_user.wecom_user_id == "wecom-create-001"
    assert new_user.name == "Brand New"

    db.refresh(request)
    assert request.status == "resolved"
    assert request.resolution == "created"
    assert request.resolved_admin_user_id == new_user.id

    identity = (
        db.query(AdminLoginIdentity)
        .filter_by(tenant_id="tenant-a", provider="wecom", subject="wecom-create-001")
        .one()
    )
    assert identity.admin_user_id == new_user.id
    assert audit_writer.call_args.kwargs["action"] == "user.access_request_account_created"


def test_create_account_from_access_request_returns_structured_conflict_on_wecom_user_id_collision(
    client: TestClient, db: Session
) -> None:
    """QA-repro (RND-321): a fresh request colliding with an orphaned
    legacy account's wecom_user_id must fail as a controlled, actionable
    conflict — not a crash, and no account created — with nothing mutated
    unless a release is explicitly requested."""
    legacy_orphan = _add_legacy_orphan(
        db, account_id="collision-holder", wecom_user_id="wecom-create-collision"
    )
    request = _add_access_request(db, subject="wecom-create-collision")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/create-account",
        json={"role": "legal", "status": "active"},
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error": "legacy_identity_conflict",
            "conflicting_account": {"id": legacy_orphan.id, "name": "Legacy Orphan"},
        }
    }

    db.refresh(request)
    db.refresh(legacy_orphan)
    assert request.status == "pending"
    assert request.resolution is None
    assert legacy_orphan.wecom_user_id == "wecom-create-collision"
    assert (
        db.query(AdminUser)
        .filter(AdminUser.wecom_user_id == "wecom-create-collision")
        .count()
        == 1
    )


def test_create_account_from_access_request_denies_release_to_a_non_owner(
    client: TestClient, db: Session
) -> None:
    legacy_orphan = _add_legacy_orphan(
        db, account_id="collision-holder", wecom_user_id="wecom-create-collision"
    )
    request = _add_access_request(db, subject="wecom-create-collision")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/create-account",
        json={
            "role": "legal",
            "status": "active",
            "release_conflicting_legacy_account": True,
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "owner_required_for_legacy_release"}

    db.refresh(legacy_orphan)
    assert legacy_orphan.wecom_user_id == "wecom-create-collision"


def test_create_account_from_access_request_owner_can_release_legacy_identity_and_complete_creation(
    client: TestClient, db: Session, admin: AdminUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import app.routers.users as users_module

    admin.role = "owner"
    db.commit()
    legacy_orphan = _add_legacy_orphan(
        db, account_id="collision-holder", wecom_user_id="wecom-create-collision"
    )
    request = _add_access_request(db, subject="wecom-create-collision")
    audit_writer = MagicMock()
    monkeypatch.setattr(users_module, "write_audit", audit_writer)

    response = client.post(
        f"/api/admin/access-requests/{request.id}/create-account",
        json={
            "role": "legal",
            "status": "active",
            "release_conflicting_legacy_account": True,
        },
    )
    assert response.status_code == 200
    new_user_id = response.json()["id"]

    db.refresh(legacy_orphan)
    assert legacy_orphan.wecom_user_id != "wecom-create-collision"
    assert legacy_orphan.status == "disabled"
    new_user = db.query(AdminUser).filter_by(id=new_user_id).one()
    assert new_user.wecom_user_id == "wecom-create-collision"

    detail = audit_writer.call_args.kwargs["detail"]
    assert detail["created_admin_user_id"] == new_user_id
    assert detail["released_legacy_account_id"] == legacy_orphan.id
    assert "wecom-create-collision" not in str(detail)


def test_create_account_from_access_request_can_start_disabled(
    client: TestClient, db: Session
) -> None:
    request = _add_access_request(db, subject="wecom-create-002")
    response = client.post(
        f"/api/admin/access-requests/{request.id}/create-account",
        json={"role": "readonlyaudit", "status": "disabled"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def test_create_account_from_access_request_rejects_invalid_role_status_and_owner_without_authority(
    client: TestClient, db: Session
) -> None:
    invalid_role = _add_access_request(db, subject="wecom-x1")
    invalid_status = _add_access_request(db, subject="wecom-x2")
    owner_attempt = _add_access_request(db, subject="wecom-x3")

    bad_role = client.post(
        f"/api/admin/access-requests/{invalid_role.id}/create-account",
        json={"role": "not-a-role", "status": "active"},
    )
    bad_status = client.post(
        f"/api/admin/access-requests/{invalid_status.id}/create-account",
        json={"role": "legal", "status": "not-a-status"},
    )
    cannot_grant_owner = client.post(
        f"/api/admin/access-requests/{owner_attempt.id}/create-account",
        json={"role": "owner", "status": "active"},
    )

    assert bad_role.status_code == 400
    assert bad_status.status_code == 400
    assert cannot_grant_owner.status_code == 403


def test_owner_can_grant_owner_role_when_creating_account_from_request(
    client: TestClient, db: Session, admin: AdminUser
) -> None:
    admin.role = "owner"
    db.commit()
    request = _add_access_request(db, subject="wecom-owner-grant")

    response = client.post(
        f"/api/admin/access-requests/{request.id}/create-account",
        json={"role": "owner", "status": "active"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "owner"


def test_second_resolution_attempt_on_same_request_is_rejected_not_double_applied(
    client: TestClient, db: Session
) -> None:
    """Simulates the race two concurrent reviewers would hit: the first
    resolve wins, the second must fail closed rather than silently
    creating a second binding for the same identity."""
    target_a = _add_user(db)
    target_b = _add_user(db)
    request = _add_access_request(db, subject="wecom-race-001")

    first = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": target_a.id},
    )
    second = client.post(
        f"/api/admin/access-requests/{request.id}/link",
        json={"admin_user_id": target_b.id},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert db.query(AdminLoginIdentity).filter_by(subject="wecom-race-001").count() == 1


@pytest.mark.parametrize("role", ["readonlyaudit", "compliance", "legal"])
def test_access_request_endpoints_require_admin_or_owner(
    client: TestClient, admin: AdminUser, db: Session, role: str
) -> None:
    target = _add_user(db)
    request = _add_access_request(db, subject="wecom-rbac")
    admin.role = role
    db.commit()

    responses = [
        client.get("/api/admin/access-requests"),
        client.post(
            f"/api/admin/access-requests/{request.id}/link",
            json={"admin_user_id": target.id},
        ),
        client.post(
            f"/api/admin/access-requests/{request.id}/create-account",
            json={"role": "legal", "status": "active"},
        ),
    ]
    for response in responses:
        assert response.status_code == 403
