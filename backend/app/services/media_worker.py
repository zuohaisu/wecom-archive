"""
Core application logic for the one-shot WeCom media download worker
(RND-222).

Extracted from scripts/download_wecom_media_once.py's _run() so the
download/persistence loops can be called and tested directly, independent
of argparse, the concurrent-run lock, tenant resolution, candidate
selection, and SDK/storage-provider lifecycle — all of which stay in the
script (see its module docstring; also see this module's docstring below
for why candidate selection specifically stays there too, not a generic
"CLI stuff stays in the shell" rule).

Candidate selection (select_candidates / select_nested_media_candidates,
both from app.media_download) deliberately stays in the CLI script rather
than moving here: scripts/download_wecom_media_once.py's control flow
decides whether to initialise the SDK/storage provider at all based on
those results (a --count-only run, or a run with zero candidates, must
never touch the SDK — see the script's Concurrency/Safety docstring
sections), so the selection call has to run before — and independent of —
this module's SDK-dependent download loop. Existing tests
(tests/test_download_wecom_media_once.py and friends) also monkeypatch
`script.select_candidates`/`script.select_nested_media_candidates`
directly, which requires the script's own module-level call site to be
the one actually invoked.

download_one() (app.media_download) is the one call site in this codebase
that always reaches the WeCom SDK through the real app.sdk.wecom_sdk
module directly, not through an injected parameter (see its own
docstring) — this module's functions accept lib/handle exactly as
download_one expects them and pass them straight through; there is
nothing here for a `sdk` parameter to inject into. Tests fake the SDK
boundary for this worker by monkeypatching app.sdk.wecom_sdk's attributes
directly (see tests/fakes.py's install_fake_sdk), the same technique the
pre-existing test suite already uses.

Transaction boundary (unchanged from the original script — see RND-222
ticket §0.3): per-candidate commit, with rollback + best-effort orphan
storage cleanup on a confirmed DB commit failure — see
_persist_download_outcome below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.media_download import (
    build_candidate_query,
    download_one,
    get_or_reset_media_file,
)
from app.media_storage import MediaStorageProvider
from app.thumbnail_pipeline import maybe_generate_after_download
from app.voice_playback_pipeline import (
    maybe_generate_after_download as maybe_generate_voice_playback_after_download,
)


@dataclass
class MediaDownloadSummary:
    downloaded: int = 0
    failed: int = 0
    reason_counts: dict = field(default_factory=dict)
    nested_downloaded: int = 0
    nested_failed: int = 0
    nested_reason_counts: dict = field(default_factory=dict)


def build_within_window_count(session: Session, tenant_id: str, msgtypes, retry: bool, since_ms: int) -> int:
    return build_candidate_query(session, tenant_id, msgtypes, retry, since_ms=since_ms).count()


def _persist_download_outcome(
    session: Session,
    storage_provider: MediaStorageProvider,
    media_file,
    outcome: str,
    detail: Optional[str],
    file_size: Optional[int],
    write_backend_name: str,
    file_type: str,
    downloaded: int,
    failed: int,
    reason_counts: dict,
) -> "tuple[int, int]":
    """Persist one download_one() outcome onto its media_files row and
    return the updated (downloaded, failed) counters.

    Shared by the top-level (--types) loop and the nested mixed/chatrecord
    item loop in download_media_candidates() below (RND-200) — both need
    identical persistence semantics (including the rollback-then-best-
    effort-orphan-cleanup path on a confirmed DB commit failure), so this
    is the one place that logic lives rather than two copies that could
    silently drift apart."""
    try:
        if outcome == "downloaded":
            media_file.file_type = file_type
            media_file.download_status = "downloaded"
            media_file.storage_backend = write_backend_name
            media_file.storage_ref = detail
            media_file.local_path = detail if write_backend_name == "local" else None
            media_file.file_size = file_size
            media_file.oss_key = None
            session.commit()
            # RND-207: generate a list thumbnail for the freshly-downloaded
            # image, co-located in the same backend. Fully isolated — a
            # thumbnail failure never affects the already-committed original.
            maybe_generate_after_download(session, storage_provider, media_file)
            # RND-258: conversion happens only after the original is safely
            # committed, and no ffmpeg outcome can change download success.
            maybe_generate_voice_playback_after_download(
                session, storage_provider, media_file
            )
            return downloaded + 1, failed
        media_file.download_status = "failed"
        media_file.local_path = None
        media_file.storage_backend = None
        media_file.storage_ref = None
        media_file.oss_key = None
        session.commit()
        reason_counts[detail or "unknown"] = reason_counts.get(detail or "unknown", 0) + 1
        return downloaded, failed + 1
    except Exception:
        session.rollback()
        if outcome == "downloaded" and detail:
            _safe_delete_after_commit_failure(storage_provider, detail)
        reason_counts["db_commit_error"] = reason_counts.get("db_commit_error", 0) + 1
        return downloaded, failed + 1


def _safe_delete_after_commit_failure(storage_provider: MediaStorageProvider, storage_ref: str) -> None:
    """Best-effort cleanup of an orphaned upload after a confirmed DB
    commit failure. Reaching this can only mean the upload/publish already
    succeeded and the database commit itself failed — never a transient
    stat/metadata hiccup, since download_one's success path never calls a
    remote stat — so deleting the now-unreferenced object here is a
    deliberate, narrow rollback for a confirmed DB persistence failure."""
    storage_provider.delete(storage_ref)


def download_media_candidates(
    session: Session,
    tenant_id: str,
    lib,
    handle,
    storage_provider: MediaStorageProvider,
    write_backend_name: str,
    timeout: int,
    candidates: list,
    nested_item_candidates: list,
) -> MediaDownloadSummary:
    """Download every candidate and nested item, persisting each outcome.

    *candidates* / *nested_item_candidates* are already-selected (see
    module docstring for why selection itself stays in the CLI script).
    Never prints and never calls sys.exit — every failure mode is counted
    into the returned summary, exactly as the original script's _run()
    counted them before this extraction.
    """
    summary = MediaDownloadSummary()

    for msg in candidates:
        media_file = get_or_reset_media_file(session, tenant_id, msg.sdkfileid, msg.id)
        if media_file is None:
            summary.failed += 1
            summary.reason_counts["media_identity_conflict"] = (
                summary.reason_counts.get("media_identity_conflict", 0) + 1
            )
            continue

        outcome, detail, file_size = download_one(
            lib, handle, storage_provider, tenant_id, msg.id, msg.msgtype, msg.sdkfileid, timeout
        )
        summary.downloaded, summary.failed = _persist_download_outcome(
            session, storage_provider, media_file, outcome, detail, file_size,
            write_backend_name, msg.msgtype, summary.downloaded, summary.failed, summary.reason_counts,
        )

    for msg, ref in nested_item_candidates:
        media_file = get_or_reset_media_file(session, tenant_id, ref["sdkfileid"], msg.id)
        if media_file is None:
            summary.nested_failed += 1
            summary.nested_reason_counts["media_identity_conflict"] = (
                summary.nested_reason_counts.get("media_identity_conflict", 0) + 1
            )
            continue

        outcome, detail, file_size = download_one(
            lib, handle, storage_provider, tenant_id, msg.id, ref["type"], ref["sdkfileid"],
            timeout, item_key=ref["path"],
        )
        summary.nested_downloaded, summary.nested_failed = _persist_download_outcome(
            session, storage_provider, media_file, outcome, detail, file_size,
            write_backend_name, ref["type"], summary.nested_downloaded, summary.nested_failed,
            summary.nested_reason_counts,
        )

    return summary
