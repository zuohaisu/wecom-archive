"""
CLI-equivalence test for scripts/download_wecom_media_once.py (RND-222).

The pre-existing tests/test_download_wecom_media_once.py and
tests/test_qiniu_worker_integration.py already extensively cover this
shell's main()/_run() CLI behavior via MagicMock sessions, and both suites
pass unmodified against the thinned shell (proof that download_one/
get_or_reset_media_file/select_candidates/select_nested_media_candidates
are still invoked exactly as before) — see app/services/media_worker.py's
module docstring for why candidate selection deliberately stays in this
script rather than moving into the service. This file adds one additional
full-stack smoke test using the real sqlite engine + FakeWecomSdk
infrastructure shared with the sync/decrypt CLI-equivalence tests, for an
end-to-end integration proof independent of MagicMock query-chain mocking.

Run (from backend/):
    pytest tests/test_download_wecom_media_once_cli.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.db.models import BillingPlan, MediaFile, Subscription
from sqlalchemy.orm import Session

from tests.fakes import (
    _TENANT_A,
    FakeWecomSdk,
    insert_archive_message,
    insert_tenant,
    insert_tenant_wecom_config,
    install_fake_sdk,
    worker_engine,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)


def test_main_downloads_candidate_and_prints_expected_summary(
    monkeypatch, tmp_path, capsys, worker_engine
) -> None:
    import scripts.download_wecom_media_once as script

    engine = worker_engine
    with Session(engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                BillingPlan(
                    id="rnd385-cli-plan",
                    code="rnd385_cli_plan",
                    display_name="RND-385 CLI plan",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=5 * 1024**3,
                ),
                Subscription(
                    id="rnd385-cli-subscription",
                    tenant_id=_TENANT_A,
                    plan_id="rnd385-cli-plan",
                    status="active",
                    starts_at=now - timedelta(days=1),
                    ends_at=now + timedelta(days=365),
                    source="test",
                    renewal_count=0,
                    revision=1,
                ),
            ]
        )
        db.commit()
        insert_archive_message(
            db,
            tenant_id=_TENANT_A,
            decrypt_status="success",
            msgtype="voice",
            sdkfileid="sdk-voice-1",
            msgtime=100,
        )

    fake = FakeWecomSdk()
    amr_bytes = b"#!AMR" + b"voice-body-bytes"
    fake.set_media_chunks("sdk-voice-1", [amr_bytes])
    install_fake_sdk(monkeypatch, fake, script.wecom_sdk)

    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media.lock"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setenv("WECOM_TENANT_ID", _TENANT_A)
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))

    monkeypatch.setattr(script, "create_engine", lambda _url: engine)
    monkeypatch.setattr(
        script,
        "resolve_tenant_archive_credentials",
        lambda *_args: type(
            "Credentials",
            (),
            {"tenant_id": _TENANT_A, "corp_id": "tenant-corp", "archive_secret": "tenant-secret"},
        )(),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--types", "voice"])

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "media_worker tenant=" in out and "candidate_selected: 1" in out
    assert "media_worker tenant=" in out and "trigger_source=manual trigger=completed attempted=1 succeeded=1 failed=0" in out
    assert "media_worker tenant=" in out and "downloaded: 1" in out
    assert "media_worker tenant=" in out and "failed: 0" in out
    assert "[PASS] download_wecom_media_once completed" in out
    assert "media_worker trigger_source=manual lifecycle=ended result=completed" in out
    assert "error_class=none" in out
    assert "completed_at=" in out
    assert fake.destroyed is True

    with Session(engine) as db:
        media_file = db.query(MediaFile).filter_by(tenant_id=_TENANT_A, sdkfileid="sdk-voice-1").one()
        assert media_file.download_status == "downloaded"
        assert media_file.download_attempts == 1
        assert Path(media_file.local_path).read_bytes() == amr_bytes


def test_main_hides_secret_like_provider_value_and_emits_failed_lifecycle(
    monkeypatch, tmp_path, capsys, worker_engine
) -> None:
    """An invalid provider must not carry a pasted URL into the journal."""
    import scripts.download_wecom_media_once as script

    with Session(worker_engine) as db:
        insert_tenant(db, _TENANT_A)
        insert_tenant_wecom_config(db, _TENANT_A, "corp1")
        insert_archive_message(
            db,
            tenant_id=_TENANT_A,
            decrypt_status="success",
            msgtype="voice",
            sdkfileid="sdk-voice-for-provider-validation",
            msgtime=100,
        )

    unsafe_provider = "https://storage.invalid/private/path?sig=fixture-only"
    monkeypatch.setenv("MEDIA_DOWNLOAD_LOCK_PATH", str(tmp_path / "media.lock"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused")
    monkeypatch.setenv("WECOM_TENANT_ID", _TENANT_A)
    monkeypatch.setenv("WECOM_SDK_LIB_PATH", "/fake/lib.so")
    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", unsafe_provider)
    monkeypatch.setattr(script, "create_engine", lambda _url: worker_engine)
    monkeypatch.setattr(
        script,
        "resolve_tenant_archive_credentials",
        lambda *_args: type(
            "Credentials",
            (),
            {"tenant_id": _TENANT_A, "corp_id": "tenant-corp", "archive_secret": "tenant-secret"},
        )(),
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc:
        script.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert unsafe_provider not in captured.out
    assert unsafe_provider not in captured.err
    assert "error_class=storage_configuration" in captured.out
    assert "lifecycle=ended result=failed" in captured.out
    assert "completed_at=" in captured.out


def test_main_invalid_argument_emits_classified_failed_lifecycle(monkeypatch, capsys) -> None:
    import scripts.download_wecom_media_once as script

    monkeypatch.setattr(sys, "argv", ["prog", "--limit", "0"])

    with pytest.raises(SystemExit) as exc:
        script.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "error_class=invalid_arguments" in out
    assert "lifecycle=ended result=failed" in out
    assert "completed_at=" in out


def test_media_worker_symbols_remain_importable_from_the_script() -> None:
    from scripts.download_wecom_media_once import (
        MediaDownloadSummary,
        _persist_download_outcome,
        _safe_delete_after_commit_failure,
        build_within_window_count,
        download_media_candidates,
    )

    assert MediaDownloadSummary is not None
    assert callable(_persist_download_outcome)
    assert callable(_safe_delete_after_commit_failure)
    assert callable(build_within_window_count)
    assert callable(download_media_candidates)
