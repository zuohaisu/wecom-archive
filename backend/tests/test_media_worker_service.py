"""
Tests for RND-222 — app.services.media_worker, the extracted media
download/persistence loop.

Scope (deliberately a smoke test, not full coverage — see RND-222 ticket
§2.4): candidate selection stays tenant-scoped through the real
app.media_download.select_candidates() (unchanged, still called directly
by the CLI script — see app/services/media_worker.py's module docstring
for why), and download_media_candidates() actually downloads and persists
a candidate end to end. The rest of media's behavior (nested items,
retry, stale-repair, --count-only, every msgtype/signature combination)
remains covered by the pre-existing tests/test_download_wecom_media_once.py
and tests/test_qiniu_worker_integration.py, both of which are unmodified
and still pass against the thinned CLI shell.

Run (from backend/):
    pytest tests/test_media_worker_service.py -v
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.db.models import MediaFile
from app.media_download import select_candidates
from app.media_storage import LocalStorageProvider
from app.sdk import wecom_sdk
from app.services.media_worker import download_media_candidates
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    _TENANT_B,
    insert_archive_message,
    install_fake_sdk,
    worker_db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)


def test_select_candidates_is_tenant_scoped_and_download_persists_the_file(
    worker_db, tmp_path, monkeypatch
) -> None:
    a_msg = insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="success",
        msgtype="voice",
        sdkfileid="sdk-a-1",
        msgtime=100,
    )
    insert_archive_message(
        worker_db,
        tenant_id=_TENANT_B,
        decrypt_status="success",
        msgtype="voice",
        sdkfileid="sdk-b-1",
        msgtime=101,
    )

    actionable, stale_repairs, total_eligible = select_candidates(
        worker_db, _TENANT_A, frozenset({"voice"}), retry=False, limit=10
    )

    assert [m.id for m in actionable] == [a_msg.id]
    assert stale_repairs == []
    assert total_eligible == 1

    fake = FakeWecomSdk()
    amr_bytes = b"#!AMR" + b"voice-body-bytes"
    fake.set_media_chunks("sdk-a-1", [amr_bytes])
    install_fake_sdk(monkeypatch, fake, wecom_sdk)

    storage_provider = LocalStorageProvider(tmp_path)

    summary = download_media_candidates(
        worker_db,
        _TENANT_A,
        "fake-lib",
        "fake-handle",
        storage_provider,
        "local",
        30,
        actionable,
        [],
    )

    assert summary.downloaded == 1
    assert summary.failed == 0

    media_file = (
        worker_db.query(MediaFile)
        .filter_by(tenant_id=_TENANT_A, sdkfileid="sdk-a-1")
        .one()
    )
    assert media_file.download_status == "downloaded"
    assert Path(media_file.local_path).read_bytes() == amr_bytes

    # Tenant B's row was never a candidate for this run and has no
    # media_files row at all.
    assert worker_db.query(MediaFile).filter_by(tenant_id=_TENANT_B).count() == 0


def test_service_module_has_no_print_sys_exit_or_shell_imports() -> None:
    import ast

    from app.services import media_worker

    source = inspect.getsource(media_worker)
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
