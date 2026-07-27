"""
Tests for RND-257 — scripts/reparse_structured_content_once.py.

Covers the historical chatrecord/mixed structured_content backfill: old
broken-shape detection, candidate selection, the re-decrypt+re-parse core
(reparse_one_message), the main loop's safety boundary (only
structured_content is ever written -- decrypted_payload/decrypt_status/
every other column stays untouched, per SF-1), idempotency, and the CLI
shell's dry-run/apply contract.

Uses the shared FakeWecomSdk / worker_db / worker_engine fixtures and RSA
test helpers from tests/fakes.py -- the same fixtures
tests/test_decrypt_wecom_messages_once_cli.py and
tests/test_backfill_revoke_associations.py already use for this exact
kind of re-decrypt test.

Run (from backend/):
    pytest tests/test_reparse_structured_content_once.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.db.models import ArchiveMessage
from app.services.decrypt_isolation import SIGSEGV_SENTINEL
from scripts import reparse_structured_content_once as reparse_script
from scripts.reparse_structured_content_once import (
    find_reparse_candidates,
    is_stale_structured_content,
    reparse_one_message,
    run_reparse_once,
    select_reparse_candidates,
    trigger_nested_media_download_scan,
)
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    _TENANT_B,
    generate_test_rsa_keypair,
    insert_archive_message,
    insert_tenant,
    insert_tenant_wecom_config,
    rsa_encrypt_key_b64,
    worker_db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
    worker_engine,  # noqa: F401 -- pytest fixture, must be imported to be discovered
    write_private_key_pem,
)
from scripts.decrypt_wecom_messages_once import _normalise_fields
from tests.test_decrypt_wecom_messages_once_cli import _install_fake_decrypt_isolated

# ---------------------------------------------------------------------------
# Fixtures matching the pre-RND-243 broken shape and the RND-243-fixed
# parser's expected input/output (see
# tests/test_structured_message_parser.py's
# test_parse_chatrecord_message_chatrecord_image_no_longer_echoes_raw_json
# for the real fixed-parser contract this mirrors).
# ---------------------------------------------------------------------------

_OLD_BROKEN_STRUCTURED_CONTENT = {
    "fields": {
        "title": "转发聊天记录",
        "items": [
            {
                "type": "ChatRecordImage",
                "text": json.dumps({"md5sum": "d82ea6db...", "filesize": 902691, "sdkfileid": "CtYB..."}),
                "fields": None,
                "media": None,
                "sender": None,
                "sender_name": None,
                "timestamp": None,
                "children": None,
                "supported": False,
            }
        ],
        "item_count": 1,
    },
    "raw": {"title": "转发聊天记录", "item": [{"type": "ChatRecordImage", "content": "..."}]},
    "parse_warnings": [],
}

_FIXED_CHATRECORD_DECRYPTED = {
    "msgtype": "chatrecord",
    "from": "staff_a",
    "tolist": ["contact_a"],
    "roomid": "",
    "msgtime": 100,
    "chatrecord": {
        "title": "转发聊天记录",
        "item": [
            {
                "type": "ChatRecordImage",
                "content": json.dumps({"md5sum": "d82ea6db...", "filesize": 902691, "sdkfileid": "CtYB..."}),
            }
        ],
    },
}

_TEXT_ONLY_CHATRECORD_DECRYPTED = {
    "msgtype": "chatrecord",
    "from": "staff_a",
    "tolist": [],
    "roomid": "",
    "msgtime": 100,
    "chatrecord": {
        "title": "文字记录",
        "item": [{"type": "ChatRecordText", "content": json.dumps({"content": "hello"})}],
    },
}


# ---------------------------------------------------------------------------
# is_stale_structured_content / _has_raw_json_echo
# ---------------------------------------------------------------------------


def test_is_stale_when_structured_content_is_none() -> None:
    assert is_stale_structured_content(None) is True


def test_is_stale_when_not_a_dict() -> None:
    assert is_stale_structured_content("not-a-dict") is True


def test_is_stale_when_media_refs_missing() -> None:
    sc = {"fields": {"items": [], "item_count": 0}, "raw": {}, "parse_warnings": []}
    assert is_stale_structured_content(sc) is True


def test_is_stale_when_media_refs_empty_list() -> None:
    sc = {"fields": {"items": [], "item_count": 0}, "raw": {}, "parse_warnings": [], "media_refs": []}
    assert is_stale_structured_content(sc) is True


def test_is_stale_when_item_text_is_raw_json_with_sdkfileid() -> None:
    sc = dict(_OLD_BROKEN_STRUCTURED_CONTENT)
    sc["media_refs"] = ["not empty but the item text is still the bug signature"]
    assert is_stale_structured_content(sc) is True


def test_is_stale_detects_raw_json_echo_in_nested_child() -> None:
    sc = {
        "fields": {
            "items": [
                {
                    "type": "mixed",
                    "text": None,
                    "children": [
                        {
                            "type": "ChatRecordImage",
                            "text": json.dumps({"sdkfileid": "sdk-1"}),
                            "children": None,
                        }
                    ],
                }
            ],
            "item_count": 1,
        },
        "raw": {},
        "parse_warnings": [],
        "media_refs": ["present"],
    }
    assert is_stale_structured_content(sc) is True


def test_is_not_stale_for_correctly_parsed_media_message() -> None:
    sc = {
        "fields": {
            "items": [{"type": "image", "text": None, "media": {"has_reference": True}, "children": None}],
            "item_count": 1,
        },
        "raw": {},
        "parse_warnings": [],
        "media_refs": [{"path": "0", "type": "image", "sdkfileid": "sdk-1"}],
    }
    assert is_stale_structured_content(sc) is False


def test_is_not_stale_for_ordinary_non_json_text() -> None:
    sc = {
        "fields": {"items": [{"type": "text", "text": "hello, not json", "children": None}], "item_count": 1},
        "raw": {},
        "parse_warnings": [],
        "media_refs": [{"path": "0", "type": "image", "sdkfileid": "unrelated"}],
    }
    assert is_stale_structured_content(sc) is False


def test_is_not_stale_for_json_text_without_sdkfileid_key() -> None:
    sc = {
        "fields": {
            "items": [{"type": "text", "text": json.dumps({"content": "hi"}), "children": None}],
            "item_count": 1,
        },
        "raw": {},
        "parse_warnings": [],
        "media_refs": [{"path": "0", "type": "image", "sdkfileid": "unrelated"}],
    }
    assert is_stale_structured_content(sc) is False


# ---------------------------------------------------------------------------
# find_reparse_candidates / select_reparse_candidates
# ---------------------------------------------------------------------------


def test_find_candidates_only_matches_chatrecord_and_mixed(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_archive_message(worker_db, msgtype="text", content_text="hi", seq=1)
    chatrecord = insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=2
    )
    candidates = find_reparse_candidates(worker_db).all()
    assert [c.id for c in candidates] == [chatrecord.id]


def test_find_candidates_excludes_non_success_decrypt_status(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_archive_message(worker_db, msgtype="mixed", decrypt_status="pending", seq=1)
    candidates = find_reparse_candidates(worker_db).all()
    assert candidates == []


def test_select_candidates_excludes_already_correct_rows(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    fresh_sc = {
        "fields": {"items": [], "item_count": 0},
        "raw": {},
        "parse_warnings": [],
        "media_refs": [{"path": "0", "type": "image", "sdkfileid": "sdk-1"}],
    }
    insert_archive_message(worker_db, msgtype="mixed", structured_content=fresh_sc, seq=1)
    stale = insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=2
    )
    candidates, total_scanned = select_reparse_candidates(worker_db)
    assert total_scanned == 2
    assert [c.id for c in candidates] == [stale.id]


def test_select_candidates_respects_tenant_scope(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_tenant(worker_db, _TENANT_B)
    a = insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=1, tenant_id=_TENANT_A
    )
    insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=1, tenant_id=_TENANT_B
    )
    candidates, _total = select_reparse_candidates(worker_db, tenant_id=_TENANT_A)
    assert [c.id for c in candidates] == [a.id]


def test_select_candidates_respects_since_and_until(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    too_old = insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=1, msgtime=100
    )
    in_window = insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=2, msgtime=200
    )
    too_new = insert_archive_message(
        worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=3, msgtime=300
    )
    candidates, _total = select_reparse_candidates(worker_db, since_ms=150, until_ms=250)
    assert [c.id for c in candidates] == [in_window.id]
    assert too_old.id not in [c.id for c in candidates]
    assert too_new.id not in [c.id for c in candidates]


def test_select_candidates_respects_limit(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    for i in range(5):
        insert_archive_message(
            worker_db, msgtype="chatrecord", structured_content=_OLD_BROKEN_STRUCTURED_CONTENT, seq=i
        )
    candidates, total_scanned = select_reparse_candidates(worker_db, limit=2)
    assert len(candidates) == 2
    assert total_scanned == 5


# ---------------------------------------------------------------------------
# reparse_one_message
# ---------------------------------------------------------------------------


def _fake_row(**kwargs):
    defaults = dict(
        msgid="m1",
        seq=1,
        publickey_ver=1,
        encrypt_random_key="x",
        encrypt_chat_msg="payload-1",
        decrypt_status="success",
        msgtype="chatrecord",
        tenant_id=_TENANT_A,
    )
    defaults.update(kwargs)
    return ArchiveMessage(**defaults)


def test_reparse_one_message_success_produces_correct_media_refs() -> None:
    priv, pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)
    row = _fake_row(encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"))

    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)

    assert outcome == "reparsed"
    assert structured_content["media_refs"] == [{"path": "0", "type": "image", "sdkfileid": "CtYB..."}]
    assert structured_content["fields"]["items"][0]["type"] == "image"


def test_reparse_one_message_never_touches_the_row(monkeypatch) -> None:
    """reparse_one_message is a pure function w.r.t. `row` -- it only ever
    reads from it and returns data; persistence is the caller's job."""
    priv, pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)
    row = _fake_row(encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"))
    original_structured_content = row.structured_content
    original_decrypted_payload = row.decrypted_payload

    reparse_one_message(priv, fake.lib, 1, row, sdk=fake)

    assert row.structured_content is original_structured_content
    assert row.decrypted_payload is original_decrypted_payload


def test_reparse_one_message_key_mismatch() -> None:
    priv, _pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    row = _fake_row(publickey_ver=2)
    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)
    assert outcome == "key_mismatch"
    assert structured_content is None


def test_reparse_one_message_missing_envelope() -> None:
    priv, _pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    row = _fake_row(encrypt_random_key="", encrypt_chat_msg="")
    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)
    assert outcome == "missing_envelope"
    assert structured_content is None


def test_reparse_one_message_rsa_failed() -> None:
    priv, _pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    row = _fake_row(encrypt_random_key="not-valid-base64-ciphertext")
    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)
    assert outcome == "rsa_failed"
    assert structured_content is None


def test_reparse_one_message_sdk_decrypt_failed() -> None:
    priv, pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", None, ret=90002)
    row = _fake_row(encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"))
    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)
    assert outcome == "sdk_decrypt_failed"
    assert structured_content is None


def test_reparse_one_message_sigsegv(monkeypatch) -> None:
    priv, pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    row = _fake_row(encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"))
    monkeypatch.setattr(reparse_script, "_decrypt_message", lambda *a, **k: (SIGSEGV_SENTINEL, None))
    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)
    assert outcome == "sigsegv"
    assert structured_content is None


def test_reparse_one_message_invalid_json(monkeypatch) -> None:
    priv, pub = generate_test_rsa_keypair()
    fake = FakeWecomSdk()
    row = _fake_row(encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"))
    monkeypatch.setattr(reparse_script, "_decrypt_message", lambda *a, **k: (0, "not-valid-json{"))
    outcome, structured_content = reparse_one_message(priv, fake.lib, 1, row, sdk=fake)
    assert outcome == "invalid_json"
    assert structured_content is None


# ---------------------------------------------------------------------------
# run_reparse_once — end-to-end safety boundary, idempotency, tenant scope
# ---------------------------------------------------------------------------


def _insert_reparseable_row(db, priv_pub, encrypt_msg_key, decrypted_payload, **kwargs):
    _priv, pub = priv_pub
    defaults = dict(
        msgtype="chatrecord",
        publickey_ver=1,
        encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"),
        encrypt_chat_msg=encrypt_msg_key,
        structured_content=_OLD_BROKEN_STRUCTURED_CONTENT,
        content_text=None,
        sender="staff_a",
        roomid="",
        msgtime=100,
        tolist=["contact_a"],
        sdkfileid=None,
    )
    defaults.update(kwargs)
    return insert_archive_message(db, **defaults)


def test_run_reparse_once_fixes_structured_content_and_only_that_column(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    keypair = generate_test_rsa_keypair()
    row = _insert_reparseable_row(worker_db, keypair, "payload-1", _FIXED_CHATRECORD_DECRYPTED)
    row_id = row.id

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

    summary = run_reparse_once(worker_db, keypair[0], fake.lib, 1, sdk=fake)

    assert summary.stale_detected == 1
    assert summary.reparsed == 1
    assert summary.media_refs_added == 1

    fresh = worker_db.query(ArchiveMessage).get(row_id)
    assert fresh.structured_content["media_refs"] == [{"path": "0", "type": "image", "sdkfileid": "CtYB..."}]
    # Safety boundary (SF-1 + "only structured_content"): every other
    # column is byte-identical to what it was before this run.
    assert fresh.decrypted_payload is None
    assert fresh.decrypt_status == "success"
    assert fresh.content_text is None
    assert fresh.sender == "staff_a"
    assert fresh.roomid == ""
    assert fresh.msgtime == 100
    assert fresh.tolist == ["contact_a"]
    assert fresh.sdkfileid is None
    assert fresh.msgtype == "chatrecord"


def test_run_reparse_once_dry_run_persists_nothing(worker_engine) -> None:
    from sqlalchemy.orm import Session

    keypair = generate_test_rsa_keypair()
    with Session(worker_engine) as session:
        insert_tenant(session, _TENANT_A)
        row = _insert_reparseable_row(session, keypair, "payload-1", _FIXED_CHATRECORD_DECRYPTED)
        row_id = row.id

        fake = FakeWecomSdk()
        fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

        summary = run_reparse_once(session, keypair[0], fake.lib, 1, sdk=fake, dry_run=True)
        assert summary.reparsed == 1  # reports what WOULD happen

    # A fresh session/connection sees the real, unmodified DB state.
    with Session(worker_engine) as verify_session:
        persisted = verify_session.query(ArchiveMessage).get(row_id)
        assert persisted.structured_content == _OLD_BROKEN_STRUCTURED_CONTENT


def test_run_reparse_once_is_idempotent_on_second_run(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    keypair = generate_test_rsa_keypair()
    _insert_reparseable_row(worker_db, keypair, "payload-1", _FIXED_CHATRECORD_DECRYPTED)

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

    first = run_reparse_once(worker_db, keypair[0], fake.lib, 1, sdk=fake)
    assert first.reparsed == 1

    second = run_reparse_once(worker_db, keypair[0], fake.lib, 1, sdk=fake)
    assert second.stale_detected == 0
    assert second.reparsed == 0


def test_run_reparse_once_respects_tenant_scope(worker_db) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_tenant(worker_db, _TENANT_B)
    keypair = generate_test_rsa_keypair()
    row_b = _insert_reparseable_row(
        worker_db, keypair, "payload-1", _FIXED_CHATRECORD_DECRYPTED, tenant_id=_TENANT_B
    )

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

    summary = run_reparse_once(worker_db, keypair[0], fake.lib, 1, tenant_id=_TENANT_A, sdk=fake)

    assert summary.stale_detected == 0
    untouched = worker_db.query(ArchiveMessage).get(row_b.id)
    assert untouched.structured_content == _OLD_BROKEN_STRUCTURED_CONTENT


def test_run_reparse_once_skips_unchanged_text_only_message_without_writing(worker_db) -> None:
    """A chatrecord message with only text children legitimately has an
    empty media_refs list forever -- is_stale_structured_content keeps
    flagging it as a candidate, but reparsing produces byte-identical
    output each time, so it must be counted as skipped_unchanged, never
    written, and never miscounted as a real fix."""
    insert_tenant(worker_db, _TENANT_A)
    keypair = generate_test_rsa_keypair()
    # Pre-computed via the real (current, already-correct) parser itself --
    # not hand-crafted -- so it is guaranteed byte-identical to whatever
    # reparse_one_message recomputes, exercising the genuine "reparse
    # output == what's already stored" path rather than an approximation.
    text_only_sc = _normalise_fields(_TEXT_ONLY_CHATRECORD_DECRYPTED)["structured_content"]
    assert text_only_sc["media_refs"] == []  # sanity: this fixture must be a legitimate stale-forever case
    row = _insert_reparseable_row(
        worker_db, keypair, "payload-text", _TEXT_ONLY_CHATRECORD_DECRYPTED, structured_content=text_only_sc
    )

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-text", _TEXT_ONLY_CHATRECORD_DECRYPTED)

    summary = run_reparse_once(worker_db, keypair[0], fake.lib, 1, sdk=fake)

    assert summary.stale_detected == 1
    assert summary.reparsed == 0
    assert summary.skipped_unchanged == 1
    unchanged = worker_db.query(ArchiveMessage).get(row.id)
    assert unchanged.structured_content == text_only_sc


# ---------------------------------------------------------------------------
# trigger_nested_media_download_scan
# ---------------------------------------------------------------------------


def test_trigger_nested_media_download_scan_invokes_the_existing_script(monkeypatch) -> None:
    calls = []

    class _FakeCompletedProcess:
        returncode = 0

    def _fake_run(cmd, env=None):
        calls.append((cmd, env))
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    exit_code = trigger_nested_media_download_scan()

    assert exit_code == 0
    assert len(calls) == 1
    cmd, _env = calls[0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("download_wecom_media_once.py")


# ---------------------------------------------------------------------------
# CLI main() — dry-run vs apply contract
# ---------------------------------------------------------------------------


def _set_required_env(monkeypatch, tmp_path, private_key) -> None:
    key_path = tmp_path / "private_key.pem"
    write_private_key_pem(private_key, key_path)

    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "secret")
    monkeypatch.setenv("WECOM_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("WECOM_PUBLIC_KEY_VERSION", "1")


def _install_fake_sdk(monkeypatch, fake: FakeWecomSdk) -> None:
    monkeypatch.setattr(reparse_script.wecom_sdk, "load_sdk", fake.load_sdk)
    monkeypatch.setattr(reparse_script.wecom_sdk, "configure_sdk", fake.configure_sdk)
    monkeypatch.setattr(reparse_script.wecom_sdk, "configure_sdk_decrypt_data", fake.configure_sdk_decrypt_data)
    monkeypatch.setattr(reparse_script.wecom_sdk, "new_sdk", fake.new_sdk)
    monkeypatch.setattr(reparse_script.wecom_sdk, "init_sdk", fake.init_sdk)
    monkeypatch.setattr(reparse_script.wecom_sdk, "destroy_sdk", fake.destroy_sdk)
    monkeypatch.setattr(reparse_script.wecom_sdk, "new_slice", fake.new_slice)
    monkeypatch.setattr(reparse_script.wecom_sdk, "free_slice", fake.free_slice)
    monkeypatch.setattr(reparse_script.wecom_sdk, "decrypt_data", fake.decrypt_data)
    monkeypatch.setattr(reparse_script.wecom_sdk, "get_slice_len", fake.get_slice_len)
    monkeypatch.setattr(reparse_script.wecom_sdk, "get_content_from_slice", fake.get_content_from_slice)
    _install_fake_decrypt_isolated(monkeypatch, fake)


def test_main_apply_mode_persists_reparsed_structured_content(monkeypatch, tmp_path, capsys, worker_engine) -> None:
    from sqlalchemy.orm import Session

    priv, pub = generate_test_rsa_keypair()
    _set_required_env(monkeypatch, tmp_path, priv)

    engine = worker_engine
    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        row = _insert_reparseable_row(db, (priv, pub), "payload-1", _FIXED_CHATRECORD_DECRYPTED)
        row_id = row.id

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

    monkeypatch.setattr(reparse_script, "create_engine", lambda _url: engine)
    _install_fake_sdk(monkeypatch, fake)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        reparse_script.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "mode: APPLY" in out
    assert "reparsed: 1" in out
    assert "[PASS] reparse_structured_content_once completed" in out

    with Session(engine) as verify:
        fresh = verify.query(ArchiveMessage).get(row_id)
        assert fresh.structured_content["media_refs"] == [{"path": "0", "type": "image", "sdkfileid": "CtYB..."}]


def test_main_dry_run_mode_persists_nothing(monkeypatch, tmp_path, capsys, worker_engine) -> None:
    from sqlalchemy.orm import Session

    priv, pub = generate_test_rsa_keypair()
    _set_required_env(monkeypatch, tmp_path, priv)

    engine = worker_engine
    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        row = _insert_reparseable_row(db, (priv, pub), "payload-1", _FIXED_CHATRECORD_DECRYPTED)
        row_id = row.id

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

    monkeypatch.setattr(reparse_script, "create_engine", lambda _url: engine)
    _install_fake_sdk(monkeypatch, fake)
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])

    with pytest.raises(SystemExit) as exc:
        reparse_script.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "mode: DRY-RUN" in out

    with Session(engine) as verify:
        fresh = verify.query(ArchiveMessage).get(row_id)
        assert fresh.structured_content == _OLD_BROKEN_STRUCTURED_CONTENT


def test_main_download_flag_triggers_download_scan_only_in_apply_mode(
    monkeypatch, tmp_path, capsys, worker_engine
) -> None:
    from sqlalchemy.orm import Session

    priv, pub = generate_test_rsa_keypair()
    _set_required_env(monkeypatch, tmp_path, priv)

    engine = worker_engine
    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        _insert_reparseable_row(db, (priv, pub), "payload-1", _FIXED_CHATRECORD_DECRYPTED)

    fake = FakeWecomSdk()
    fake.set_decrypt_response("payload-1", _FIXED_CHATRECORD_DECRYPTED)

    monkeypatch.setattr(reparse_script, "create_engine", lambda _url: engine)
    _install_fake_sdk(monkeypatch, fake)

    triggered = []
    monkeypatch.setattr(reparse_script, "trigger_nested_media_download_scan", lambda: triggered.append(True) or 0)
    monkeypatch.setattr(sys, "argv", ["prog", "--download"])

    with pytest.raises(SystemExit):
        reparse_script.main()

    assert triggered == [True]
