"""
Tests for RND-201 — scripts/backfill_revoke_associations_once.py.

Covers the historical-catch-up backfill for revoke-type archive_messages
rows that reached decrypt_status="success" before RND-201 shipped (and so
were never passed through app.revoke_reconciliation.reconcile_revoke_event
at decrypt time). Uses the shared sqlite-backed `db` fixture / _TENANT_A /
_TENANT_B / _insert_message helper (see test_reachability_audit.py).
"""

from __future__ import annotations

import pytest

from app.db.models import MessageRevocation
from scripts.backfill_revoke_associations_once import (
    find_unreconciled_revoke_events,
    main,
)
from tests.test_reachability_audit import _TENANT_A, _TENANT_B, _insert_message, db  # noqa: F401 -- pytest fixture, must be imported to be discovered


def _revoke_structured_content(pre_msgid: "str | None") -> dict:
    if not pre_msgid:
        return {"fields": None, "raw": {}, "parse_warnings": ["missing_pre_msgid"]}
    return {"fields": {"pre_msgid": pre_msgid}, "raw": {"pre_msgid": pre_msgid}, "parse_warnings": []}


def _insert_revoke_event(db, *, pre_msgid, tenant_id=_TENANT_A, decrypt_status="success", **kwargs):
    return _insert_message(
        db,
        msgtype="revoke",
        structured_content=_revoke_structured_content(pre_msgid),
        tenant_id=tenant_id,
        decrypt_status=decrypt_status,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# find_unreconciled_revoke_events — candidate query
# ---------------------------------------------------------------------------


def test_finds_historical_revoke_row_with_no_message_revocations_row(db) -> None:
    revoke_event = _insert_revoke_event(db, pre_msgid="target-1", seq=1)
    candidates = find_unreconciled_revoke_events(db).all()
    assert [c.id for c in candidates] == [revoke_event.id]


def test_excludes_revoke_row_already_reconciled(db) -> None:
    from app.revoke_reconciliation import reconcile_revoke_event

    revoke_event = _insert_revoke_event(db, pre_msgid="target-2", seq=1)
    reconcile_revoke_event(db, revoke_event)
    db.commit()

    candidates = find_unreconciled_revoke_events(db).all()
    assert candidates == []


def test_excludes_non_revoke_and_not_yet_decrypted_rows(db) -> None:
    _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    _insert_revoke_event(db, pre_msgid="t", seq=2, decrypt_status="pending")

    candidates = find_unreconciled_revoke_events(db).all()
    assert candidates == []


def test_candidate_query_respects_tenant_scope(db) -> None:
    revoke_a = _insert_revoke_event(db, pre_msgid="ta", tenant_id=_TENANT_A, seq=1)
    _insert_revoke_event(db, pre_msgid="tb", tenant_id=_TENANT_B, seq=2)

    candidates = find_unreconciled_revoke_events(db, _TENANT_A).all()
    assert [c.id for c in candidates] == [revoke_a.id]


# ---------------------------------------------------------------------------
# main() — dry-run vs apply, reporting
# ---------------------------------------------------------------------------


def _sqlite_url(tmp_path, name="backfill.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _make_populated_sqlite_db(tmp_path):
    """A real (file-backed, not shared-connection in-memory) sqlite DB
    with the same schema as the shared `db` fixture, populated with one
    of each candidate outcome: linkable, pending, malformed. Used to
    drive main() end-to-end via a real DATABASE_URL, the same way an
    operator would run this script."""
    from sqlalchemy import create_engine, text
    from tests.test_reachability_audit import _SCHEMA_SQL

    url = _sqlite_url(tmp_path)
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    from sqlalchemy.orm import Session
    from app.db.models import ArchiveMessage

    session = Session(engine)
    # Linkable: original already archived.
    session.add(ArchiveMessage(
        msgid="orig-linkable", seq=1, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="text", content_text="hi", tenant_id=_TENANT_A,
    ))
    session.add(ArchiveMessage(
        msgid="revoke-linkable", seq=2, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke",
        structured_content=_revoke_structured_content("orig-linkable"), tenant_id=_TENANT_A,
    ))
    # Pending: no original archived.
    session.add(ArchiveMessage(
        msgid="revoke-pending", seq=3, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke",
        structured_content=_revoke_structured_content("never-shows-up"), tenant_id=_TENANT_A,
    ))
    # Malformed: no pre_msgid.
    session.add(ArchiveMessage(
        msgid="revoke-malformed", seq=4, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke",
        structured_content=_revoke_structured_content(None), tenant_id=_TENANT_A,
    ))
    session.commit()
    session.close()
    engine.dispose()
    return url


def test_dry_run_does_not_persist_any_changes(tmp_path, monkeypatch, capsys) -> None:
    url = _make_populated_sqlite_db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py"])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "mode: DRY-RUN" in out
    assert "linked: 1" in out
    assert "pending: 1" in out
    assert "malformed: 1" in out

    # Nothing was actually written -- a fresh session sees no
    # message_revocations rows and the original is still not revoked.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.models import ArchiveMessage

    engine = create_engine(url)
    session = Session(engine)
    assert session.query(MessageRevocation).count() == 0
    original = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-linkable").one()
    assert original.is_revoked is False
    session.close()


def test_apply_persists_changes(tmp_path, monkeypatch, capsys) -> None:
    url = _make_populated_sqlite_db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "mode: APPLY" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.models import ArchiveMessage

    engine = create_engine(url)
    session = Session(engine)
    assert session.query(MessageRevocation).count() == 3
    original = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-linkable").one()
    assert original.is_revoked is True
    session.close()


def test_apply_is_idempotent_on_second_run(tmp_path, monkeypatch, capsys) -> None:
    url = _make_populated_sqlite_db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])

    with pytest.raises(SystemExit):
        main()
    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr().out
    # Second run's candidate scan finds nothing left to do.
    assert "candidates_scanned: 0" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    assert session.query(MessageRevocation).count() == 3  # not duplicated
    session.close()


def test_tenant_scoped_apply_does_not_touch_other_tenants(tmp_path, monkeypatch, capsys) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from app.db.models import ArchiveMessage
    from tests.test_reachability_audit import _SCHEMA_SQL

    url = _sqlite_url(tmp_path, "tenant_scoped.db")
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    session = Session(engine)
    session.add(ArchiveMessage(
        msgid="orig-a", seq=1, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="text", content_text="a", tenant_id=_TENANT_A,
    ))
    session.add(ArchiveMessage(
        msgid="revoke-a", seq=2, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke",
        structured_content=_revoke_structured_content("orig-a"), tenant_id=_TENANT_A,
    ))
    session.add(ArchiveMessage(
        msgid="orig-b", seq=1, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="text", content_text="b", tenant_id=_TENANT_B,
    ))
    session.add(ArchiveMessage(
        msgid="revoke-b", seq=2, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke",
        structured_content=_revoke_structured_content("orig-b"), tenant_id=_TENANT_B,
    ))
    session.commit()
    session.close()
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply", "--tenant-id", _TENANT_A])

    with pytest.raises(SystemExit):
        main()

    engine = create_engine(url)
    session = Session(engine)
    original_a = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-a").one()
    original_b = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-b").one()
    assert original_a.is_revoked is True
    assert original_b.is_revoked is False  # untouched -- different tenant
    assert session.query(MessageRevocation).count() == 1
    session.close()
