"""
Tests for RND-201 round 2 QA fix — historical revoke recovery in
scripts/backfill_revoke_associations_once.py.

A revoke row decrypted before RND-201's revoke parser existed has
structured_content = NULL (the old parser returned None unconditionally
for msgtype="revoke"), so its pre_msgid was never extracted or
persisted. Round 1's backfill reconciled such rows directly, which
permanently classified them "malformed" even though the association was
actually recoverable from the still-retained encrypted envelope. This
file covers the re-decrypt recovery pass added to fix that.
"""

from __future__ import annotations

import scripts.backfill_revoke_associations_once as backfill_script
from app.db.models import ArchiveMessage, MessageRevocation
from scripts.backfill_revoke_associations_once import (
    find_historical_undecrypted_revoke_events,
    main,
)
from tests.test_reachability_audit import _TENANT_A, _TENANT_B, _insert_message, db  # noqa: F401 -- pytest fixture, must be imported to be discovered


def _insert_historical_revoke_row(db, *, tenant_id=_TENANT_A, decrypt_status="success", **kwargs):
    """A revoke row exactly as the pre-RND-201 decrypt pipeline would
    have left it: decrypted successfully, but structured_content NULL
    (never extracted) -- distinct from _insert_revoke_event() in the
    sibling test file, which always sets structured_content already."""
    defaults = dict(
        msgtype="revoke",
        structured_content=None,
        decrypt_status=decrypt_status,
        tenant_id=tenant_id,
        encrypt_random_key="encrypted-key-blob",
        encrypt_chat_msg="encrypted-msg-blob",
    )
    defaults.update(kwargs)
    return _insert_message(db, **defaults)


class _FakePrivateKey:
    pass


class _FakeLib:
    pass


def _patch_sdk_env(monkeypatch, expected_pubkey_ver=1):
    """Bypass real WeCom SDK/RSA calls entirely -- tests exercise
    main()'s orchestration logic (which candidates get recovered, how
    outcomes are counted/reported, dry-run vs apply), not the RSA/ctypes
    plumbing itself (that's scripts/decrypt_wecom_messages_once.py's own
    existing coverage, reused verbatim here via import, not
    reimplemented)."""
    monkeypatch.setattr(
        backfill_script,
        "_load_optional_sdk_env",
        lambda: (_FakePrivateKey(), _FakeLib(), object(), expected_pubkey_ver, "fake-lib-path"),
    )


# ---------------------------------------------------------------------------
# find_historical_undecrypted_revoke_events — candidate query
# ---------------------------------------------------------------------------


def test_finds_revoke_row_with_null_structured_content(db) -> None:
    row = _insert_historical_revoke_row(db, seq=1)
    candidates = find_historical_undecrypted_revoke_events(db).all()
    assert [c.id for c in candidates] == [row.id]


def test_excludes_row_that_already_has_structured_content(db) -> None:
    _insert_historical_revoke_row(
        db,
        structured_content={"fields": {"pre_msgid": "x"}, "raw": {}, "parse_warnings": []},
        seq=1,
    )
    assert find_historical_undecrypted_revoke_events(db).all() == []


def test_excludes_non_revoke_and_undecrypted_rows(db) -> None:
    _insert_message(db, msgtype="text", content_text="hi", seq=1, tenant_id=_TENANT_A)
    _insert_historical_revoke_row(db, decrypt_status="pending", seq=2)
    assert find_historical_undecrypted_revoke_events(db).all() == []


def test_respects_tenant_scope(db) -> None:
    row_a = _insert_historical_revoke_row(db, tenant_id=_TENANT_A, seq=1)
    _insert_historical_revoke_row(db, tenant_id=_TENANT_B, seq=2)
    candidates = find_historical_undecrypted_revoke_events(db, _TENANT_A).all()
    assert [c.id for c in candidates] == [row_a.id]


# ---------------------------------------------------------------------------
# recover_historical_revoke_structured_content — pure re-decrypt function
# ---------------------------------------------------------------------------


def test_recover_returns_structured_content_on_success(db, monkeypatch) -> None:
    row = _insert_historical_revoke_row(db, seq=1, publickey_ver=1)
    monkeypatch.setattr(backfill_script, "_rsa_decrypt_encrypt_key", lambda *a: "plain-key")
    monkeypatch.setattr(
        backfill_script, "_decrypt_message", lambda *a, **k: (0, '{"msgtype":"revoke","revoke":{"pre_msgid":"orig-1"}}')
    )
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row
    )
    assert outcome == "recovered"
    assert structured_content["fields"] == {"pre_msgid": "orig-1"}


def test_recover_reports_key_mismatch_without_attempting_decrypt(db, monkeypatch) -> None:
    row = _insert_historical_revoke_row(db, seq=1, publickey_ver=2)
    called = {"n": 0}
    monkeypatch.setattr(
        backfill_script, "_rsa_decrypt_encrypt_key", lambda *a: called.__setitem__("n", called["n"] + 1)
    )
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row  # expected version 1, row has 2
    )
    assert outcome == "key_mismatch"
    assert structured_content is None
    assert called["n"] == 0  # never even attempted RSA decrypt


def test_recover_reports_rsa_failure(db, monkeypatch) -> None:
    row = _insert_historical_revoke_row(db, seq=1, publickey_ver=1)
    monkeypatch.setattr(backfill_script, "_rsa_decrypt_encrypt_key", lambda *a: None)
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row
    )
    assert outcome == "rsa_failed"
    assert structured_content is None


def test_recover_reports_sdk_decrypt_failure(db, monkeypatch) -> None:
    row = _insert_historical_revoke_row(db, seq=1, publickey_ver=1)
    monkeypatch.setattr(backfill_script, "_rsa_decrypt_encrypt_key", lambda *a: "plain-key")
    monkeypatch.setattr(backfill_script, "_decrypt_message", lambda *a, **k: (90002, None))
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row
    )
    assert outcome == "sdk_decrypt_failed"
    assert structured_content is None


def test_recover_reports_invalid_json(db, monkeypatch) -> None:
    row = _insert_historical_revoke_row(db, seq=1, publickey_ver=1)
    monkeypatch.setattr(backfill_script, "_rsa_decrypt_encrypt_key", lambda *a: "plain-key")
    monkeypatch.setattr(backfill_script, "_decrypt_message", lambda *a, **k: (0, "{not valid json"))
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row
    )
    assert outcome == "invalid_json"
    assert structured_content is None


def test_recover_reports_missing_envelope() -> None:
    row = ArchiveMessage(
        msgid="m", seq=1, publickey_ver=1, encrypt_random_key="", encrypt_chat_msg="",
        msgtype="revoke", decrypt_status="success",
    )
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row
    )
    assert outcome == "missing_envelope"
    assert structured_content is None


def test_recover_never_fabricates_pre_msgid_when_decrypted_payload_lacks_revoke_key(db, monkeypatch) -> None:
    """A malformed/legacy decrypted payload with no revoke.pre_msgid at
    all -- recovery must report it via the normal 'recovered' + fields=
    None path (parse_structured_content's own missing_pre_msgid warning),
    never invent a value."""
    row = _insert_historical_revoke_row(db, seq=1, publickey_ver=1)
    monkeypatch.setattr(backfill_script, "_rsa_decrypt_encrypt_key", lambda *a: "plain-key")
    monkeypatch.setattr(backfill_script, "_decrypt_message", lambda *a, **k: (0, '{"msgtype":"revoke","revoke":{}}'))
    outcome, structured_content = backfill_script.recover_historical_revoke_structured_content(
        _FakePrivateKey(), _FakeLib(), 1, row
    )
    assert outcome == "recovered"
    assert structured_content["fields"] is None
    assert structured_content["parse_warnings"] == ["missing_pre_msgid"]


# ---------------------------------------------------------------------------
# _load_optional_sdk_env — graceful skip when not configured
# ---------------------------------------------------------------------------


def test_load_optional_sdk_env_returns_none_when_unconfigured(monkeypatch) -> None:
    for name in (
        "WECOM_SDK_LIB_PATH", "WECOM_CORP_ID", "WECOM_ARCHIVE_SECRET",
        "WECOM_PRIVATE_KEY_PATH", "WECOM_PUBLIC_KEY_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    assert backfill_script._load_optional_sdk_env() is None


def test_load_optional_sdk_env_returns_none_on_partial_configuration(monkeypatch) -> None:
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/nonexistent/lib.so")
    monkeypatch.setenv("WECOM_CORP_ID", "dev-corp")
    for name in ("WECOM_ARCHIVE_SECRET", "WECOM_PRIVATE_KEY_PATH", "WECOM_PUBLIC_KEY_VERSION"):
        monkeypatch.delenv(name, raising=False)
    assert backfill_script._load_optional_sdk_env() is None


# ---------------------------------------------------------------------------
# main() end-to-end -- recovery integrated into the full backfill run
# ---------------------------------------------------------------------------


def _sqlite_url(tmp_path, name="historical.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _make_sqlite_db_with_historical_row(tmp_path, name="historical.db"):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from tests.test_reachability_audit import _SCHEMA_SQL

    url = _sqlite_url(tmp_path, name)
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    session = Session(engine)
    session.add(ArchiveMessage(
        msgid="orig-1", seq=1, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="text", content_text="original content",
        sender="staff_a", roomid="room1", msgtime=1000, tenant_id=_TENANT_A,
    ))
    session.add(ArchiveMessage(
        msgid="historical-revoke-1", seq=2, publickey_ver=1, encrypt_random_key="k-blob",
        encrypt_chat_msg="c-blob", decrypt_status="success", msgtype="revoke",
        structured_content=None, sender="staff_a", roomid="room1", msgtime=2000,
        tenant_id=_TENANT_A,
    ))
    session.commit()
    session.close()
    engine.dispose()
    return url


def test_main_recovers_historical_row_and_links_it_on_apply(tmp_path, monkeypatch, capsys) -> None:
    url = _make_sqlite_db_with_historical_row(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("recovered", {"fields": {"pre_msgid": "orig-1"}, "raw": {}, "parse_warnings": []}),
    )

    import pytest
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "historical_recovered: 1" in out
    assert "linked: 1" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revoke_row = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "historical-revoke-1").one()
    original = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-1").one()
    assert revoke_row.structured_content["fields"] == {"pre_msgid": "orig-1"}
    assert original.is_revoked is True
    revocation = session.query(MessageRevocation).one()
    assert revocation.status == "linked"
    assert revocation.target_msgid == "orig-1"
    session.close()


def test_main_dry_run_recovery_does_not_persist_structured_content(tmp_path, monkeypatch, capsys) -> None:
    url = _make_sqlite_db_with_historical_row(tmp_path, "dryrun.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py"])
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("recovered", {"fields": {"pre_msgid": "orig-1"}, "raw": {}, "parse_warnings": []}),
    )

    import pytest
    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr().out
    assert "mode: DRY-RUN" in out
    assert "historical_recovered: 1" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revoke_row = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "historical-revoke-1").one()
    original = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-1").one()
    assert revoke_row.structured_content is None  # nothing written
    assert original.is_revoked is False
    assert session.query(MessageRevocation).count() == 0
    session.close()


def test_main_preserves_all_other_columns_on_the_recovered_row(tmp_path, monkeypatch) -> None:
    """Recovery must touch ONLY structured_content -- every other column
    on the historical revoke row itself stays byte-identical."""
    url = _make_sqlite_db_with_historical_row(tmp_path, "preserve.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("recovered", {"fields": {"pre_msgid": "orig-1"}, "raw": {}, "parse_warnings": []}),
    )

    import pytest
    with pytest.raises(SystemExit):
        main()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revoke_row = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "historical-revoke-1").one()
    assert revoke_row.sender == "staff_a"
    assert revoke_row.roomid == "room1"
    assert revoke_row.msgtime == 2000
    assert revoke_row.seq == 2
    assert revoke_row.decrypt_status == "success"
    assert revoke_row.encrypt_random_key == "k-blob"
    assert revoke_row.encrypt_chat_msg == "c-blob"
    session.close()


def test_main_recovery_failure_leaves_row_untouched_and_retryable(tmp_path, monkeypatch, capsys) -> None:
    url = _make_sqlite_db_with_historical_row(tmp_path, "failure.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("rsa_failed", None),
    )

    import pytest
    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr().out
    assert "historical_recovery_failed: 1" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revoke_row = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "historical-revoke-1").one()
    assert revoke_row.structured_content is None  # never fabricated
    # No MessageRevocation row was created for this event either -- a
    # failed recovery attempt must NOT fall through to reconciliation and
    # create a premature "malformed" row, which (being terminal) would
    # permanently block a later successful recovery from taking effect
    # (see find_unreconciled_revoke_events()'s docstring).
    assert session.query(MessageRevocation).count() == 0
    # Still eligible for a future retry (e.g. once real WeCom credentials
    # are fixed) -- the candidate query still matches it.
    remaining = find_historical_undecrypted_revoke_events(session).all()
    assert [r.id for r in remaining] == [revoke_row.id]
    session.close()


def test_a_row_that_fails_recovery_then_succeeds_on_a_later_run_still_links(tmp_path, monkeypatch, capsys) -> None:
    """The exact regression this fix targets: run 1 has bad/unavailable
    SDK credentials and fails to recover a historical row; run 2 (e.g.
    after an operator fixes the credentials) succeeds. The row must
    still end up correctly linked -- run 1's failure must not have
    permanently poisoned it with a terminal "malformed" association."""
    import pytest

    url = _make_sqlite_db_with_historical_row(tmp_path, "eventual_success.db")
    monkeypatch.setenv("DATABASE_URL", url)

    # --- Run 1: recovery fails ---
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("sdk_decrypt_failed", None),
    )
    with pytest.raises(SystemExit):
        main()
    capsys.readouterr()

    # --- Run 2: recovery succeeds ---
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("recovered", {"fields": {"pre_msgid": "orig-1"}, "raw": {}, "parse_warnings": []}),
    )
    with pytest.raises(SystemExit):
        main()
    out = capsys.readouterr().out
    assert "historical_recovered: 1" in out
    assert "linked: 1" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revoke_row = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "historical-revoke-1").one()
    original = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "orig-1").one()
    assert revoke_row.structured_content["fields"] == {"pre_msgid": "orig-1"}
    assert original.is_revoked is True
    revocation = session.query(MessageRevocation).one()
    assert revocation.status == "linked"
    session.close()


def test_a_row_with_genuinely_no_pre_msgid_in_its_real_payload_becomes_malformed_once(
    tmp_path, monkeypatch, capsys
) -> None:
    """Distinguish "recovery mechanically failed" (retryable, tested
    above) from "recovery succeeded but the real WeCom payload genuinely
    has no pre_msgid" (a real malformed event, not a recovery gap) --
    the latter must still resolve to "malformed" and stop being rescanned
    by the historical-recovery pass, exactly like a live-pipeline
    malformed revoke event would."""
    import pytest

    url = _make_sqlite_db_with_historical_row(tmp_path, "genuinely_malformed.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("recovered", {"fields": None, "raw": {}, "parse_warnings": ["missing_pre_msgid"]}),
    )
    with pytest.raises(SystemExit):
        main()
    out = capsys.readouterr().out
    assert "historical_recovered: 1" in out
    assert "malformed: 1" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revocation = session.query(MessageRevocation).one()
    assert revocation.status == "malformed"
    # No longer a historical-recovery candidate -- structured_content is
    # now a real (non-NULL) dict, so re-running finds nothing left to do.
    assert find_historical_undecrypted_revoke_events(session).all() == []
    session.close()


def test_main_recovery_key_mismatch_is_reported_separately_from_generic_failure(tmp_path, monkeypatch, capsys) -> None:
    url = _make_sqlite_db_with_historical_row(tmp_path, "keymismatch.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    _patch_sdk_env(monkeypatch, expected_pubkey_ver=99)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("key_mismatch", None),
    )

    import pytest
    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr().out
    assert "historical_recovery_key_mismatch: 1" in out
    assert "historical_recovery_failed" not in out


def test_main_skips_recovery_entirely_without_sdk_credentials(tmp_path, monkeypatch, capsys) -> None:
    url = _make_sqlite_db_with_historical_row(tmp_path, "nosdk.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr("sys.argv", ["backfill_revoke_associations_once.py", "--apply"])
    for name in (
        "WECOM_SDK_LIB_PATH", "WECOM_CORP_ID", "WECOM_ARCHIVE_SECRET",
        "WECOM_PRIVATE_KEY_PATH", "WECOM_PUBLIC_KEY_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)

    import pytest
    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr().out
    assert "historical_recovery_skipped_no_sdk: 1" in out

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    session = Session(engine)
    revoke_row = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "historical-revoke-1").one()
    assert revoke_row.structured_content is None
    session.close()


def test_main_skip_historical_recovery_flag_bypasses_recovery_even_with_sdk_configured(
    tmp_path, monkeypatch, capsys
) -> None:
    url = _make_sqlite_db_with_historical_row(tmp_path, "skipflag.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(
        "sys.argv", ["backfill_revoke_associations_once.py", "--apply", "--skip-historical-recovery"]
    )
    _patch_sdk_env(monkeypatch)
    # If recovery were attempted, this would raise -- proves it never runs.
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("recovery should not run")),
    )

    import pytest
    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr().out
    assert "historical_candidates_scanned: 0" in out
    assert "historical_recovered: 0" in out


def test_main_tenant_scoped_recovery_does_not_touch_other_tenants(tmp_path, monkeypatch) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from tests.test_reachability_audit import _SCHEMA_SQL

    url = _sqlite_url(tmp_path, "tenant_scoped_recovery.db")
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    session = Session(engine)
    session.add(ArchiveMessage(
        msgid="revoke-a", seq=1, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke", structured_content=None, tenant_id=_TENANT_A,
    ))
    session.add(ArchiveMessage(
        msgid="revoke-b", seq=1, publickey_ver=1, encrypt_random_key="k", encrypt_chat_msg="c",
        decrypt_status="success", msgtype="revoke", structured_content=None, tenant_id=_TENANT_B,
    ))
    session.commit()
    session.close()
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_revoke_associations_once.py", "--apply", "--tenant-id", _TENANT_A],
    )
    _patch_sdk_env(monkeypatch)
    monkeypatch.setattr(
        backfill_script,
        "recover_historical_revoke_structured_content",
        lambda *a, **k: ("recovered", {"fields": {"pre_msgid": "never-exists"}, "raw": {}, "parse_warnings": []}),
    )

    import pytest
    with pytest.raises(SystemExit):
        main()

    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import Session as _Session

    engine = _create_engine(url)
    session = _Session(engine)
    row_a = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "revoke-a").one()
    row_b = session.query(ArchiveMessage).filter(ArchiveMessage.msgid == "revoke-b").one()
    assert row_a.structured_content is not None  # recovered
    assert row_b.structured_content is None  # untouched -- different tenant
    session.close()
