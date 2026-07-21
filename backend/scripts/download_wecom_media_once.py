#!/usr/bin/env python3
"""
One-shot: download media (image/voice/video/file/emotion) for archived
WeCom messages and populate media_files rows.

This is the SOLE media download entry point for this codebase (RND-147
image support + RND-151 recency/ordering + RND-199 voice/video/file/
emotion, all unified onto one pipeline — see app.media_download's module
docstring for why an earlier revision's second, image-specific script was
retired). Candidate selection, the pending/downloaded/failed state
machine, write-to-.part-then-atomic-publish safety, and the non-blocking
concurrent-run lock all live in app.media_download; this script is only
the CLI/process wrapper (argument parsing, environment/lock/SDK
lifecycle, and per-candidate persistence). --types defaults to every
supported type but can be narrowed, e.g. to run image and voice on
separate schedules with separate --limit/--since-hours budgets.

Usage (from backend/):
    python scripts/download_wecom_media_once.py --count-only
    python scripts/download_wecom_media_once.py --limit 20
    python scripts/download_wecom_media_once.py --limit 20 --retry
    python scripts/download_wecom_media_once.py --types image,voice --limit 20
    python scripts/download_wecom_media_once.py --since-hours 72 --newest-first --limit 10

Required environment variables (only DATABASE_URL / WECOM_CORP_ID are
needed for --count-only, since that mode never touches the SDK or
filesystem):
    DATABASE_URL          PostgreSQL connection string
    WECOM_CORP_ID         WeCom corporation ID (resolves the active tenant)
    WECOM_SDK_LIB_PATH    Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_ARCHIVE_SECRET  WeCom conversation archive secret
    STORAGE_LOCAL_PATH    Media storage root (same variable RND-144 reads)

Optional environment variables:
    WECOM_MEDIA_TIMEOUT      Per-chunk SDK call timeout in seconds (default 30)
    MEDIA_DOWNLOAD_LOCK_PATH Lock file path (default:
                             /srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock)

Tenant scoping: resolved server-side from WECOM_CORP_ID via
tenant_wecom_configs — there is no --tenant-id flag; this codebase's rule
is that tenant_id is never accepted from caller-supplied input.

Concurrency: acquires a non-blocking process-level file lock (fcntl.flock)
before touching the database at all. If another invocation already holds
the lock, this exits 0 immediately without selecting candidates or
downloading anything — never blocks waiting for the lock. Because there is
now only one pipeline, there is only one lock: a single systemd
timer/service (or cron entry) should own this script, though an operator
can still run --types-narrowed invocations sequentially without
contention (they share the same lock, so overlapping runs safely no-op
rather than double-processing).

Exit codes:
    0  Success (including --count-only, "nothing to do", and "lock held")
    1  Any fatal failure (missing env, SDK load/init error, DB error)

Safety constraints:
    - Never prints sdkfileid, local_path, storage_ref, or any message
      body/content.
    - --count-only performs zero writes (DB or filesystem).
    - Only a byte signature matching the expected category for a
      message's type is ever marked "downloaded" (see
      app.media_download._SIGNATURE_CATEGORY_BY_MSGTYPE) — never the
      caller-supplied msgtype/sdkfileid alone.
    - Downloads are written to a .part temp file, always removed on any
      failure path.
    - media_files.sdkfileid is unique per tenant; get_or_reset_media_file
      never reuses/overwrites a row belonging to a different message.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from typing import FrozenSet, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, TenantWecomConfig
from app.media_download import (
    GENERIC_DOWNLOAD_MSGTYPES,
    count_candidates_with_existing_media_row,
    download_one,
    get_or_reset_media_file,
    select_candidates,
    select_nested_media_candidates,
)
from app.media_storage import (
    LocalStorageProvider,
    MediaStorageProvider,
    UnsupportedMediaStorageProvider,
    get_configured_write_backend_name,
    get_media_storage_provider,
)
from app.sdk import wecom_sdk
from app.thumbnail_pipeline import maybe_generate_after_download

_DEFAULT_LIMIT = 10
_DEFAULT_TIMEOUT = 30
# The single lock for the single pipeline (RND-147's original path, kept
# unchanged so an existing deployment's lock directory/permissions need no
# migration).
_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _optional_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[FAIL] Environment variable {name} is not a valid integer: {raw!r}", flush=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Concurrent-run guard — non-blocking fcntl.flock
# ---------------------------------------------------------------------------


def _acquire_lock(lock_path: str) -> Optional[int]:
    lock_dir = os.path.dirname(lock_path)
    if lock_dir and not os.path.isdir(lock_dir):
        try:
            os.makedirs(lock_dir, mode=0o750, exist_ok=True)
        except OSError:
            print("[FAIL] Cannot create lock directory", flush=True)
            sys.exit(1)

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return None
    return lock_fd


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(lock_fd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


def _require_tenant_id(session: Session, corp_id: str) -> str:
    row = (
        session.query(TenantWecomConfig)
        .filter(
            TenantWecomConfig.corp_id == corp_id,
            TenantWecomConfig.is_active == True,  # noqa: E712
        )
        .first()
    )
    if row is None:
        print(
            "[FAIL] No active tenant found for this corp. "
            "Run bootstrap_default_tenant.py first.",
            flush=True,
        )
        sys.exit(1)
    return row.tenant_id


# ---------------------------------------------------------------------------
# --types parsing
# ---------------------------------------------------------------------------


def _parse_types(raw: Optional[str]) -> FrozenSet[str]:
    """Parse --types into a validated msgtype set, defaulting to every
    type this script supports. Rejects any type outside
    GENERIC_DOWNLOAD_MSGTYPES loudly (never silently drops an unknown
    type from the requested set)."""
    if not raw or not raw.strip():
        return GENERIC_DOWNLOAD_MSGTYPES
    requested = frozenset(t.strip() for t in raw.split(",") if t.strip())
    unknown = requested - GENERIC_DOWNLOAD_MSGTYPES
    if unknown:
        print(
            f"[FAIL] --types contains unsupported message type(s): {sorted(unknown)}. "
            f"Supported: {sorted(GENERIC_DOWNLOAD_MSGTYPES)}",
            flush=True,
        )
        sys.exit(1)
    return requested


# ---------------------------------------------------------------------------
# Storage provider
# ---------------------------------------------------------------------------


def _media_storage_provider(backend_name: str) -> MediaStorageProvider:
    try:
        provider = get_media_storage_provider(backend_name)
    except UnsupportedMediaStorageProvider as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)

    if isinstance(provider, LocalStorageProvider) and provider.root is None:
        print("[FAIL] STORAGE_LOCAL_PATH is not set", flush=True)
        sys.exit(1)
    return provider


def _since_ms_cutoff(since_hours: float) -> int:
    return int(time.time() * 1000) - int(since_hours * 3600 * 1000)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot WeCom generic media download — voice/video/file/emotion (RND-199)"
    )
    parser.add_argument("--count-only", action="store_true", help="Report candidate counts only; perform no writes")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help=f"Max messages to process this run (default {_DEFAULT_LIMIT})")
    parser.add_argument("--retry", action="store_true", help="Also retry media_files rows with download_status='failed'")
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help=(
            "Comma-separated message types to process (subset of "
            f"{sorted(GENERIC_DOWNLOAD_MSGTYPES)}); default: all of them"
        ),
    )
    parser.add_argument("--since-hours", type=float, default=None, help="Only select candidates with msgtime within the last N hours")
    parser.add_argument("--newest-first", action="store_true", help="Order candidates by msgtime descending instead of oldest-first")
    parser.add_argument(
        "--skip-nested",
        action="store_true",
        help=(
            "Skip mixed/chatrecord nested media items (RND-200). By default, "
            "after processing --types candidates, this script also downloads "
            "media referenced by nested items inside mixed/chatrecord "
            "messages (its own --limit/--retry/--since-hours-budgeted pass, "
            "reusing the same SDK session/storage provider) — pass this "
            "flag to disable that and process --types candidates only, as "
            "before RND-200. Note: --newest-first has no effect on the "
            "nested pass, which is always oldest-first regardless."
        ),
    )
    args = parser.parse_args()

    if args.limit <= 0:
        print("[FAIL] --limit must be a positive integer", flush=True)
        sys.exit(1)

    if args.since_hours is not None and args.since_hours <= 0:
        print("[FAIL] --since-hours must be a positive number", flush=True)
        sys.exit(1)

    msgtypes = _parse_types(args.types)

    lock_path = os.environ.get("MEDIA_DOWNLOAD_LOCK_PATH", "").strip() or _DEFAULT_LOCK_PATH
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        print("[INFO] candidate_total: 0", flush=True)
        print("[INFO] candidate_selected: 0", flush=True)
        print(
            "[PASS] another instance already holds the run lock — exiting without "
            "selecting candidates",
            flush=True,
        )
        sys.exit(0)

    try:
        _run(args, msgtypes)
    finally:
        _release_lock(lock_fd)


def _run(args: argparse.Namespace, msgtypes: FrozenSet[str]) -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")

    engine = create_engine(database_url)

    with Session(engine) as session:
        tenant_id = _require_tenant_id(session, corp_id)

        since_ms = _since_ms_cutoff(args.since_hours) if args.since_hours is not None else None

        actionable, stale_repairs, total_eligible = select_candidates(
            session,
            tenant_id,
            msgtypes,
            args.retry,
            args.limit,
            since_ms=since_ms,
            newest_first=args.newest_first,
        )
        candidates: List[ArchiveMessage] = list(actionable) + [m for m, _mf in stale_repairs]

        print(f"[INFO] types: {sorted(msgtypes)}", flush=True)
        print(f"[INFO] candidate_total: {total_eligible}", flush=True)
        print(f"[INFO] candidate_selected: {len(candidates)}", flush=True)
        if stale_repairs:
            print(f"[INFO] stale_downloaded_repair_selected: {len(stale_repairs)}", flush=True)

        nested_item_candidates: List = []
        nested_messages_scanned = 0
        if not args.skip_nested:
            # --retry and --since-hours are honored here exactly as they
            # are for the --types pass above (RND-200 QA fix — an earlier
            # revision silently ignored both for nested candidates).
            # --newest-first has no nested equivalent: see
            # build_nested_media_candidate_query's docstring for why.
            nested_item_candidates, nested_messages_scanned = select_nested_media_candidates(
                session, tenant_id, args.limit, retry=args.retry, since_ms=since_ms
            )
            print(f"[INFO] nested_candidate_messages_scanned: {nested_messages_scanned}", flush=True)
            print(f"[INFO] nested_candidate_items_selected: {len(nested_item_candidates)}", flush=True)

        if args.count_only:
            print(
                f"[INFO] candidate_ordering: {'newest_first' if args.newest_first else 'oldest_first'}",
                flush=True,
            )
            if since_ms is not None:
                within_window = build_within_window_count(session, tenant_id, msgtypes, args.retry, since_ms)
                print(f"[INFO] since_hours: {args.since_hours}", flush=True)
                print(f"[INFO] candidates_in_window: {within_window}", flush=True)
                print(f"[INFO] candidates_excluded_by_window: {total_eligible - within_window}", flush=True)
            existing_media_count = count_candidates_with_existing_media_row(session, tenant_id, msgtypes)
            print(f"[INFO] candidates_with_existing_media_row: {existing_media_count}", flush=True)
            print("[PASS] count-only mode — no writes performed", flush=True)
            sys.exit(0)

        if not candidates and not nested_item_candidates:
            print("[PASS] no candidates to process", flush=True)
            sys.exit(0)

        lib_path = _require_env("WECOM_SDK_LIB_PATH")
        secret = _require_env("WECOM_ARCHIVE_SECRET")
        timeout = _optional_int_env("WECOM_MEDIA_TIMEOUT", _DEFAULT_TIMEOUT)
        write_backend_name = get_configured_write_backend_name()
        storage_provider = _media_storage_provider(write_backend_name)

        try:
            lib = wecom_sdk.load_sdk(lib_path)
        except FileNotFoundError:
            print("[FAIL] SDK library not found at the configured path", flush=True)
            sys.exit(1)
        except OSError:
            print("[FAIL] Failed to load SDK library", flush=True)
            sys.exit(1)

        try:
            wecom_sdk.configure_sdk(lib)
            wecom_sdk.configure_sdk_media_data(lib)
        except AttributeError as exc:
            print(f"[FAIL] SDK missing expected media-download symbol: {exc}", flush=True)
            sys.exit(1)

        handle = wecom_sdk.new_sdk(lib)
        if not handle:
            print("[FAIL] NewSdk() returned a null handle", flush=True)
            sys.exit(1)

        init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
        if init_ret != 0:
            print(f"[FAIL] Init() failed (return code {init_ret})", flush=True)
            try:
                wecom_sdk.destroy_sdk(lib, handle)
            except Exception:
                pass
            sys.exit(1)

        downloaded = 0
        failed = 0
        reason_counts: dict[str, int] = {}

        for msg in candidates:
            media_file = get_or_reset_media_file(session, tenant_id, msg.sdkfileid, msg.id)
            if media_file is None:
                failed += 1
                reason_counts["media_identity_conflict"] = (
                    reason_counts.get("media_identity_conflict", 0) + 1
                )
                continue

            outcome, detail, file_size = download_one(
                lib, handle, storage_provider, tenant_id, msg.id, msg.msgtype, msg.sdkfileid, timeout
            )
            downloaded, failed = _persist_download_outcome(
                session, storage_provider, media_file, outcome, detail, file_size,
                write_backend_name, msg.msgtype, downloaded, failed, reason_counts,
            )

        nested_downloaded = 0
        nested_failed = 0
        nested_reason_counts: dict[str, int] = {}

        for msg, ref in nested_item_candidates:
            media_file = get_or_reset_media_file(session, tenant_id, ref["sdkfileid"], msg.id)
            if media_file is None:
                nested_failed += 1
                nested_reason_counts["media_identity_conflict"] = (
                    nested_reason_counts.get("media_identity_conflict", 0) + 1
                )
                continue

            outcome, detail, file_size = download_one(
                lib, handle, storage_provider, tenant_id, msg.id, ref["type"], ref["sdkfileid"],
                timeout, item_key=ref["path"],
            )
            nested_downloaded, nested_failed = _persist_download_outcome(
                session, storage_provider, media_file, outcome, detail, file_size,
                write_backend_name, ref["type"], nested_downloaded, nested_failed, nested_reason_counts,
            )

        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass

        print(f"[INFO] downloaded: {downloaded}", flush=True)
        print(f"[INFO] failed: {failed}", flush=True)
        if reason_counts:
            diag = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
            print(f"[INFO] failed_reasons: {diag}", flush=True)
        if not args.skip_nested:
            print(f"[INFO] nested_downloaded: {nested_downloaded}", flush=True)
            print(f"[INFO] nested_failed: {nested_failed}", flush=True)
            if nested_reason_counts:
                nested_diag = ", ".join(f"{k}={v}" for k, v in sorted(nested_reason_counts.items()))
                print(f"[INFO] nested_failed_reasons: {nested_diag}", flush=True)
        print("[PASS] download_wecom_media_once completed", flush=True)
        sys.exit(0)


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
) -> tuple[int, int]:
    """Persist one download_one() outcome onto its media_files row and
    return the updated (downloaded, failed) counters.

    Shared by the top-level (--types) loop and the nested mixed/chatrecord
    item loop in _run() (RND-200) — both need identical persistence
    semantics (including the rollback-then-best-effort-orphan-cleanup path
    on a confirmed DB commit failure), so this is the one place that logic
    lives rather than two copies that could silently drift apart."""
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


def build_within_window_count(session: Session, tenant_id: str, msgtypes, retry: bool, since_ms: int) -> int:
    from app.media_download import build_candidate_query

    return build_candidate_query(session, tenant_id, msgtypes, retry, since_ms=since_ms).count()


if __name__ == "__main__":
    main()
