"""
Tests for RND-222 — app.services.decrypt_worker, the extracted decrypt
core loop, and its tenant-scope audit fix.

Scope:
  - run_decrypt_once() only ever reads/repairs/commits ONE tenant's data —
    the pending/failed scan, the recipient-repair scan
    (repair_missing_recipients), the revoke-reconciliation repair scan
    (reconcile_pending_revocations), and the post-commit pending_remaining
    count are all tenant_id-filtered. Before this fix
    decrypt_wecom_messages_once.py's pending/failed scan had no tenant
    filter at all (see the RND-222 ticket's audit finding) — a run for
    tenant A must never observe or mutate tenant B's rows.
  - A commit failure raises DecryptCommitError rather than printing/
    exiting (service purity — see app/services/decrypt_worker.py's module
    docstring).
  - The service module never prints, never calls sys.exit, and never
    imports app.main/routers/scripts.* (would create the exact CLI/router
    coupling this extraction is meant to remove).

Run (from backend/):
    pytest tests/test_decrypt_worker_service.py -v
"""

from __future__ import annotations

import inspect

import pytest

from app.db.models import ArchiveMessageRecipient, ExternalContactRefreshTask, MessageRevocation
from app.services import decrypt_worker as decrypt_worker_module
from app.services.decrypt_isolation import IsolatedDecryptResult
from app.services.decrypt_worker import DecryptCommitError, run_decrypt_once
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    _TENANT_B,
    generate_test_rsa_keypair,
    insert_archive_message,
    rsa_encrypt_key_b64,
    worker_db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)

_PUBKEY_VER = 1


@pytest.fixture()
def rsa_keys():
    return generate_test_rsa_keypair()


def _make_pending(db, tenant_id, pub, encrypt_msg, **kwargs):
    return insert_archive_message(
        db,
        tenant_id=tenant_id,
        decrypt_status="pending",
        publickey_ver=_PUBKEY_VER,
        encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"),
        encrypt_chat_msg=encrypt_msg,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# RND-340 — newly observed room IDs are dispatched fail-soft after decrypt.
# ---------------------------------------------------------------------------


def test_new_group_room_dispatches_metadata_refresh_without_blocking_decrypt(
    worker_db, rsa_keys, monkeypatch
) -> None:
    priv, pub = rsa_keys
    message = _make_pending(worker_db, _TENANT_A, pub, "payload-group", seq=1)
    sdk = FakeWecomSdk()
    sdk.set_decrypt_response(
        "payload-group",
        {
            "msgtype": "text",
            "from": "staff_a",
            "tolist": ["contact_a"],
            "roomid": "test-group-room",
            "msgtime": 100,
            "text": {"content": "hi"},
        },
    )
    dispatched = []
    monkeypatch.setattr(
        decrypt_worker_module,
        "dispatch_group_chat_metadata_refresh",
        lambda tenant_id, corp_id, roomid: dispatched.append((tenant_id, corp_id, roomid)),
    )

    summary = run_decrypt_once(
        worker_db,
        _TENANT_A,
        "fake-lib",
        priv,
        _PUBKEY_VER,
        sdk=sdk,
        corp_id="test-corp",
    )

    assert summary.success == 1
    assert dispatched == [(_TENANT_A, "test-corp", "test-group-room")]
    worker_db.refresh(message)
    assert message.decrypt_status == "success"


def test_group_metadata_dispatch_failure_never_blocks_decrypt(worker_db, rsa_keys, monkeypatch) -> None:
    priv, pub = rsa_keys
    message = _make_pending(worker_db, _TENANT_A, pub, "payload-group-fail", seq=1)
    sdk = FakeWecomSdk()
    sdk.set_decrypt_response(
        "payload-group-fail",
        {
            "msgtype": "text",
            "from": "staff_a",
            "tolist": ["contact_a"],
            "roomid": "test-group-room",
            "msgtime": 100,
            "text": {"content": "hi"},
        },
    )
    monkeypatch.setattr(
        decrypt_worker_module,
        "dispatch_group_chat_metadata_refresh",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("dispatch failed")),
    )

    summary = run_decrypt_once(
        worker_db,
        _TENANT_A,
        "fake-lib",
        priv,
        _PUBKEY_VER,
        sdk=sdk,
        corp_id="test-corp",
    )

    assert summary.success == 1
    worker_db.refresh(message)
    assert message.decrypt_status == "success"


# ---------------------------------------------------------------------------
# Tenant scope — the RND-222 audit fix
# ---------------------------------------------------------------------------


def test_run_decrypt_once_only_touches_own_tenant_pending_rows(worker_db, rsa_keys) -> None:
    priv, pub = rsa_keys
    sdk = FakeWecomSdk()

    msg_a = _make_pending(worker_db, _TENANT_A, pub, "payload-a", seq=1)
    msg_b = _make_pending(worker_db, _TENANT_B, pub, "payload-b", seq=2)

    sdk.set_decrypt_response(
        "payload-a",
        {
            "msgtype": "text",
            "from": "staff_a",
            "tolist": ["contact_a"],
            "roomid": "",
            "msgtime": 100,
            "text": {"content": "hi"},
        },
    )
    # tenant B's row is deliberately left unconfigured on the fake SDK —
    # if the tenant filter in run_decrypt_once ever regresses to scanning
    # every tenant, decrypt_data() would be invoked for it too. Asserted
    # against directly below via msg_b's untouched decrypt_status instead
    # of relying on that call never happening.

    summary = run_decrypt_once(worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, sdk=sdk)

    assert summary.scanned == 1
    assert summary.success == 1

    worker_db.refresh(msg_a)
    worker_db.refresh(msg_b)
    assert msg_a.decrypt_status == "success"
    assert msg_a.content_text == "hi"
    assert msg_b.decrypt_status == "pending"
    assert msg_b.content_text is None


def test_repair_and_reconcile_scans_are_tenant_scoped(worker_db, rsa_keys) -> None:
    priv, _pub = rsa_keys
    sdk = FakeWecomSdk()

    # Tenant B: an already-decrypted message with a tolist but zero
    # recipient rows (a repair_missing_recipients() candidate), and a
    # pending revocation targeting an existing tenant-B original. Neither
    # must be touched by a tenant-A run.
    b_target = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_B,
        decrypt_status="success",
        msgid="orig-b",
        tolist=["contact_b"],
        msgtime=10,
    )
    b_revoke_event = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_B,
        decrypt_status="success",
        msgtype="revoke",
        msgtime=20,
    )
    worker_db.add(
        MessageRevocation(
            tenant_id=_TENANT_B,
            revoke_event_message_id=b_revoke_event.id,
            revoke_event_msgid=b_revoke_event.msgid,
            revoke_event_msgtime=20,
            target_msgid="orig-b",
            status="pending",
        )
    )
    worker_db.commit()

    # Tenant A has nothing pending/failed -- scanned == 0 -- but the
    # repair scans still run on every invocation (self-healing, by
    # design); they must still respect the tenant boundary.
    summary = run_decrypt_once(worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, sdk=sdk)

    assert summary.scanned == 0
    assert summary.recipients_repaired == 0
    assert summary.revocations_reconciled == 0

    assert (
        worker_db.query(ArchiveMessageRecipient)
        .filter_by(message_id=b_target.id)
        .count()
        == 0
    )
    revocation = worker_db.query(MessageRevocation).filter_by(tenant_id=_TENANT_B).one()
    assert revocation.status == "pending"
    assert revocation.original_message_id is None


def test_tenant_a_failures_never_mutate_tenant_b_committed_data(worker_db, rsa_keys) -> None:
    priv, _pub = rsa_keys
    sdk = FakeWecomSdk()

    b_msg = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_B,
        decrypt_status="success",
        content_text="already decrypted",
        msgtime=1,
    )
    # SF-2 guard case: missing encrypted fields -- an intentional,
    # deterministic tenant-A failure.
    a_msg = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="pending",
        encrypt_random_key="",
        encrypt_chat_msg="",
        msgtime=2,
    )

    summary = run_decrypt_once(worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, sdk=sdk)

    assert summary.scanned == 1
    assert summary.failed == 1

    worker_db.refresh(a_msg)
    worker_db.refresh(b_msg)
    assert a_msg.decrypt_status == "failed"
    assert b_msg.decrypt_status == "success"
    assert b_msg.content_text == "already decrypted"


def test_only_inbound_direct_external_messages_enqueue_contact_refresh(
    worker_db, rsa_keys
) -> None:
    """Group and outbound traffic never become external-contact API work."""
    ExternalContactRefreshTask.__table__.create(worker_db.bind)
    priv, pub = rsa_keys
    sdk = FakeWecomSdk()
    _make_pending(worker_db, _TENANT_A, pub, "inbound-direct", seq=1)
    _make_pending(worker_db, _TENANT_A, pub, "inbound-group", seq=2)
    _make_pending(worker_db, _TENANT_A, pub, "outbound-direct", seq=3)
    sdk.set_decrypt_response(
        "inbound-direct",
        {
            "msgtype": "text",
            "from": "wma-external",
            "tolist": ["staff_a"],
            "roomid": "",
            "msgtime": 100,
            "text": {"content": "hi"},
        },
    )
    sdk.set_decrypt_response(
        "inbound-group",
        {
            "msgtype": "text",
            "from": "wma-external",
            "tolist": ["staff_a"],
            "roomid": "room-001",
            "msgtime": 101,
            "text": {"content": "hi"},
        },
    )
    sdk.set_decrypt_response(
        "outbound-direct",
        {
            "msgtype": "text",
            "from": "staff_a",
            "tolist": ["wma-external"],
            "roomid": "",
            "msgtime": 102,
            "text": {"content": "hi"},
        },
    )

    summary = run_decrypt_once(worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, sdk=sdk)

    task = worker_db.query(ExternalContactRefreshTask).one()
    assert summary.external_contact_refresh_enqueued == 1
    assert task.external_userid == "wma-external"
    assert task.source == "inbound-direct-message"


# ---------------------------------------------------------------------------
# RND-231 — isolated DecryptData: a sigsegv on one row must not stop the
# batch, and must be classified distinctly from an ordinary SDK failure.
# Exercised via the decrypt_message_isolated seam (monkeypatched here,
# exactly like the sdk= injection pattern used everywhere else in this
# file) rather than by actually spawning a subprocess -- the subprocess
# mechanism itself (real SIGSEGV containment) is covered end-to-end by
# tests/test_decrypt_isolation.py; this file's job is proving
# run_decrypt_once's LOOP behavior around that seam.
# ---------------------------------------------------------------------------


def test_sigsegv_on_one_row_is_counted_and_batch_continues(worker_db, rsa_keys, monkeypatch) -> None:
    priv, pub = rsa_keys

    msg_ok = _make_pending(worker_db, _TENANT_A, pub, "payload-ok", seq=1)
    msg_crash = _make_pending(worker_db, _TENANT_A, pub, "payload-crash", seq=2)
    msg_ok2 = _make_pending(worker_db, _TENANT_A, pub, "payload-ok2", seq=3)

    def _fake_isolated(lib_path, encrypt_key, encrypt_msg, timeout=15.0):
        if encrypt_msg == "payload-crash":
            return IsolatedDecryptResult("sigsegv", None, None, "signal 11")
        return IsolatedDecryptResult(
            "success",
            0,
            (
                '{"msgtype":"text","from":"staff_a","tolist":["contact_a"],'
                '"roomid":"","msgtime":100,"text":{"content":"hi"}}'
            ),
        )

    monkeypatch.setattr(decrypt_worker_module, "decrypt_message_isolated", _fake_isolated)

    summary = run_decrypt_once(
        worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, lib_path="/fake/lib.so"
    )

    # The crashed row is one more counted failure -- the loop reached and
    # finished processing every row in the batch, it did not stop early.
    assert summary.scanned == 3
    assert summary.success == 2
    assert summary.failed == 1
    assert summary.sigsegv == 1

    worker_db.refresh(msg_ok)
    worker_db.refresh(msg_crash)
    worker_db.refresh(msg_ok2)
    assert msg_ok.decrypt_status == "success"
    assert msg_crash.decrypt_status == "failed"
    assert msg_ok2.decrypt_status == "success"


def test_isolation_other_outcome_is_counted_distinctly_from_sigsegv(worker_db, rsa_keys, monkeypatch) -> None:
    priv, pub = rsa_keys
    _make_pending(worker_db, _TENANT_A, pub, "payload-timeout", seq=1)

    monkeypatch.setattr(
        decrypt_worker_module,
        "decrypt_message_isolated",
        lambda *a, **k: IsolatedDecryptResult("other", None, None, "timeout"),
    )

    summary = run_decrypt_once(
        worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, lib_path="/fake/lib.so"
    )

    assert summary.failed == 1
    assert summary.sigsegv == 0
    assert summary.isolation_other == 1


def test_malformed_input_is_rejected_before_isolated_call_and_counted(worker_db, rsa_keys, monkeypatch) -> None:
    from app.services.decrypt_isolation import MalformedDecryptInput

    priv, pub = rsa_keys
    _make_pending(worker_db, _TENANT_A, pub, "payload-bad", seq=1)

    def _raise_malformed(*a, **k):
        raise MalformedDecryptInput("bad input")

    monkeypatch.setattr(decrypt_worker_module, "decrypt_message_isolated", _raise_malformed)

    summary = run_decrypt_once(
        worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, lib_path="/fake/lib.so"
    )

    assert summary.failed == 1
    assert summary.malformed_input == 1


def test_lib_path_omitted_uses_in_process_path_unchanged(worker_db, rsa_keys, monkeypatch) -> None:
    """Regression guard: every caller that omits lib_path (the default)
    must be completely unaffected by RND-231 -- decrypt_message_isolated
    must never even be consulted."""
    priv, pub = rsa_keys
    sdk = FakeWecomSdk()
    _make_pending(worker_db, _TENANT_A, pub, "payload-a", seq=1)
    sdk.set_decrypt_response(
        "payload-a",
        {
            "msgtype": "text", "from": "staff_a", "tolist": ["contact_a"],
            "roomid": "", "msgtime": 100, "text": {"content": "hi"},
        },
    )

    def _boom(*a, **k):
        raise AssertionError("decrypt_message_isolated must not be called without lib_path")

    monkeypatch.setattr(decrypt_worker_module, "decrypt_message_isolated", _boom)

    summary = run_decrypt_once(worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, sdk=sdk)
    assert summary.success == 1


# ---------------------------------------------------------------------------
# Commit failure -> DecryptCommitError, never sys.exit
# ---------------------------------------------------------------------------


def test_commit_failure_raises_decrypt_commit_error(worker_db, rsa_keys, monkeypatch) -> None:
    priv, _pub = rsa_keys
    sdk = FakeWecomSdk()

    def _boom():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(worker_db, "commit", _boom)

    with pytest.raises(DecryptCommitError, match="db exploded"):
        run_decrypt_once(worker_db, _TENANT_A, "fake-lib", priv, _PUBKEY_VER, sdk=sdk)


# ---------------------------------------------------------------------------
# Service purity
# ---------------------------------------------------------------------------


def test_service_module_has_no_print_sys_exit_or_shell_imports() -> None:
    import ast

    from app.services import decrypt_worker

    source = inspect.getsource(decrypt_worker)
    # print( is a plain substring check (safe -- no docstring in this
    # module happens to contain that literal). Imports are checked via the
    # AST, not substring matching, since several docstrings here reference
    # "scripts/decrypt_wecom_messages_once.py" and "sys.exit" in prose.
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
