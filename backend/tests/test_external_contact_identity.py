"""RND-170 contracts for separated external-contact identity facts."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.db.external_contacts import upsert_external_contact
from app.db.models import (
    AdminUser,
    ArchiveMessage,
    ArchiveMessageRecipient,
    Contact,
    ExternalContact,
    ExternalContactFollow,
    ExternalContactNicknameHistory,
)
from app.services.external_contact_identity import (
    external_contact_display_names,
    normalize_identity_name,
    resolve_external_contact_display_name,
    sync_external_contact_identity,
)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


@pytest.fixture()
def identity_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        Contact.__table__,
        ExternalContact.__table__,
        ExternalContactFollow.__table__,
        ExternalContactNicknameHistory.__table__,
    ):
        table.create(engine)
    with Session(engine) as session:
        yield session


def _external_contact(
    db: Session,
    *,
    tenant_id: str = TENANT_A,
    external_userid: str = "wm-customer-001",
    legacy_name: str = "英子备注",
) -> ExternalContact:
    return upsert_external_contact(
        db,
        tenant_id,
        external_userid,
        legacy_name,
        None,
        "[]",
        None,
        None,
        None,
        None,
    )


def _detail(name: str, follows: list[dict]) -> dict:
    return {"external_contact": {"name": name}, "follow_user": follows}


def test_normalization_treats_space_and_invisible_only_values_as_blank() -> None:
    assert normalize_identity_name("  Alice\u200b  ") == "Alice"
    assert normalize_identity_name("\u200b\ufeff \t") is None
    assert normalize_identity_name("\u034f\ufe0f") is None
    assert normalize_identity_name("❤️") == "❤️"
    assert resolve_external_contact_display_name("wm-customer-001") != "wm-customer-001"


def test_sync_separates_current_nickname_remarks_and_history(identity_db: Session) -> None:
    contact = _external_contact(identity_db)
    first_seen = datetime(2026, 8, 1, tzinfo=timezone.utc)
    blank_seen = datetime(2026, 8, 2, tzinfo=timezone.utc)
    renamed_seen = datetime(2026, 8, 3, tzinfo=timezone.utc)

    first = sync_external_contact_identity(
        identity_db,
        contact,
        _detail(
            "  客户小王\u200b ",
            [
                {"userid": "staff-yingzi", "remark": "英子客户备注"},
                {"userid": "staff-li", "remark": "李四客户备注"},
            ],
        ),
        observed_at=first_seen,
    )
    identity_db.commit()

    assert first.nickname_changed is False
    assert contact.name == "英子备注"  # Legacy compatibility field is untouched.
    assert contact.current_nickname_raw == "  客户小王\u200b "
    assert contact.current_nickname_normalized == "客户小王"
    assert contact.current_nickname_display == "客户小王"
    relations = {
        row.follow_userid: row.remark_normalized
        for row in identity_db.query(ExternalContactFollow).all()
    }
    assert relations == {"staff-yingzi": "英子客户备注", "staff-li": "李四客户备注"}
    assert identity_db.query(ExternalContactNicknameHistory).count() == 0

    # Equivalent whitespace/invisible formatting is not a new identity state.
    same = sync_external_contact_identity(
        identity_db,
        contact,
        _detail("客户小王", [{"userid": "staff-yingzi", "remark": " 英子客户备注\u200b"}]),
        observed_at=blank_seen,
    )
    identity_db.commit()
    assert same.nickname_changed is False
    assert contact.current_nickname_raw == "  客户小王\u200b "
    assert contact.current_nickname_observed_at.replace(tzinfo=timezone.utc) == first_seen
    assert identity_db.query(ExternalContactNicknameHistory).count() == 0
    assert identity_db.query(ExternalContactFollow).filter_by(
        follow_userid="staff-yingzi"
    ).one().remark_raw == "英子客户备注"
    assert identity_db.query(ExternalContactFollow).filter_by(
        follow_userid="staff-li"
    ).one().is_active is False

    # A real blank transition is retained, and repeated invisible blanks do
    # not create a duplicate history event.
    blank = sync_external_contact_identity(
        identity_db,
        contact,
        _detail(" \u200b ", [{"userid": "staff-yingzi", "remark": "英子客户备注"}]),
        observed_at=blank_seen,
    )
    identity_db.commit()
    assert blank.nickname_changed is True
    assert contact.current_nickname_raw is None
    assert contact.current_nickname_normalized is None
    history = identity_db.query(ExternalContactNicknameHistory).one()
    assert history.old_nickname_normalized == "客户小王"
    assert history.new_nickname_normalized is None
    assert history.observed_at.replace(tzinfo=timezone.utc) == blank_seen

    repeated_blank = sync_external_contact_identity(
        identity_db,
        contact,
        _detail("\ufeff", [{"userid": "staff-yingzi", "remark": "英子客户备注"}]),
        observed_at=renamed_seen,
    )
    identity_db.commit()
    assert repeated_blank.nickname_changed is False
    assert identity_db.query(ExternalContactNicknameHistory).count() == 1

    renamed = sync_external_contact_identity(
        identity_db,
        contact,
        _detail("客户小王新名", [{"userid": "staff-yingzi", "remark": "英子客户备注"}]),
        observed_at=renamed_seen,
    )
    identity_db.commit()
    assert renamed.nickname_changed is True
    assert identity_db.query(ExternalContactNicknameHistory).count() == 2
    assert contact.current_nickname_normalized == "客户小王新名"


def test_malformed_follow_snapshot_does_not_deactivate_known_relationships(
    identity_db: Session,
) -> None:
    contact = _external_contact(identity_db)
    sync_external_contact_identity(
        identity_db,
        contact,
        _detail("客户昵称", [{"userid": "staff-yingzi", "remark": "员工备注"}]),
    )
    identity_db.commit()

    result = sync_external_contact_identity(
        identity_db,
        contact,
        _detail("客户昵称", [{"userid": "\u200b", "remark": "corrupt"}]),
    )
    identity_db.commit()

    assert result.follow_relations_changed == 0
    assert identity_db.query(ExternalContactFollow).one().is_active is True


def test_display_names_use_only_explicit_employee_context(identity_db: Session) -> None:
    contact = _external_contact(identity_db)
    sync_external_contact_identity(
        identity_db,
        contact,
        _detail(
            "客户真实昵称",
            [
                {"userid": "staff-yingzi", "remark": "英子备注"},
                {"userid": "staff-li", "remark": "李四备注"},
            ],
        ),
    )
    identity_db.commit()

    assert external_contact_display_names(
        identity_db, TENANT_A, {contact.external_userid}, follow_userid="staff-yingzi"
    ) == {contact.external_userid: "英子备注"}
    assert external_contact_display_names(
        identity_db, TENANT_A, {contact.external_userid}, follow_userid="staff-li"
    ) == {contact.external_userid: "李四备注"}
    assert external_contact_display_names(identity_db, TENANT_A, {contact.external_userid}) == {
        contact.external_userid: "客户真实昵称"
    }

    other = _external_contact(
        identity_db,
        tenant_id=TENANT_B,
        external_userid=contact.external_userid,
        legacy_name="Tenant B legacy",
    )
    sync_external_contact_identity(
        identity_db,
        other,
        _detail("Tenant B nickname", [{"userid": "staff-b", "remark": "Tenant B remark"}]),
    )
    identity_db.commit()
    assert external_contact_display_names(identity_db, TENANT_A, {contact.external_userid}) == {
        contact.external_userid: "客户真实昵称"
    }


def test_callback_targeted_refresh_fetches_current_profile_and_writes_history(
    identity_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import external_contact_sync as sync

    payload = {
        "external_contact": {"name": "回调前昵称"},
        "follow_user": [{"userid": "staff-yingzi", "remark": "回调备注"}],
    }
    monkeypatch.setattr(sync, "get_wecom_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(sync.wecom_contacts, "get_external_contact", lambda *args: payload)
    monkeypatch.setattr(sync.wecom_contacts, "get_corp_tag_list", lambda _token: {})
    monkeypatch.setattr(sync, "_interaction_stats", lambda *args: (None, None))

    first = sync.refresh_external_contact(
        identity_db, TENANT_A, "corp-a", "secret", "wm-callback-001"
    )
    identity_db.commit()
    payload["external_contact"]["name"] = "回调后昵称"
    second = sync.refresh_external_contact(
        identity_db, TENANT_A, "corp-a", "secret", "wm-callback-001"
    )
    identity_db.commit()

    row = identity_db.query(ExternalContact).one()
    assert first.found is True and first.inserted is True
    assert second.found is True and second.nickname_changed is True
    assert row.current_nickname_display == "回调后昵称"
    assert identity_db.query(ExternalContactNicknameHistory).count() == 1


def test_legacy_name_is_never_overwritten_by_another_follow_user(identity_db: Session) -> None:
    contact = _external_contact(identity_db, legacy_name="英子备注")
    identity_db.commit()

    updated = upsert_external_contact(
        identity_db,
        TENANT_A,
        contact.external_userid,
        "李四备注",
        None,
        "[]",
        None,
        "staff-li",
        None,
        None,
    )
    identity_db.commit()

    assert updated.name == "英子备注"
    assert updated.owner_wecom_userid == "staff-li"


@pytest.fixture()
def api_db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (
        Contact.__table__,
        AdminUser.__table__,
        ExternalContact.__table__,
        ExternalContactFollow.__table__,
        ExternalContactNicknameHistory.__table__,
    ):
        table.create(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture()
def api_client(api_db: Session):
    from app.auth import get_current_user
    from app.db.session import get_db
    from app.main import app

    def override_db():
        yield api_db

    app.dependency_overrides[get_current_user] = lambda: (
        SimpleNamespace(role="admin"),
        TENANT_A,
    )
    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


def _seed_searchable_identity(db: Session) -> ExternalContact:
    db.add(Contact(tenant_id=TENANT_A, wecom_userid="staff-yingzi", name="英子"))
    contact = _external_contact(db, external_userid="wm-search-001")
    sync_external_contact_identity(
        db,
        contact,
        _detail("旧昵称", [{"userid": "staff-yingzi", "remark": "重点客户"}]),
    )
    sync_external_contact_identity(
        db,
        contact,
        _detail("当前昵称", [{"userid": "staff-yingzi", "remark": "重点客户"}]),
    )
    other = _external_contact(
        db,
        tenant_id=TENANT_B,
        external_userid="wm-search-other",
        legacy_name="Other",
    )
    sync_external_contact_identity(
        db,
        other,
        _detail("当前昵称", [{"userid": "staff-b", "remark": "重点客户"}]),
    )
    db.commit()
    return contact


@pytest.mark.parametrize(
    ("query", "match_type"),
    [("重点", "remark"), ("当前", "current_nickname"), ("旧昵", "historical_nickname")],
)
def test_external_contact_list_searches_each_identity_layer_once(
    api_client: TestClient, api_db: Session, query: str, match_type: str
) -> None:
    contact = _seed_searchable_identity(api_db)

    response = api_client.get(f"/api/admin/external-contacts?q={query}")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["external_userid"] == contact.external_userid
    assert item["display_name"] == "当前昵称"
    assert item["current_nickname"] == "当前昵称"
    assert item["search_matches"] == [
        {
            "match_type": match_type,
            "follow_userid": "staff-yingzi" if match_type == "remark" else None,
        }
    ]
    assert item["follow_remarks"] == [
        {
            "follow_userid": "staff-yingzi",
            "follow_user_display_name": "英子",
            "remark": "重点客户",
            "is_active": True,
            "observed_at": item["follow_remarks"][0]["observed_at"],
        }
    ]


@pytest.mark.parametrize(
    ("query", "match_field", "match_context_userid"),
    [
        ("重点", "remark", "staff-yingzi"),
        ("当前", "current_nickname", None),
        ("旧昵", "historical_nickname", None),
    ],
)
def test_global_contact_search_marks_external_hit_type_and_deduplicates_identity(
    api_client: TestClient,
    api_db: Session,
    query: str,
    match_field: str,
    match_context_userid: str | None,
) -> None:
    contact = _seed_searchable_identity(api_db)

    response = api_client.get(f"/api/search/contacts?q={query}")

    assert response.status_code == 200
    body = response.json()
    matching = [item for item in body if item["wecom_userid"] == contact.external_userid]
    assert len(matching) == 1
    assert matching[0]["is_external_contact"] is True
    assert matching[0]["match_field"] == match_field
    assert matching[0]["match_context_userid"] == match_context_userid
    assert api_client.get("/api/search/contacts?q=%E2%80%8B").json() == []
    assert api_client.get("/api/admin/external-contacts?q=%E2%80%8B").json()["items"] == []


def test_model_and_migration_preserve_legacy_name_for_api_backfill() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/0035_external_contact_identity.py"
    ).read_text(encoding="utf-8")
    columns = {column.name for column in ExternalContact.__table__.columns}

    assert {
        "external_userid",
        "current_nickname_raw",
        "current_nickname_normalized",
        "current_nickname_display",
        "current_nickname_observed_at",
    } <= columns
    follow_fk = next(iter(ExternalContactFollow.__table__.foreign_key_constraints))
    history_fk = next(iter(ExternalContactNicknameHistory.__table__.foreign_key_constraints))
    assert [element.parent.name for element in follow_fk.elements] == [
        "tenant_id",
        "external_userid",
    ]
    assert [element.parent.name for element in history_fk.elements] == [
        "tenant_id",
        "external_userid",
    ]
    assert 'revision: str = "0035"' in migration
    assert 'down_revision: Union[str, None] = "0034"' in migration
    assert "fk_external_contact_follows_tenant_contact" in migration
    assert "fk_external_contact_nickname_history_tenant_contact" in migration
    assert "UPDATE external_contacts" not in migration
    assert "legacy external_contacts.name" in migration


@pytest.mark.parametrize("source", ("timer", "manual"))
def test_timer_and_manual_sync_reconcile_external_contacts_only_after_archive_success(
    source: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/run_archive_worker_once.py"
    spec = importlib.util.spec_from_file_location("rnd170_archive_worker", script)
    worker = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(worker)
    calls: list[str] = []
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("ARCHIVE_WORKER_TRIGGER_SOURCE", source)
    monkeypatch.setattr(worker, "_run_script", lambda _path, label: calls.append(label))
    monkeypatch.setattr(
        worker,
        "_run_external_contact_reconciliation",
        lambda: calls.append("external-contact-reconciliation"),
    )
    monkeypatch.setattr(
        worker,
        "_run_best_effort_reachability_automation",
        lambda: calls.append("reachability"),
    )
    monkeypatch.setattr(
        worker,
        "_request_media_worker_after_archive",
        lambda: worker.MediaWorkerDispatch.NO_WORK,
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    assert calls == [
        "sync_wecom_archive_once.py",
        "decrypt_wecom_messages_once.py",
        "external-contact-reconciliation",
        "reachability",
    ]


def test_staff_centered_conversation_titles_use_that_staffs_remark() -> None:
    from app.services.listing_service import list_conversations

    from tests.test_http_contract import _make_session

    db = _make_session()
    try:
        # The shared SQLite fixture deliberately contains only archive tables;
        # add the RND-170 tables after its setup transaction has committed.
        db.commit()
        for table in (
            ExternalContact.__table__,
            ExternalContactFollow.__table__,
            ExternalContactNicknameHistory.__table__,
        ):
            table.create(db.get_bind())

        db.add_all(
            [
                Contact(tenant_id=TENANT_A, wecom_userid="staff-yingzi", name="英子"),
                Contact(tenant_id=TENANT_A, wecom_userid="staff-li", name="李四"),
                Contact(tenant_id=TENANT_A, wecom_userid="wm-title-001", name="英子旧备注"),
                AdminUser(
                    id="admin-yingzi",
                    tenant_id=TENANT_A,
                    wecom_user_id="staff-yingzi",
                    name="英子",
                ),
                AdminUser(
                    id="admin-li",
                    tenant_id=TENANT_A,
                    wecom_user_id="staff-li",
                    name="李四",
                ),
            ]
        )
        contact = _external_contact(db, external_userid="wm-title-001")
        sync_external_contact_identity(
            db,
            contact,
            _detail(
                "客户当前昵称",
                [
                    {"userid": "staff-yingzi", "remark": "英子备注"},
                    {"userid": "staff-li", "remark": "李四备注"},
                ],
            ),
        )
        for msgid, sender, recipient, msgtime in (
            ("title-1", "staff-yingzi", "wm-title-001", 1),
            ("title-2", "staff-li", "wm-title-001", 2),
        ):
            message = ArchiveMessage(
                msgid=msgid,
                seq=msgtime,
                publickey_ver=1,
                encrypt_random_key="key",
                encrypt_chat_msg="payload",
                decrypt_status="success",
                msgtype="text",
                sender=sender,
                msgtime=msgtime,
                tenant_id=TENANT_A,
            )
            db.add(message)
            db.flush()
            db.add(
                ArchiveMessageRecipient(
                    message_id=message.id,
                    receiver_userid=recipient,
                    tenant_id=TENANT_A,
                )
            )
        db.commit()

        yingzi = list_conversations(db, TENANT_A, "staff-yingzi")
        li = list_conversations(db, TENANT_A, "staff-li")
        customer = list_conversations(db, TENANT_A, "wm-title-001")

        assert yingzi[0]["display_name"] == "英子备注"
        assert li[0]["display_name"] == "李四备注"
        assert customer[0]["display_name"] == "客户当前昵称"
    finally:
        db.close()
