#!/usr/bin/env python3
"""
One-shot: download image media for archived WeCom messages and populate
media_files rows (RND-147).

Image only. This script can be run manually or by the scheduled recent
image media download timer (RND-168; see
deploy/systemd/wecom-archive-media-download.timer and
docs/wecom_archive_media_download_runbook.md). media_files is otherwise
never populated by any other code path (RND-144 only serves/renders rows
that already exist here).

Usage (from backend/):
    python scripts/download_wecom_image_media_once.py --count-only
    python scripts/download_wecom_image_media_once.py --limit 20
    python scripts/download_wecom_image_media_once.py --limit 20 --retry
    python scripts/download_wecom_image_media_once.py --since-hours 72 --newest-first --limit 10

RND-151: the WeCom platform media retrieval window expires, so a run with
the default oldest-first ordering can have its whole --limit budget
consumed by old, already-expired image candidates before ever reaching
recently ingested ones. --since-hours restricts fresh-candidate selection
to messages no older than N hours (by msgtime, epoch-ms, compared against
current UTC time — never local timezone formatting); --newest-first
orders fresh candidates by msgtime descending (id descending as a stable
tie-breaker) instead of the default ascending-id order. The production
runbook should use both together. Neither flag affects
build_downloaded_repair_query (the stale "downloaded" repair scan), which
is about already-downloaded rows, not new-candidate prioritization.

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
tenant_wecom_configs, exactly like sync_wecom_archive_once.py. There is no
--tenant-id flag — this codebase's rule (see app/routers/conversations.py)
is that tenant_id is never accepted from caller-supplied input.

Concurrency: acquires a non-blocking process-level file lock (same
fcntl.flock pattern as run_archive_worker_once.py) before touching the
database at all. If another invocation already holds the lock, this exits
0 immediately without selecting candidates or downloading anything — never
blocks waiting for the lock.

Candidate selection (tenant-scoped) — see select_candidates():
    decrypt_status == "success", msgtype == "image", sdkfileid set.
    "actionable" rows (no media_files row yet, an existing "pending" row,
    or — only with --retry — an existing "failed" row) always take
    priority up to --limit.
    Any remaining budget is used to re-verify "downloaded" rows against
    the RND-144 servability predicate (app/media_storage.py); one that is
    no longer servable (file missing, outside the storage root, or a
    disallowed extension) is treated as stale and repaired. A "downloaded"
    row whose file is still servable is left untouched.

Exit codes:
    0  Success (including --count-only, "nothing to do", and "lock held")
    1  Any fatal failure (missing env, SDK load/init error, DB error)

Safety constraints:
    - Never prints sdkfileid, local_path, or any message body/content.
    - --count-only performs zero writes (DB or filesystem).
    - Only allow-listed image types (jpg/jpeg/png/gif/webp), detected from
      the downloaded bytes themselves, are ever marked "downloaded" — an
      unrecognised byte signature is marked "failed" and no local_path is
      written (see app/media_storage.py:detect_image_type_from_bytes).
    - Downloads are written to a .part temp file; the .part file is always
      removed on any failure path (download error, write error,
      unsupported type, rename error) via a single centralized cleanup —
      see download_one()'s try/finally.
    - media_files.sdkfileid is globally unique with no tenant_id column.
      get_or_reset_media_file() never reuses/overwrites an existing row
      whose archive_message_id does not match the message currently being
      processed — a mismatch fails that candidate safely instead of
      silently reassigning another message's media row.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, or_
from sqlalchemy.orm import Query, Session

from app.db.models import ArchiveMessage, MediaFile, TenantWecomConfig
from app.media_storage import detect_image_type_from_bytes, resolve_image_file_state
from app.sdk import wecom_sdk

_DEFAULT_LIMIT = 10
_DEFAULT_TIMEOUT = 30
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
# Concurrent-run guard — same fcntl.flock pattern as run_archive_worker_once.py
# ---------------------------------------------------------------------------


def _acquire_lock(lock_path: str) -> Optional[int]:
    """Acquire a non-blocking exclusive file lock.

    Returns the open file descriptor on success, or None if another
    process already holds the lock (caller must treat that as a safe
    no-op, not an error — never print the lock path itself). Never blocks.
    """
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
# Tenant resolution (same pattern as sync_wecom_archive_once.py)
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
# Candidate queries
# ---------------------------------------------------------------------------


def build_candidate_query(
    session: Session,
    tenant_id: str,
    retry: bool,
    since_ms: Optional[int] = None,
    newest_first: bool = False,
) -> Query:
    """Tenant-scoped, image-only query for rows eligible for a *fresh*
    download attempt: no media_files row yet, an existing "pending" row
    (safe to resume — it never completed), or (only with --retry) an
    existing "failed" row. Never includes "downloaded" rows — those are
    handled separately by build_downloaded_repair_query so a servability
    re-check can decide whether they need repair.

    since_ms (RND-151): when set, restricts to ArchiveMessage.msgtime >=
    since_ms (epoch-ms, same units already used for msgtime elsewhere in
    this codebase). None (default) applies no recency filter — unchanged
    behavior.

    newest_first (RND-151): when True, orders by msgtime descending with
    id descending as a stable tie-breaker (messages sharing one msgtime
    are common in a batch-ingested burst), instead of the default
    ascending-id order — so a run can prioritize recently ingested images
    over old, possibly platform-expired ones.
    """
    query = (
        session.query(ArchiveMessage)
        .outerjoin(MediaFile, MediaFile.archive_message_id == ArchiveMessage.id)
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtype == "image",
            ArchiveMessage.sdkfileid.isnot(None),
            ArchiveMessage.sdkfileid != "",
        )
    )
    eligible_statuses = ["pending", "failed"] if retry else ["pending"]
    query = query.filter(
        or_(MediaFile.id.is_(None), MediaFile.download_status.in_(eligible_statuses))
    )
    if since_ms is not None:
        query = query.filter(ArchiveMessage.msgtime >= since_ms)
    if newest_first:
        return query.order_by(ArchiveMessage.msgtime.desc(), ArchiveMessage.id.desc())
    return query.order_by(ArchiveMessage.id)


def build_downloaded_repair_query(session: Session, tenant_id: str) -> Query:
    """Tenant-scoped, image-only query for rows currently marked
    "downloaded" — candidates for the servability re-check (RND-147 QA
    fix): a downloaded row whose file is missing, outside the storage
    root, or has a disallowed extension must be treated as stale and
    repairable, not skipped forever just because its status says
    "downloaded"."""
    return (
        session.query(ArchiveMessage, MediaFile)
        .join(MediaFile, MediaFile.archive_message_id == ArchiveMessage.id)
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtype == "image",
            ArchiveMessage.sdkfileid.isnot(None),
            ArchiveMessage.sdkfileid != "",
            MediaFile.download_status == "downloaded",
        )
        .order_by(ArchiveMessage.id)
    )


def is_downloaded_media_file_stale(media_file: MediaFile) -> bool:
    """True when a media_files row claims download_status="downloaded" but
    its file is not actually servable per the RND-144 predicate (missing,
    outside the configured storage root, or a disallowed extension) — i.e.
    this row is stale/broken and safe to repair, not a real skip case.

    Reuses app.media_storage.resolve_image_file_state — the exact same
    tri-state the timeline serializer and the media route use — so "stale"
    here means precisely "the RND-144 route could not serve this file".
    """
    return resolve_image_file_state(media_file.local_path) != "servable"


_REPAIR_SCAN_BATCH_SIZE = 500


def _scan_for_stale_downloaded(
    session: Session,
    tenant_id: str,
    needed: int,
    batch_size: int = _REPAIR_SCAN_BATCH_SIZE,
) -> List[Tuple[ArchiveMessage, MediaFile]]:
    """Scan "downloaded" rows in ascending-id batches of `batch_size`,
    servability-checking each one, until `needed` stale rows are found or
    the table is exhausted.

    RND-147 QA fix ("stale repair starvation"): `needed` (the caller's
    remaining --limit budget) must never be used as the SQL LIMIT for this
    query — doing so silently drops any stale row that happens to sort
    after enough valid/servable "downloaded" rows to fill that limit, so a
    genuinely broken row could be skipped forever no matter how many times
    this script re-runs. Instead, `batch_size` is a fixed internal fetch
    size independent of `needed`; the servability filter runs in Python on
    each batch, and only rows that fail it ever count against `needed`.
    Pagination (rather than one unbounded fetch) keeps memory bounded for
    tenants with a large "downloaded" set.
    """
    stale: List[Tuple[ArchiveMessage, MediaFile]] = []
    if needed <= 0:
        return stale

    last_id = 0
    while len(stale) < needed:
        batch = (
            build_downloaded_repair_query(session, tenant_id)
            .filter(ArchiveMessage.id > last_id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for msg, media_file in batch:
            if is_downloaded_media_file_stale(media_file):
                stale.append((msg, media_file))
                if len(stale) >= needed:
                    break
        last_id = batch[-1][0].id
        if len(batch) < batch_size:
            break
    return stale


def select_candidates(
    session: Session,
    tenant_id: str,
    retry: bool,
    limit: int,
    since_ms: Optional[int] = None,
    newest_first: bool = False,
) -> Tuple[List[ArchiveMessage], List[Tuple[ArchiveMessage, MediaFile]], int]:
    """Return (actionable, stale_repairs, total_eligible).

    actionable: fresh-download candidates (see build_candidate_query),
    filtered by since_ms and ordered per newest_first (RND-151), up to
    `limit`.
    stale_repairs: (message, media_file) pairs currently marked
    "downloaded" that fail the servability check, filling whatever budget
    remains after `actionable` — capped so the repair scan can never push
    the total above `limit` nor crowd out fresh candidates. The scan
    itself (_scan_for_stale_downloaded) is not limited by that budget
    until *after* servability filtering, so a valid/servable "downloaded"
    row can never consume the quota or starve a later stale one out of it.
    total_eligible: count of *all* fresh candidates ignoring both `limit`
    and since_ms (for --count-only reporting) — unaffected by RND-151's
    recency window so it keeps meaning "total eligible ignoring window".
    """
    total_eligible = build_candidate_query(session, tenant_id, retry).count()
    actionable = (
        build_candidate_query(
            session, tenant_id, retry, since_ms=since_ms, newest_first=newest_first
        )
        .limit(limit)
        .all()
    )

    remaining_budget = limit - len(actionable)
    stale_repairs = _scan_for_stale_downloaded(session, tenant_id, remaining_budget)

    return actionable, stale_repairs, total_eligible


def _since_ms_cutoff(since_hours: float) -> int:
    """Convert --since-hours into an epoch-ms cutoff using the current UTC
    time (time.time() is already timezone-independent — never derived from
    local wall-clock formatting)."""
    return int(time.time() * 1000) - int(since_hours * 3600 * 1000)


def count_candidates_with_existing_media_row(session: Session, tenant_id: str) -> int:
    """Count of tenant-scoped, image-only, decrypted messages that already
    have *any* media_files row (any download_status) — safe, aggregate-only
    visibility for --count-only reporting; never touches sdkfileid/paths."""
    return (
        session.query(ArchiveMessage)
        .join(MediaFile, MediaFile.archive_message_id == ArchiveMessage.id)
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtype == "image",
            ArchiveMessage.sdkfileid.isnot(None),
            ArchiveMessage.sdkfileid != "",
        )
        .count()
    )


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


def _media_root() -> Path:
    raw = os.environ.get("STORAGE_LOCAL_PATH", "").strip()
    if not raw:
        print("[FAIL] STORAGE_LOCAL_PATH is not set", flush=True)
        sys.exit(1)
    root = Path(raw)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def target_paths(media_root: Path, tenant_id: str, archive_message_id: int) -> Tuple[Path, Path]:
    """Return (base_path_without_extension, part_path) under
    <media_root>/tenants/<tenant_id>/images/. The .part path is fixed
    regardless of the eventual detected type so on-failure cleanup is
    unambiguous."""
    directory = media_root / "tenants" / tenant_id / "images"
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / str(archive_message_id)
    part_path = directory / f"{archive_message_id}.part"
    return base, part_path


def _safe_unlink(path: Path) -> None:
    """Best-effort remove; never raises, never prints the path."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# media_files state transitions
# ---------------------------------------------------------------------------


def get_or_reset_media_file(
    session: Session, sdkfileid: str, archive_message_id: int
) -> Optional[MediaFile]:
    """Return the media_files row for sdkfileid, creating it (or resetting
    an existing pending/failed/stale-downloaded row) to
    download_status="pending" before an attempt begins.

    media_files.sdkfileid is globally unique with no tenant_id column on
    this table yet. An existing row is only ever safe to reuse when its
    archive_message_id already matches the message about to be processed.
    If an existing row's archive_message_id differs — a genuine sdkfileid
    collision across messages/tenants, or an inconsistent (msg, media_file)
    pairing — reusing it would silently reassign someone else's media row,
    so this returns None. Callers must treat None as "fail this candidate
    safely" and must not touch the conflicting row.
    """
    row = session.query(MediaFile).filter(MediaFile.sdkfileid == sdkfileid).first()
    if row is None:
        row = MediaFile(
            sdkfileid=sdkfileid,
            archive_message_id=archive_message_id,
            download_status="pending",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    if row.archive_message_id != archive_message_id:
        return None

    row.download_status = "pending"
    row.local_path = None
    row.oss_key = None
    row.file_size = None
    session.commit()
    session.refresh(row)
    return row


def download_one(
    lib,
    handle,
    media_root: Path,
    tenant_id: str,
    archive_message_id: int,
    sdkfileid: str,
    timeout: int,
) -> Tuple[str, Optional[str]]:
    """Download one image message's media.

    Returns (outcome, detail): outcome is "downloaded" or "failed"; detail
    is the final file path as a string when downloaded, or a short internal
    diagnostic tag (never an identifier/path/payload fragment) when failed.

    The .part temp file is always cleaned up on any failure path — a
    single try/finally guard covers download errors, write failures,
    unsupported byte signatures, and rename failures alike, so there is
    exactly one place that can leak a stray .part file rather than one per
    failure branch.
    """
    base_path, part_path = target_paths(media_root, tenant_id, archive_message_id)
    outcome = "failed"
    detail: Optional[str] = "unknown_error"

    try:
        try:
            chunks = bytearray()
            for chunk in wecom_sdk.iter_media_chunks(lib, handle, sdkfileid, timeout=timeout):
                chunks.extend(chunk)
            data = bytes(chunks)
        except wecom_sdk.SdkMediaError:
            detail = "sdk_error"
            return outcome, detail
        except Exception:
            detail = "download_error"
            return outcome, detail

        if not data:
            detail = "empty_payload"
            return outcome, detail

        try:
            part_path.write_bytes(data)
        except OSError:
            detail = "write_error"
            return outcome, detail

        ext = detect_image_type_from_bytes(data)
        if ext is None:
            detail = "unsupported_type"
            return outcome, detail

        final_path = base_path.with_suffix(ext)
        try:
            os.replace(part_path, final_path)
        except OSError:
            detail = "rename_error"
            return outcome, detail

        outcome, detail = "downloaded", str(final_path)
        return outcome, detail
    finally:
        if outcome != "downloaded":
            _safe_unlink(part_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot WeCom image media download (RND-147)"
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Report candidate counts only; perform no writes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Max messages to process this run (default {_DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Also retry media_files rows with download_status='failed'",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help=(
            "Only select candidates with msgtime within the last N hours "
            "(RND-151; recommended production value: 72)"
        ),
    )
    parser.add_argument(
        "--newest-first",
        action="store_true",
        help=(
            "Order candidates by msgtime descending (id descending as tie-"
            "breaker) instead of oldest-first (RND-151)"
        ),
    )
    args = parser.parse_args()

    if args.limit <= 0:
        print("[FAIL] --limit must be a positive integer", flush=True)
        sys.exit(1)

    if args.since_hours is not None and args.since_hours <= 0:
        print("[FAIL] --since-hours must be a positive number", flush=True)
        sys.exit(1)

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
        _run(args)
    finally:
        _release_lock(lock_fd)


def _run(args: argparse.Namespace) -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")

    engine = create_engine(database_url)

    with Session(engine) as session:
        tenant_id = _require_tenant_id(session, corp_id)

        since_ms = _since_ms_cutoff(args.since_hours) if args.since_hours is not None else None

        actionable, stale_repairs, total_eligible = select_candidates(
            session,
            tenant_id,
            args.retry,
            args.limit,
            since_ms=since_ms,
            newest_first=args.newest_first,
        )
        candidates: List[ArchiveMessage] = list(actionable) + [m for m, _mf in stale_repairs]

        print(f"[INFO] candidate_total: {total_eligible}", flush=True)
        print(f"[INFO] candidate_selected: {len(candidates)}", flush=True)
        if stale_repairs:
            print(
                f"[INFO] stale_downloaded_repair_selected: {len(stale_repairs)}",
                flush=True,
            )
        if args.count_only:
            print(
                f"[INFO] candidate_ordering: "
                f"{'newest_first' if args.newest_first else 'oldest_first'}",
                flush=True,
            )
            if since_ms is not None:
                within_window = build_candidate_query(
                    session, tenant_id, args.retry, since_ms=since_ms
                ).count()
                print(f"[INFO] since_hours: {args.since_hours}", flush=True)
                print(f"[INFO] candidates_in_window: {within_window}", flush=True)
                print(
                    f"[INFO] candidates_excluded_by_window: {total_eligible - within_window}",
                    flush=True,
                )
            existing_media_count = count_candidates_with_existing_media_row(session, tenant_id)
            print(
                f"[INFO] candidates_with_existing_media_row: {existing_media_count}",
                flush=True,
            )
            print("[PASS] count-only mode — no writes performed", flush=True)
            sys.exit(0)

        if not candidates:
            print("[PASS] no candidates to process", flush=True)
            sys.exit(0)

        lib_path = _require_env("WECOM_SDK_LIB_PATH")
        secret = _require_env("WECOM_ARCHIVE_SECRET")
        timeout = _optional_int_env("WECOM_MEDIA_TIMEOUT", _DEFAULT_TIMEOUT)
        media_root = _media_root()

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
            media_file = get_or_reset_media_file(session, msg.sdkfileid, msg.id)
            if media_file is None:
                failed += 1
                reason_counts["media_identity_conflict"] = (
                    reason_counts.get("media_identity_conflict", 0) + 1
                )
                continue

            outcome, detail = download_one(
                lib, handle, media_root, tenant_id, msg.id, msg.sdkfileid, timeout
            )

            try:
                if outcome == "downloaded":
                    media_file.file_type = "image"
                    media_file.download_status = "downloaded"
                    media_file.local_path = detail
                    media_file.file_size = os.path.getsize(detail)
                    media_file.oss_key = None
                    session.commit()
                    downloaded += 1
                else:
                    media_file.download_status = "failed"
                    media_file.local_path = None
                    media_file.oss_key = None
                    session.commit()
                    failed += 1
                    reason_counts[detail or "unknown"] = (
                        reason_counts.get(detail or "unknown", 0) + 1
                    )
            except Exception:
                session.rollback()
                if outcome == "downloaded" and detail:
                    # DB commit failed after a successful download+rename —
                    # remove the orphaned file rather than leave a file on
                    # disk with no corresponding media_files record.
                    _safe_unlink(Path(detail))
                failed += 1
                reason_counts["db_commit_error"] = reason_counts.get("db_commit_error", 0) + 1

        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass

        print(f"[INFO] downloaded: {downloaded}", flush=True)
        print(f"[INFO] failed: {failed}", flush=True)
        if reason_counts:
            diag = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
            print(f"[INFO] failed_reasons: {diag}", flush=True)
        print("[PASS] download_wecom_image_media_once completed", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
