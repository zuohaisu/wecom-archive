"""RND-387 per-tenant archive worker contracts.

Loop mode runs the sync+decrypt chain once per active tenant with a merged
per-tenant child environment; one tenant's failure never aborts the others
and the media wake-up stays per-tenant.  WECOM_TENANT_ID hard-fails for one
explicit tenant; WECOM_CORP_ID keeps the legacy chain byte-identical.
The decrypt shell falls back to the stored per-tenant RSA key when no key
provider can serve one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from sqlalchemy.orm import Session

from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    _TENANT_B,
    generate_test_rsa_keypair,
    insert_tenant,
    insert_tenant_wecom_config,
    worker_engine,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)

ARCHIVE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_archive_worker_once.py"
DECRYPT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/decrypt_wecom_messages_once.py"
SYNC_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_wecom_archive_once.py"
_SECRET = "rnd387-archive-secret"
_SECRET_B = "rnd387-archive-secret-b"
_FIELD_KEY = Fernet.generate_key().decode("ascii")


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _worker_module():
    return _module(ARCHIVE_SCRIPT, "archive_worker_rnd387")


def _encrypted_config(db, tenant_id: str, corp_id: str, secret: str) -> None:
    config = insert_tenant_wecom_config(db, tenant_id, corp_id)
    config.set_app_secret(secret)
    config.publickey_version = 3
    db.commit()


@pytest.fixture()
def field_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)


# ---------------------------------------------------------------------------
# run_archive_worker_once.py — loop mode
# ---------------------------------------------------------------------------


def _install_loop_fixtures(monkeypatch, tmp_path, engine):
    """Shared env + mocks for loop-mode main() runs."""
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.delenv("WECOM_TENANT_ID", raising=False)
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    worker = _worker_module()
    monkeypatch.setattr(worker, "_tenant_engine", lambda: engine)
    script_calls: list[tuple[str, str, dict, str]] = []
    monkeypatch.setattr(
        worker,
        "_run_script_env",
        lambda _path, label, env, tag: script_calls.append((label, env, tag)) or True,
    )
    reachability_envs: list[dict] = []
    monkeypatch.setattr(
        worker,
        "_run_best_effort_reachability_automation",
        lambda env=None: reachability_envs.append(env) if env is not None else reachability_envs.append(None),
    )
    media_tenants: list[str | None] = []
    monkeypatch.setattr(
        worker,
        "_request_media_worker_after_archive",
        lambda tenant_id=None: media_tenants.append(tenant_id) or worker.MediaWorkerDispatch.ACCEPTED,
    )
    return worker, script_calls, reachability_envs, media_tenants


def test_loop_runs_each_tenant_with_merged_credentials_env(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant(db, _TENANT_B)
        _encrypted_config(db, _TENANT_A, "corp-a", _SECRET)
        _encrypted_config(db, _TENANT_B, "corp-b", _SECRET_B)

    worker, script_calls, reachability_envs, media_tenants = _install_loop_fixtures(
        monkeypatch, tmp_path, worker_engine
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    assert [label for label, _env, _tag in script_calls] == [
        "sync_wecom_archive_once.py",
        "decrypt_wecom_messages_once.py",
        "sync_wecom_archive_once.py",
        "decrypt_wecom_messages_once.py",
    ]
    env_a = script_calls[0][1]
    assert env_a["WECOM_TENANT_ID"] == _TENANT_A
    assert env_a["WECOM_CORP_ID"] == "corp-a"
    assert env_a["WECOM_ARCHIVE_SECRET"] == _SECRET
    assert env_a["WECOM_PUBLIC_KEY_VERSION"] == "3"
    env_b = script_calls[2][1]
    assert env_b["WECOM_TENANT_ID"] == _TENANT_B
    assert env_b["WECOM_ARCHIVE_SECRET"] == _SECRET_B
    # Reachability runs once per tenant with that tenant's env.
    assert [e["WECOM_TENANT_ID"] for e in reachability_envs] == [_TENANT_A, _TENANT_B]
    assert media_tenants == [_TENANT_A, _TENANT_B]
    out = capsys.readouterr().out
    assert "lifecycle=ended result=completed exit_code=0 error_class=none" in out


def test_loop_one_tenant_failure_continues_and_media_stays_per_successful_tenant(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant(db, _TENANT_B)
        _encrypted_config(db, _TENANT_A, "corp-a", _SECRET)
        _encrypted_config(db, _TENANT_B, "corp-b", _SECRET_B)

    worker = _worker_module()
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.delenv("WECOM_TENANT_ID", raising=False)
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.setattr(worker, "_tenant_engine", lambda: worker_engine)

    from app.services.tenant_credentials import tenant_log_tag

    tag_a = tenant_log_tag(_TENANT_A)

    def _run_script_env(script_path, label, env, tag) -> bool:
        if tag == tag_a and label.startswith("sync"):
            print(
                f"[FAIL] archive_worker tenant={tag} error_class=child_worker_failed "
                "child_exit_code=1",
                flush=True,
            )
            return False
        return True

    monkeypatch.setattr(worker, "_run_script_env", _run_script_env)
    media_tenants: list[str | None] = []
    monkeypatch.setattr(
        worker,
        "_request_media_worker_after_archive",
        lambda tenant_id=None: media_tenants.append(tenant_id) or worker.MediaWorkerDispatch.ACCEPTED,
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    # Tenant A's sync failure logged as a child failure; media only for B.
    out = capsys.readouterr().out
    assert "error_class=child_worker_failed" in out
    assert media_tenants == [_TENANT_B]


def test_loop_all_tenants_failed_exits_1_without_media(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        _encrypted_config(db, _TENANT_A, "corp-a", _SECRET)

    worker = _worker_module()
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.delenv("WECOM_TENANT_ID", raising=False)
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.setattr(worker, "_tenant_engine", lambda: worker_engine)
    monkeypatch.setattr(worker, "_run_script_env", lambda _path, _label, _env, _tag: False)
    media_tenants: list[object] = []
    monkeypatch.setattr(
        worker,
        "_request_media_worker_after_archive",
        lambda tenant_id=None: media_tenants.append(tenant_id) or worker.MediaWorkerDispatch.ACCEPTED,
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 1
    out = capsys.readouterr().out
    assert "lifecycle=ended result=failed exit_code=1 error_class=all_tenants_failed" in out
    assert media_tenants == []


def test_loop_zero_active_tenants_is_a_success_noop(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    worker, _script_calls, _reachability_envs, media_tenants = _install_loop_fixtures(
        monkeypatch, tmp_path, worker_engine
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    out = capsys.readouterr().out
    assert "tenant_count=0 trigger=no-active-tenants" in out
    assert media_tenants == []


def test_loop_legacy_credentials_skip_tenant_and_continue(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant(db, _TENANT_B)
        # Tenant A's row keeps the plaintext "secret" from the insert helper
        # and must be classified as historical/non-Fernet format.
        insert_tenant_wecom_config(db, _TENANT_A, "corp-a")
        _encrypted_config(db, _TENANT_B, "corp-b", _SECRET_B)

    worker, script_calls, _reachability_envs, media_tenants = _install_loop_fixtures(
        monkeypatch, tmp_path, worker_engine
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    from app.services.tenant_credentials import tenant_log_tag

    tag_a = tenant_log_tag(_TENANT_A)
    out = capsys.readouterr().out
    assert (
        f"[WARN] archive_worker tenant={tag_a} trigger=skipped-failed "
        "error_class=tenant_credentials_legacy_format" in out
    )
    # Only tenant B's chain ran; media wakes only B.
    assert [env["WECOM_TENANT_ID"] for label, env, _tag in script_calls if label.startswith("sync")] == [
        _TENANT_B
    ]
    assert media_tenants == [_TENANT_B]


def test_single_tenant_chain_hard_fails_when_config_missing(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        _encrypted_config(db, _TENANT_A, "corp-a", _SECRET)

    worker = _worker_module()
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_TENANT_ID", "tenant-unknown")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setattr(worker, "_tenant_engine", lambda: worker_engine)

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 1
    out = capsys.readouterr().out
    assert "error_class=tenant_config_unavailable" in out
    assert "Tenant archive credentials are unavailable" in out


def test_single_tenant_chain_hard_fails_on_legacy_credentials(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp-a")

    worker = _worker_module()
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_TENANT_ID", _TENANT_A)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setattr(worker, "_tenant_engine", lambda: worker_engine)

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 1
    out = capsys.readouterr().out
    assert "error_class=tenant_credentials_legacy_format" in out


def test_legacy_env_chain_unchanged(
    monkeypatch, tmp_path, worker_engine, capsys, field_encryption_key
) -> None:
    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        _encrypted_config(db, _TENANT_A, "corp1", _SECRET)

    worker = _worker_module()
    monkeypatch.setenv("WORKER_LOCK_PATH", str(tmp_path / "archive.lock"))
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_CORP_ID", "corp1")
    monkeypatch.delenv("WECOM_TENANT_ID", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    calls: list[str] = []
    monkeypatch.setattr(worker, "_run_script", lambda _path, label: calls.append(label))
    reachability_calls: list[object] = []
    monkeypatch.setattr(
        worker,
        "_run_best_effort_reachability_automation",
        lambda: reachability_calls.append("diagnostic"),
    )
    media_calls: list[object] = []
    monkeypatch.setattr(
        worker,
        "_request_media_worker_after_archive",
        lambda: media_calls.append("media") or worker.MediaWorkerDispatch.ACCEPTED,
    )

    with pytest.raises(SystemExit) as result:
        worker.main()

    assert result.value.code == 0
    assert calls == ["sync_wecom_archive_once.py", "decrypt_wecom_messages_once.py"]
    assert reachability_calls == ["diagnostic"]
    assert media_calls == ["media"]


# ---------------------------------------------------------------------------
# decrypt shell — per-tenant stored RSA key fallback
# ---------------------------------------------------------------------------


def _install_fake_sdk(monkeypatch, script, fake: FakeWecomSdk) -> None:
    for name in (
        "load_sdk",
        "configure_sdk",
        "configure_sdk_get_chat_data",
        "configure_sdk_decrypt_data",
        "new_sdk",
        "init_sdk",
        "destroy_sdk",
        "new_slice",
        "free_slice",
        "decrypt_data",
        "get_slice_len",
        "get_content_from_slice",
    ):
        monkeypatch.setattr(script.wecom_sdk, name, getattr(fake, name))


def test_decrypt_per_tenant_uses_stored_rsa_key_when_provider_fails(
    monkeypatch, worker_engine, capsys
) -> None:
    import scripts.decrypt_wecom_messages_once as script

    priv, pub = generate_test_rsa_keypair()
    stored_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_TENANT_ID", _TENANT_A)
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_PUBLIC_KEY_VERSION", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")

    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        config = insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        config.set_credentials(_SECRET, stored_pem)
        config.publickey_version = 2
        db.commit()

    fake = FakeWecomSdk()
    captured: dict[str, object] = {}

    def _capture_decrypt(session, tenant_id, lib, private_key, expected_pubkey_ver, **kwargs):
        captured["private_key"] = private_key
        captured["tenant_id"] = tenant_id
        captured["expected_pubkey_ver"] = expected_pubkey_ver
        return SimpleNamespace(
            scanned=0,
            success=0,
            failed=0,
            unsupported=0,
            pending_remaining=0,
            external_contact_refresh_enqueued=False,
            key_mismatch=False,
            rsa_failed=False,
            sigsegv=False,
            isolation_other=False,
            malformed_input=False,
            recipient_upsert_failed=False,
            recipients_repaired=False,
            revoke_event_seen=False,
            revoke_reconcile_failed=False,
            revocations_reconciled=False,
            return_codes=None,
        )

    monkeypatch.setattr(script, "create_engine", lambda _url: worker_engine)
    monkeypatch.setattr(script, "get_key_provider", lambda _session, **kwargs: (_ for _ in ()).throw(script.KeyProviderError("no key")))
    monkeypatch.setattr(script, "run_decrypt_once", _capture_decrypt)
    _install_fake_sdk(monkeypatch, script, fake)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    # The stored PEM was parsed and handed to the decrypt loop.
    private_key = captured["private_key"]
    assert private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode() == stored_pem
    assert captured["tenant_id"] == _TENANT_A
    assert captured["expected_pubkey_ver"] == 2
    assert fake.init_calls == [("corp1", _SECRET)]
    assert "[INFO] decrypt scanned: 0" in capsys.readouterr().out


def test_decrypt_per_tenant_still_fails_when_no_stored_key(
    monkeypatch, worker_engine, capsys
) -> None:
    import scripts.decrypt_wecom_messages_once as script

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_TENANT_ID", _TENANT_A)
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("WECOM_PUBLIC_KEY_VERSION", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")

    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        _encrypted_config(db, _TENANT_A, "corp1", _SECRET)

    monkeypatch.setattr(script, "create_engine", lambda _url: worker_engine)
    monkeypatch.setattr(script, "get_key_provider", lambda _session, **kwargs: (_ for _ in ()).throw(script.KeyProviderError("no key")))
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 1
    assert "Private key retrieval failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# sync shell — per-tenant mode
# ---------------------------------------------------------------------------


def test_sync_per_tenant_reads_credentials_from_config_row(
    monkeypatch, worker_engine, capsys
) -> None:
    import scripts.sync_wecom_archive_once as script

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _FIELD_KEY)
    monkeypatch.setenv("WECOM_TENANT_ID", _TENANT_B)
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")

    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_B)
        _encrypted_config(db, _TENANT_B, "corp-b", _SECRET_B)

    captured: dict[str, object] = {}

    def _capture_sync(session, tenant_id, corp_id, lib, handle, slice_ptr, limit):
        captured["tenant_id"] = tenant_id
        captured["corp_id"] = corp_id
        return MagicMock(return_code=0, record_count=0, inserted=0, skipped_duplicate=0, previous_seq=0, new_seq=0)

    fake = FakeWecomSdk()
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
    monkeypatch.setattr(script, "create_engine", lambda _url: worker_engine)
    monkeypatch.setattr(script, "run_sync_once", _capture_sync)
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0
    assert captured == {"tenant_id": _TENANT_B, "corp_id": "corp-b"}
    assert fake.init_calls == [("corp-b", _SECRET_B)]
    assert "[PASS] sync_wecom_archive_once completed successfully" in capsys.readouterr().out
