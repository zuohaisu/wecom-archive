"""
CLI-equivalence tests for scripts/decrypt_wecom_messages_once.py (RND-222).

Confirms the thin shell (env parsing, tenant resolution, SDK lifecycle,
print/exit-code contract) still behaves exactly as documented after the
core loop moved to app.services.decrypt_worker.run_decrypt_once() —
same [INFO]/[FAIL]/[PASS] output lines, same exit codes — plus the one
declared, in-scope behavior change (RND-222 §2.3): a missing/inactive
TenantWecomConfig for WECOM_CORP_ID now fails the run (fail-fast), where
previously decrypt had no tenant check at all and would scan every
tenant's pending/failed rows.

Run (from backend/):
    pytest tests/test_decrypt_wecom_messages_once_cli.py -v
"""

from __future__ import annotations

import sys

import pytest

from app.services import decrypt_worker as decrypt_worker_module
from app.services.decrypt_isolation import IsolatedDecryptResult
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    generate_test_rsa_keypair,
    insert_archive_message,
    insert_tenant,
    insert_tenant_wecom_config,
    rsa_encrypt_key_b64,
    worker_engine,  # noqa: F401 -- pytest fixture, must be imported to be discovered
    write_private_key_pem,
)


def _install_fake_decrypt_isolated(monkeypatch, fake: FakeWecomSdk) -> None:
    """RND-231: scripts/decrypt_wecom_messages_once.py's main() always
    passes WECOM_SDK_LIB_PATH through to run_decrypt_once() as lib_path,
    which routes DecryptData through
    app.services.decrypt_isolation.decrypt_message_isolated() — a real
    subprocess spawn that loads the SDK fresh and so can never see an
    in-process monkeypatch of app.sdk.wecom_sdk (the CLI shell's own SDK
    lifecycle calls — Init/GetChatData family — still go through that
    module directly and stay faked the old way). This bridges
    decrypt_message_isolated to the SAME FakeWecomSdk instance the test
    already configures via fake.set_decrypt_response(...), so this test
    keeps exercising the CLI shell's [INFO]/[FAIL]/[PASS] contract without
    spawning a real child process or needing a real .so.
    """

    def _fake_decrypt_isolated(lib_path, encrypt_key, encrypt_msg, timeout=15.0):
        ret = fake.decrypt_data(fake.lib, encrypt_key, encrypt_msg, "isolated-slice")
        if ret != 0:
            return IsolatedDecryptResult("sdk_decrypt_failed", ret, None)
        length = fake.get_slice_len(fake.lib, "isolated-slice")
        if length <= 0:
            return IsolatedDecryptResult("sdk_decrypt_failed", ret, None, "empty result")
        content = fake.get_content_from_slice(fake.lib, "isolated-slice")
        return IsolatedDecryptResult("success", ret, content.decode("utf-8"))

    monkeypatch.setattr(decrypt_worker_module, "decrypt_message_isolated", _fake_decrypt_isolated)


def _set_required_env(monkeypatch, tmp_path, private_key) -> None:
    key_path = tmp_path / "private_key.pem"
    write_private_key_pem(private_key, key_path)

    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.setenv("WECOM_ARCHIVE_SECRET", "secret")
    monkeypatch.setenv("WECOM_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("WECOM_PUBLIC_KEY_VERSION", "1")


def test_main_decrypts_pending_row_and_prints_expected_summary(
    monkeypatch, tmp_path, capsys, worker_engine
) -> None:
    import scripts.decrypt_wecom_messages_once as script

    priv, pub = generate_test_rsa_keypair()
    _set_required_env(monkeypatch, tmp_path, priv)

    engine = worker_engine
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        insert_archive_message(
            db,
            tenant_id=_TENANT_A,
            decrypt_status="pending",
            publickey_ver=1,
            encrypt_random_key=rsa_encrypt_key_b64(pub, "sym-key"),
            encrypt_chat_msg="payload-1",
            msgtime=100,
        )

    fake = FakeWecomSdk()
    fake.set_decrypt_response(
        "payload-1",
        {
            "msgtype": "text",
            "from": "staff_a",
            "tolist": ["contact_a"],
            "roomid": "",
            "msgtime": 100,
            "text": {"content": "hi"},
        },
    )

    monkeypatch.setattr(script, "create_engine", lambda _url: engine)
    monkeypatch.setattr(script.wecom_sdk, "load_sdk", fake.load_sdk)
    monkeypatch.setattr(script.wecom_sdk, "configure_sdk", fake.configure_sdk)
    monkeypatch.setattr(
        script.wecom_sdk, "configure_sdk_get_chat_data", fake.configure_sdk_get_chat_data
    )
    monkeypatch.setattr(
        script.wecom_sdk, "configure_sdk_decrypt_data", fake.configure_sdk_decrypt_data
    )
    monkeypatch.setattr(script.wecom_sdk, "new_sdk", fake.new_sdk)
    monkeypatch.setattr(script.wecom_sdk, "init_sdk", fake.init_sdk)
    monkeypatch.setattr(script.wecom_sdk, "destroy_sdk", fake.destroy_sdk)
    monkeypatch.setattr(script.wecom_sdk, "new_slice", fake.new_slice)
    monkeypatch.setattr(script.wecom_sdk, "free_slice", fake.free_slice)
    monkeypatch.setattr(script.wecom_sdk, "decrypt_data", fake.decrypt_data)
    monkeypatch.setattr(script.wecom_sdk, "get_slice_len", fake.get_slice_len)
    monkeypatch.setattr(script.wecom_sdk, "get_content_from_slice", fake.get_content_from_slice)
    _install_fake_decrypt_isolated(monkeypatch, fake)

    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "[INFO] decrypt scanned: 1" in out
    assert "[INFO] decrypt success: 1" in out
    assert "[INFO] decrypt failed: 0" in out
    assert "[INFO] decrypt skipped_unsupported: 0" in out
    assert "[INFO] decrypt pending_remaining: 0" in out
    assert "[PASS] decrypt_wecom_messages_once completed" in out
    assert fake.destroyed is True


def test_main_fails_fast_when_no_active_tenant_config(
    monkeypatch, tmp_path, capsys, worker_engine
) -> None:
    """RND-222 declared behavior change: decrypt now fails fast (like
    sync/media already did) instead of silently scanning every tenant."""
    import scripts.decrypt_wecom_messages_once as script

    priv, _pub = generate_test_rsa_keypair()
    _set_required_env(monkeypatch, tmp_path, priv)

    engine = worker_engine  # no TenantWecomConfig row inserted at all

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
    assert "[FAIL] No active tenant found for this corp" in out


def test_decrypt_worker_symbols_remain_importable_from_the_script() -> None:
    """Backward-compat surface RND-222 must preserve — see
    tests/test_recipient_persistence.py and
    scripts/backfill_revoke_associations_once.py, both of which import
    these names directly from this script module."""
    from scripts.decrypt_wecom_messages_once import (
        _decrypt_message,
        _load_private_key,
        _normalise_fields,
        _rsa_decrypt_encrypt_key,
        _upsert_recipients,
        build_missing_recipient_repair_query,
        repair_missing_recipients,
    )

    assert callable(_decrypt_message)
    assert callable(_load_private_key)
    assert callable(_normalise_fields)
    assert callable(_rsa_decrypt_encrypt_key)
    assert callable(_upsert_recipients)
    assert callable(build_missing_recipient_repair_query)
    assert callable(repair_missing_recipients)
