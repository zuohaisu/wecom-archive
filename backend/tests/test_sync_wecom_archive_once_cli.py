"""
CLI-equivalence tests for scripts/sync_wecom_archive_once.py (RND-222).

Confirms the thin shell resolves its selected tenant's encrypted
TenantWecomConfig before SDK/slice lifecycle and keeps its documented
[INFO]/[FAIL]/[PASS] output lines and exit codes. The historical
``_require_tenant_id`` helper remains importable for non-runtime recovery
scripts, but normal sync no longer selects a tenant from global CorpID.

Run (from backend/):
    pytest tests/test_sync_wecom_archive_once_cli.py -v
"""

from __future__ import annotations

import sys

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, SyncState
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    insert_tenant,
    insert_tenant_wecom_config,
    worker_engine,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)


_FIELD_KEY = Fernet.generate_key().decode("ascii")


def _set_required_env(monkeypatch, tenant_id: str = _TENANT_A) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_TENANT_ID", tenant_id)
    # Ambient legacy values must not select credentials for this shell.
    monkeypatch.setenv("WECOM_CORP_ID", "ambient-oauth-corp")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "ambient-legacy-secret")


def _install_fake_sdk(monkeypatch, script, fake: FakeWecomSdk) -> None:
    for name in (
        "load_sdk",
        "configure_sdk",
        "configure_sdk_get_chat_data",
        "new_sdk",
        "init_sdk",
        "destroy_sdk",
        "new_slice",
        "free_slice",
        "get_chat_data",
        "get_slice_len",
        "get_content_from_slice",
    ):
        monkeypatch.setattr(script.wecom_sdk, name, getattr(fake, name))


def test_main_syncs_new_records_and_prints_expected_summary(
    monkeypatch, capsys, worker_engine
) -> None:
    import scripts.sync_wecom_archive_once as script

    _set_required_env(monkeypatch)

    engine = worker_engine
    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        config = insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        config.set_app_secret("secret")
        db.commit()

    fake = FakeWecomSdk()
    fake.set_chat_data(
        [
            {
                "msgid": "m1",
                "seq": 10,
                "publickey_ver": 1,
                "encrypt_random_key": "rk",
                "encrypt_chat_msg": "cm",
            }
        ],
        ret=0,
    )

    monkeypatch.setattr(script, "create_engine", lambda _url: engine)
    _install_fake_sdk(monkeypatch, script, fake)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "[INFO] return_code: 0" in out
    assert "[INFO] record_count: 1" in out
    assert "[INFO] inserted: 1" in out
    assert "[INFO] skipped_duplicate: 0" in out
    assert "[INFO] previous_seq: 0" in out
    assert "[INFO] new_seq: 10" in out
    assert "[PASS] sync_wecom_archive_once completed successfully" in out
    assert fake.destroyed is True

    with Session(engine) as db:
        assert db.query(ArchiveMessage).filter_by(tenant_id=_TENANT_A).count() == 1
        state = db.query(SyncState).filter_by(tenant_id=_TENANT_A, corp_id="corp1").one()
        assert state.last_seq == 10


def test_main_fails_when_get_chat_data_returns_nonzero(
    monkeypatch, capsys, worker_engine
) -> None:
    import scripts.sync_wecom_archive_once as script

    _set_required_env(monkeypatch)

    engine = worker_engine
    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        config = insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        config.set_app_secret("secret")
        db.commit()

    fake = FakeWecomSdk()
    fake.set_chat_data([], ret=90002)

    monkeypatch.setattr(script, "create_engine", lambda _url: engine)
    _install_fake_sdk(monkeypatch, script, fake)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "[INFO] return_code: 90002" in out
    assert "[FAIL] GetChatData returned code 90002" in out


def test_main_fails_fast_when_no_active_tenant_config(monkeypatch, capsys, worker_engine) -> None:
    import scripts.sync_wecom_archive_once as script

    _set_required_env(monkeypatch)
    engine = worker_engine  # no TenantWecomConfig row inserted

    monkeypatch.setattr(script, "create_engine", lambda _url: engine)
    monkeypatch.setattr(
        script.wecom_sdk,
        "load_sdk",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not load SDK")),
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "sync_worker error_class=tenant_config_unavailable" in out


def test_sync_worker_symbols_remain_importable_from_the_script() -> None:
    """Backward-compat surface RND-222 must preserve — see
    tests/test_tenant_sync_state.py and tests/test_tenant_foundation.py,
    both of which import these names directly from this script module."""
    from scripts.sync_wecom_archive_once import _read_seq, _require_tenant_id, _upsert_seq

    assert callable(_read_seq)
    assert callable(_require_tenant_id)
    assert callable(_upsert_seq)
