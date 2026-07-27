"""
Tests for RND-222 — app.services.sync_worker, the extracted sync core loop.

Scope:
  - run_sync_once() persists GetChatData records scoped to (tenant_id,
    msgid) idempotency, advances the seq cursor only when records came
    back, and reports a non-zero GetChatData return code via the returned
    summary rather than raising.
  - Two tenants sharing a corp_id/msgid never collide (matches the
    uq_archive_messages_tenant_msgid constraint sync relies on).
  - The service module never prints, never calls sys.exit, and never
    imports app.main/routers/scripts.*.

Run (from backend/):
    pytest tests/test_sync_worker_service.py -v
"""

from __future__ import annotations

import inspect

from app.db.models import ArchiveMessage, SyncState
from app.services.sync_worker import run_sync_once
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    _TENANT_B,
    worker_db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)

_CORP_ID = "corp1"


def _chat_record(msgid: str, seq: int) -> dict:
    return {
        "msgid": msgid,
        "seq": seq,
        "publickey_ver": 1,
        "encrypt_random_key": "rk",
        "encrypt_chat_msg": "cm",
    }


def test_run_sync_once_persists_records_and_advances_seq_cursor(worker_db) -> None:
    sdk = FakeWecomSdk()
    sdk.set_chat_data([_chat_record("m1", 10), _chat_record("m2", 20)], ret=0)

    summary = run_sync_once(
        worker_db, _TENANT_A, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )

    assert summary.return_code == 0
    assert summary.record_count == 2
    assert summary.inserted == 2
    assert summary.skipped_duplicate == 0
    assert summary.previous_seq == 0
    assert summary.new_seq == 20

    rows = worker_db.query(ArchiveMessage).filter_by(tenant_id=_TENANT_A).all()
    assert {r.msgid for r in rows} == {"m1", "m2"}
    assert all(r.decrypt_status == "pending" for r in rows)

    state = worker_db.query(SyncState).filter_by(tenant_id=_TENANT_A, corp_id=_CORP_ID).one()
    assert state.last_seq == 20
    assert state.status == "idle"
    assert state.seq_version == 1
    assert state.started_at is not None
    assert state.error_message is None


def test_run_sync_once_is_idempotent_on_tenant_and_msgid(worker_db) -> None:
    sdk = FakeWecomSdk()
    sdk.set_chat_data([_chat_record("m1", 10)], ret=0)

    first = run_sync_once(
        worker_db, _TENANT_A, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )
    assert first.inserted == 1

    second = run_sync_once(
        worker_db, _TENANT_A, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )
    assert second.inserted == 0
    assert second.skipped_duplicate == 1

    assert worker_db.query(ArchiveMessage).filter_by(tenant_id=_TENANT_A).count() == 1


def test_run_sync_once_never_collides_across_tenants_sharing_a_msgid(worker_db) -> None:
    sdk = FakeWecomSdk()
    sdk.set_chat_data([_chat_record("shared-msgid", 5)], ret=0)

    run_sync_once(
        worker_db, _TENANT_A, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )
    run_sync_once(
        worker_db, _TENANT_B, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )

    rows = worker_db.query(ArchiveMessage).filter_by(msgid="shared-msgid").all()
    assert {r.tenant_id for r in rows} == {_TENANT_A, _TENANT_B}
    assert len(rows) == 2

    seq_a = worker_db.query(SyncState).filter_by(tenant_id=_TENANT_A, corp_id=_CORP_ID).one()
    seq_b = worker_db.query(SyncState).filter_by(tenant_id=_TENANT_B, corp_id=_CORP_ID).one()
    assert seq_a.last_seq == 5
    assert seq_b.last_seq == 5


def test_run_sync_once_reports_nonzero_return_code_without_touching_seq_or_records(
    worker_db,
) -> None:
    sdk = FakeWecomSdk()
    sdk.set_chat_data([_chat_record("should-not-persist", 99)], ret=90002)

    summary = run_sync_once(
        worker_db, _TENANT_A, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )

    assert summary.return_code == 90002
    assert summary.record_count == 0
    assert summary.inserted == 0
    assert summary.new_seq == 0
    assert worker_db.query(ArchiveMessage).count() == 0
    state = worker_db.query(SyncState).filter_by(tenant_id=_TENANT_A, corp_id=_CORP_ID).one()
    assert state.last_seq == 0
    assert state.status == "error"
    assert state.error_message == "sync_failed"
    assert state.seq_version == 0


def test_run_sync_once_advances_refresh_version_for_empty_success(worker_db) -> None:
    sdk = FakeWecomSdk()
    sdk.set_chat_data([], ret=0)

    summary = run_sync_once(
        worker_db, _TENANT_A, _CORP_ID, "fake-lib", "fake-handle", "fake-slice", 500, sdk=sdk
    )

    assert summary.return_code == 0
    assert summary.record_count == 0
    assert summary.new_seq == 0
    state = worker_db.query(SyncState).filter_by(tenant_id=_TENANT_A, corp_id=_CORP_ID).one()
    assert state.last_seq == 0
    assert state.status == "idle"
    assert state.seq_version == 1


def test_service_module_has_no_print_sys_exit_or_shell_imports() -> None:
    import ast

    from app.services import sync_worker

    source = inspect.getsource(sync_worker)
    assert "print(" not in source

    tree = ast.parse(source)
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "sys" not in imported_modules
    assert not any(m == "app.main" or m.startswith("app.routers") for m in imported_modules)
    assert not any(m == "scripts" or m.startswith("scripts.") for m in imported_modules)
