#!/usr/bin/env python3
"""
One-shot, repeatable: migrate historical Local-backed media_files rows to
Qiniu Kodo (RND-186).

RND-186 fix (media scope revision): this migrates ANY media_files row with a
recognized, content-verified media type — image, video, voice, or file —
not images only. A row's own MediaFile.file_type (already a generic column,
set by whichever download worker created the row) selects candidates;
app.media_storage.detect_media_signature_from_bytes() content-verifies the
actual bytes against that claim before anything is uploaded. Today the only
download worker in this codebase (download_wecom_image_media_once.py) ever
produces file_type="image" rows — this script does not change that, and
does not gain the ability to serve video/voice/file media (the media route
still only serves images; see app/media_storage.py's module docstring and
the RND-186 fix report's "Remaining Limitations") — it is forward-compatible
scaffolding: the moment a future download worker starts writing
file_type="video"/"voice"/"file" rows, this script migrates them with zero
further changes.

This is a migration TOOL, not a one-time migration event: it is designed to
be run manually, repeatedly, in small batches, safely interrupted, and
resumed, without ever risking the media a user is actively viewing. It does
not change how NEW media is downloaded (scripts/download_wecom_image_media_once.py
is untouched) and it does not change how media is served (app/media_storage.py's
serving-side functions, the media route, and the RND-187 signed-URL route
are untouched) — it only copies bytes for already-downloaded rows and, on
confirmed success, flips that row's storage_backend/storage_ref (migration
0005's columns) from "local" to "qiniu_kodo" so every existing read path
resolves it through QiniuStorageProvider from then on with no code change
required.

Usage (from backend/):
    python scripts/migrate_local_media_to_qiniu.py --count-only
    python scripts/migrate_local_media_to_qiniu.py --dry-run --limit 20
    python scripts/migrate_local_media_to_qiniu.py --limit 20 --batch-size 10
    python scripts/migrate_local_media_to_qiniu.py --limit 50 --retry

Required environment variables (only DATABASE_URL / WECOM_CORP_ID are needed
for --count-only, since that mode never touches storage):
    DATABASE_URL          PostgreSQL connection string
    WECOM_CORP_ID         WeCom corporation ID (resolves the active tenant)
    STORAGE_LOCAL_PATH    Local media root — must still be readable; this
                           tool reads FROM local, it does not delete it
    QINIU_ACCESS_KEY, QINIU_SECRET_KEY, QINIU_BUCKET, QINIU_DOMAIN
                          Qiniu credentials — the migration target is always
                          Qiniu Kodo, independent of MEDIA_STORAGE_PROVIDER
                          (this tool works even before the deployment's
                          default WRITE provider has been switched to Qiniu)

Optional environment variables:
    QINIU_REGION, QINIU_TIMEOUT_SECONDS   Same meaning as for the download
                                           worker / app.media_storage.
    MEDIA_MIGRATION_LOCK_PATH             Lock file path (default:
                                           /srv/apps/wecom-archive-365/shared/run/wecom-media-migration.lock)

Tenant scoping: resolved server-side from WECOM_CORP_ID via
tenant_wecom_configs, exactly like every other one-shot script in this
codebase (sync_wecom_archive_once.py, download_wecom_image_media_once.py).
There is no --tenant-id flag — tenant_id is never accepted from
caller-supplied input, and every query/write in this script is scoped by
that resolved tenant_id (tenant isolation).

Candidate selection (tenant-scoped) — see build_candidate_query():
    storage_backend == "local", download_status == "downloaded" (only rows
    that actually have bytes to migrate — pending/failed downloads have
    nothing to copy), file_type IN app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES
    ("image"/"video"/"voice"/"file" — a row with no file_type, or some
    future/unrecognized value, is excluded outright, not merely skipped —
    "the system can identify the media type" is an admission criterion, per
    RND-186), and migration_status IS NULL (never attempted), or — only
    with --retry — migration_status == "failed". A row already migrated
    (storage_backend == "qiniu_kodo") is never selected: it is excluded by
    the storage_backend filter itself, which is what makes re-running this
    script with no flags a true no-op for already-migrated rows — zero
    reads, zero uploads, zero writes for those rows, not just an idempotent
    overwrite.

    Rows that are explicitly NOT candidates (never even considered, not
    just skipped at migration time): messages with no media_files row at
    all (miniprogram/link/location/card/structured messages never get one
    — the query starts from media_files, not messages), rows whose
    download failed (download_status != "downloaded"), and rows whose
    file_type isn't in the supported set.

Per-row migration (see migrate_one() / _read_and_identify()):
    1. Resolve the row's local reference (storage_ref, falling back to
       legacy local_path) through LocalStorageProvider's own safe-path
       resolution (app/media_storage.py) — the exact same traversal/
       existence safety the serving route already relies on. Reused, not
       reimplemented. A missing/unresolvable local file is never migrated
       (RND-186 explicit exclusion: "no local file entity").
    2. Read the file's bytes and content-verify the media type via
       app.media_storage.detect_media_signature_from_bytes() — never trust
       the stored file_type, an extension, or msgtype alone (same
       discipline the image-only download worker already applies). A
       signature detect_media_signature_from_bytes() doesn't recognize at
       all is never migrated ("cannot safely identify type" — RND-186
       explicit exclusion). A signature that IS recognized but doesn't
       match the row's own file_type ("media_type_mismatch") is also never
       migrated — a stale/corrupted file_type must not silently relabel
       what actually gets uploaded.
    3. Upload the bytes to Qiniu at a deterministic key built by
       app.media_storage.build_tenant_media_key(tenant_id,
       media_key_category(media_type), archive_message_id, ext) —
       "tenants/{tenant_id}/{images,videos,voice,files}/{id}{ext}" — the
       SAME key format the download worker already uses for Qiniu image
       writes, generalized by media_type so a migrated row of any
       supported type is byte-for-byte indistinguishable in shape from a
       row that a future type-specific download worker wrote straight to
       Qiniu. Deterministic: re-uploading (e.g. after an interrupted run)
       overwrites the same object in place, never accumulates duplicates.
    4. Only on a confirmed-successful upload does a single DB commit flip
       storage_backend/storage_ref/file_size/mime_type/checksum_sha256/
       bucket (together, atomically) to the new Qiniu reference and
       record migration_status="migrated". file_size/mime_type/checksum
       are all recomputed from the exact bytes just uploaded — never
       trusted from the row's pre-existing (download-time) file_size, and
       never inferred from an extension. The local file and
       media_files.local_path are left completely untouched — the whole
       point of this two-step design is that a Qiniu upload failure,
       partway-through crash, or process kill leaves the row exactly as
       servable as it was before this script ever ran (see "Failure and
       Resumability" below).

Storage metadata persistence (RND-186 QA fix — migration 0007):
    A migrated row's bucket/mime_type/checksum_sha256 are first-class
    media_files columns, not encoded into storage_ref. storage_ref remains
    the sole authoritative object key / local path reference (unchanged
    from migration 0005) — deliberately NOT duplicated into a second
    "object_key" column, since two columns holding the same value would be
    a drift risk with no offsetting benefit. bucket is read directly from
    the provider instance that performed the upload
    (QiniuStorageProvider.bucket), never re-read from QINIU_BUCKET
    independently, so it can never drift from what was actually used.

Failure and Resumability:
    - A failure at any step (missing/unsafe local file, corrupted/
      unrecognised bytes, Qiniu upload error) leaves storage_backend/
      storage_ref untouched — the row is still "local" and still fully
      servable exactly as before. Only migration_status="failed" (plus a
      short, sanitized migration_error tag) is recorded. This is what
      guarantees "never affects live media access": a bad migration
      attempt is invisible to every read path.
    - Unlike the download worker, a successful Qiniu upload followed by a
      DB commit failure needs NO cleanup/rollback of the just-uploaded
      Qiniu object: the object sits at a deterministic key that nothing
      yet references (storage_backend is still "local"), so it is neither
      "orphaned garbage" nor reachable through any authorized read path.
      The next run (or a --retry re-run once migration_status="failed" is
      recorded) simply re-uploads to that same key (a harmless overwrite)
      and retries the commit.
    - A run interrupted (crash, kill, deploy) mid-batch leaves every
      already-committed row migrated and every not-yet-reached row
      untouched ("local", migration_status unchanged) — safe to re-run
      immediately with the same flags.

Concurrency: acquires a non-blocking process-level file lock (same
fcntl.flock pattern as download_wecom_image_media_once.py /
run_archive_worker_once.py) before touching the database. If another
instance already holds the lock, this exits 0 immediately without
selecting candidates or uploading anything.

Local Cleanup (explicitly NOT implemented by this script — see
find_local_cleanup_candidates()): once a row is migrated
(storage_backend="qiniu_kodo", migration_status="migrated") its local file
is no longer read by anything, but this script deliberately never deletes
it — the current operational posture keeps local files as a rollback
safety net. find_local_cleanup_candidates() is the query a future cleanup
tool should reuse verbatim; this script only ever reports the candidate
count (via --count-only), never deletes.

Exit codes:
    0  Success (including --count-only, --dry-run, "nothing to do", and
       "lock held")
    1  Any fatal failure (missing env, DB error, misconfigured Qiniu)

Safety constraints:
    - Never prints sdkfileid, local_path, storage_ref/object key, or any
      message body/content — only aggregate counts and short diagnostic
      tags, matching download_wecom_image_media_once.py's discipline.
    - --count-only and --dry-run perform zero writes (DB, local filesystem,
      or Qiniu) and zero Qiniu API calls.
    - --dry-run still reads local bytes and content-verifies the media
      signature (so its report reflects exactly what a live run would
      attempt) but never calls the Qiniu SDK and never touches the
      database.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Query, Session

from app.db.models import MediaFile, TenantWecomConfig
from app.media_storage import (
    SUPPORTED_MIGRATION_MEDIA_TYPES,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageProvider,
    MediaStorageUnavailable,
    build_tenant_media_key,
    compute_sha256_checksum,
    detect_media_signature_from_bytes,
    get_media_storage_provider_for_backend,
    media_key_category,
)

_DEFAULT_LIMIT = 50
_DEFAULT_BATCH_SIZE = 20
_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-media-migration.lock"

_SOURCE_BACKEND = "local"
_TARGET_BACKEND = "qiniu_kodo"


# ---------------------------------------------------------------------------
# Env helpers (same shape as download_wecom_image_media_once.py)
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Concurrent-run guard
# ---------------------------------------------------------------------------


def _acquire_lock(lock_path: str) -> Optional[int]:
    """Acquire a non-blocking exclusive file lock. Returns the open file
    descriptor on success, or None if another process already holds the
    lock. Never blocks, never prints the lock path itself."""
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
# Tenant resolution (same pattern as every other one-shot script)
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


def build_candidate_query(session: Session, tenant_id: str, retry: bool) -> Query:
    """Tenant-scoped query for media_files rows eligible for a local ->
    Qiniu migration attempt: currently Local-backed, fully downloaded (has
    bytes to migrate), a recognized media type (RND-186: image/video/voice/
    file — app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES; a row with no
    file_type, or some unrecognized future value, is excluded outright —
    "the system can identify the media type" is an admission criterion, not
    just a per-row skip reason), and either never attempted
    (migration_status IS NULL) or — only with --retry — previously failed
    (migration_status == "failed").

    Never includes an already-migrated row: storage_backend == "qiniu_kodo"
    excludes it by construction, so this query alone is what makes
    "skip already migrated" and "re-running is a no-op" true without any
    extra flag.
    """
    query = session.query(MediaFile).filter(
        MediaFile.tenant_id == tenant_id,
        MediaFile.storage_backend == _SOURCE_BACKEND,
        MediaFile.download_status == "downloaded",
        MediaFile.file_type.in_(sorted(SUPPORTED_MIGRATION_MEDIA_TYPES)),
    )
    if retry:
        query = query.filter(
            (MediaFile.migration_status.is_(None))
            | (MediaFile.migration_status == "failed")
        )
    else:
        query = query.filter(MediaFile.migration_status.is_(None))
    return query.order_by(MediaFile.id)


def _select_candidates(
    session: Session, tenant_id: str, retry: bool, limit: int, batch_size: int
) -> List[MediaFile]:
    """Paginate through build_candidate_query in batch_size-sized pages
    (same cursor-by-id pattern as
    download_wecom_image_media_once.py's _scan_for_stale_downloaded) until
    `limit` rows are collected or candidates are exhausted. Pagination
    keeps memory bounded independent of how large --limit is; batch_size is
    purely a fetch-chunk size, not a commit-grouping unit (this script
    commits per row — see migrate_one()/_run())."""
    selected: List[MediaFile] = []
    if limit <= 0:
        return selected

    last_id = 0
    while len(selected) < limit:
        page = (
            build_candidate_query(session, tenant_id, retry)
            .filter(MediaFile.id > last_id)
            .limit(batch_size)
            .all()
        )
        if not page:
            break
        for row in page:
            selected.append(row)
            if len(selected) >= limit:
                break
        last_id = page[-1].id
        if len(page) < batch_size:
            break
    return selected


def count_candidates(session: Session, tenant_id: str, retry: bool) -> int:
    """Count of all eligible candidates ignoring --limit (for --count-only
    reporting)."""
    return build_candidate_query(session, tenant_id, retry).count()


def count_already_migrated(session: Session, tenant_id: str) -> int:
    """Tenant-scoped count of rows already migrated — visibility only, used
    by --count-only to report "skip already migrated" without touching any
    of those rows."""
    return (
        session.query(MediaFile)
        .filter(
            MediaFile.tenant_id == tenant_id,
            MediaFile.storage_backend == _TARGET_BACKEND,
            MediaFile.migration_status == "migrated",
        )
        .count()
    )


# ---------------------------------------------------------------------------
# Local Cleanup extension point (RND-186 scope: report only, never delete)
# ---------------------------------------------------------------------------


def is_local_cleanup_candidate(media_file) -> bool:
    """True iff media_file has been migrated to Qiniu and still has a local
    file reference lingering. This is the exact predicate a future "delete
    now-unreferenced local files" tool should reuse — this script itself
    never deletes anything, it only ever reports the count (--count-only),
    keeping local files as a rollback safety net per current operational
    policy. Duck-typed like resolve_media_file_state()."""
    return (
        getattr(media_file, "storage_backend", None) == _TARGET_BACKEND
        and getattr(media_file, "migration_status", None) == "migrated"
        and bool(getattr(media_file, "local_path", None))
    )


def find_local_cleanup_candidates(session: Session, tenant_id: str) -> Query:
    """Tenant-scoped query mirroring is_local_cleanup_candidate() at the SQL
    level — the query a future local-cleanup script should start from.
    Exposed and tested here so that future tool has zero design work left:
    every row this query returns is safe to consider for local-file
    deletion (bytes are already durably in Qiniu); this script does not act
    on it."""
    return session.query(MediaFile).filter(
        MediaFile.tenant_id == tenant_id,
        MediaFile.storage_backend == _TARGET_BACKEND,
        MediaFile.migration_status == "migrated",
        MediaFile.local_path.isnot(None),
    )


def count_local_cleanup_candidates(session: Session, tenant_id: str) -> int:
    return find_local_cleanup_candidates(session, tenant_id).count()


# ---------------------------------------------------------------------------
# Per-row migration
# ---------------------------------------------------------------------------


def _resolve_local_ref(media_file: MediaFile) -> Optional[str]:
    """The row's own local reference: storage_ref first (authoritative per
    migration 0005), falling back to legacy local_path only if storage_ref
    was somehow never backfilled — mirrors
    app.media_storage.resolve_effective_storage_reference's fallback rule,
    but only for the already-known-local case this script only ever
    selects."""
    return media_file.storage_ref or media_file.local_path


def target_storage_ref(
    tenant_id: str, archive_message_id: int, media_type: str, ext: str
) -> str:
    """Deterministic Qiniu object key —
    "tenants/{tenant_id}/{images,videos,voice,files}/{id}{ext}" — the SAME
    format download_wecom_image_media_once.py's target_storage_refs()
    already produces for a fresh Qiniu image download, generalized by
    media_type (app.media_storage.media_key_category) so a migrated row of
    any supported type is indistinguishable in shape from one a future
    type-specific download worker writes straight to Qiniu. Deterministic
    per (tenant_id, archive_message_id): re-migrating (e.g. after an
    interrupted run) overwrites the same object, never accumulates
    duplicates."""
    category = media_key_category(media_type)
    return build_tenant_media_key(tenant_id, category, str(archive_message_id), suffix=ext)


class MigrationSkip(Exception):
    """Raised internally by _read_and_identify()/migrate_one() for an
    expected, non-fatal reason the candidate cannot be migrated this
    attempt: "missing_local_reference", "local_file_missing",
    "unrecognized_signature" (the bytes don't match any known, allow-listed
    media signature — RND-186 explicit exclusion: "无法安全识别类型的文件"),
    "media_type_mismatch" (the bytes DO match a known signature, but for a
    different media_type than the row's own file_type — a stale/corrupted
    file_type must never silently relabel what gets uploaded), or
    "qiniu_upload_error". Carries only a short, sanitized diagnostic tag —
    never a path, sdkfileid, or raw exception string."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MigrationResult(NamedTuple):
    """Everything a successful migrate_one() call determined, for the
    caller to persist atomically. file_size/mime_type/checksum_sha256 are
    all derived from the exact bytes that were uploaded — never trusted
    from the row's pre-existing (download-time) file_size, never inferred
    from an extension (RND-186 QA fix)."""

    storage_ref: str
    media_type: str
    mime_type: str
    checksum_sha256: str
    file_size: int
    bucket: Optional[str]


def _read_and_identify(source_provider: MediaStorageProvider, media_file: MediaFile):
    """Resolve, read, and content-verify a candidate's local bytes — the
    part of migration shared by --dry-run (which stops here) and the live
    path (migrate_one(), which continues on to upload), so the two can
    never disagree about what counts as migratable.

    Returns (data, MediaSignatureMatch). Raises MigrationSkip for every
    expected failure reason; never a bare exception.
    """
    local_ref = _resolve_local_ref(media_file)
    if not local_ref:
        raise MigrationSkip("missing_local_reference")

    if not source_provider.supports_local_path():
        raise MigrationSkip("missing_local_reference")
    local_path = source_provider.get_local_path(local_ref)
    if local_path is None:
        raise MigrationSkip("local_file_missing")

    data = local_path.read_bytes()
    match = detect_media_signature_from_bytes(data)
    if match is None:
        raise MigrationSkip("unrecognized_signature")

    declared_type = getattr(media_file, "file_type", None)
    if declared_type and declared_type != match.media_type:
        raise MigrationSkip("media_type_mismatch")

    return data, match


def migrate_one(
    source_provider: MediaStorageProvider,
    target_provider: MediaStorageProvider,
    tenant_id: str,
    media_file: MediaFile,
) -> MigrationResult:
    """Migrate a single row's bytes from Local to Qiniu.

    Returns a MigrationResult on success. Raises MigrationSkip (never a
    bare exception) for every expected failure reason — see
    _read_and_identify() and MigrationSkip's docstring for the full list.
    Callers must catch MigrationSkip and treat it as "leave
    storage_backend/storage_ref untouched, record migration_status=failed".

    This function performs NO database write — it only touches the local
    filesystem (read-only) and Qiniu (a single upload). The caller is
    responsible for the DB transition, so a caller that decides not to
    commit (e.g. --dry-run) never has to undo anything here.
    """
    data, match = _read_and_identify(source_provider, media_file)

    target_ref = target_storage_ref(
        tenant_id, media_file.archive_message_id, match.media_type, match.extension
    )
    try:
        target_provider.save_bytes(target_ref, data)
    except (MediaStorageOperationError, MediaStorageUnavailable, MediaStorageConfigurationError):
        raise MigrationSkip("qiniu_upload_error")

    return MigrationResult(
        storage_ref=target_ref,
        media_type=match.media_type,
        mime_type=match.mime_type,
        checksum_sha256=compute_sha256_checksum(data),
        file_size=len(data),
        bucket=getattr(target_provider, "bucket", None),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot, repeatable Local -> Qiniu historical media migration (RND-186)"
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Report candidate counts only; perform no reads or writes against storage",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read and validate candidates exactly as a live run would, but "
            "never call Qiniu and never write to the database"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Max rows to migrate this run (default {_DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=(
            f"Candidate fetch page size (default {_DEFAULT_BATCH_SIZE}); "
            "does not change commit granularity — each row commits "
            "independently"
        ),
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Also retry media_files rows with migration_status='failed'",
    )
    args = parser.parse_args()

    if args.limit <= 0:
        print("[FAIL] --limit must be a positive integer", flush=True)
        sys.exit(1)
    if args.batch_size <= 0:
        print("[FAIL] --batch-size must be a positive integer", flush=True)
        sys.exit(1)

    lock_path = os.environ.get("MEDIA_MIGRATION_LOCK_PATH", "").strip() or _DEFAULT_LOCK_PATH
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
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


def _build_providers() -> Tuple[MediaStorageProvider, MediaStorageProvider]:
    """Source is always explicitly "local", target is always explicitly
    "qiniu_kodo" — independent of MEDIA_STORAGE_PROVIDER/STORAGE_BACKEND,
    so this tool works whether or not the deployment's default WRITE
    provider has been switched to Qiniu yet."""
    try:
        source = get_media_storage_provider_for_backend(_SOURCE_BACKEND)
        target = get_media_storage_provider_for_backend(_TARGET_BACKEND)
    except MediaStorageConfigurationError as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)

    if not source.supports_local_path():
        print("[FAIL] local storage provider misconfigured", flush=True)
        sys.exit(1)
    return source, target


def _run(args: argparse.Namespace) -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")

    engine = create_engine(database_url)

    with Session(engine) as session:
        tenant_id = _require_tenant_id(session, corp_id)

        total_eligible = count_candidates(session, tenant_id, args.retry)
        already_migrated = count_already_migrated(session, tenant_id)
        cleanup_candidates = count_local_cleanup_candidates(session, tenant_id)

        print(f"[INFO] candidate_total: {total_eligible}", flush=True)
        print(f"[INFO] already_migrated: {already_migrated}", flush=True)
        print(f"[INFO] local_cleanup_candidates: {cleanup_candidates}", flush=True)

        if args.count_only:
            print("[PASS] count-only mode — no reads or writes performed", flush=True)
            sys.exit(0)

        candidates = _select_candidates(
            session, tenant_id, args.retry, args.limit, args.batch_size
        )
        print(f"[INFO] candidate_selected: {len(candidates)}", flush=True)

        if not candidates:
            print("[PASS] no candidates to migrate", flush=True)
            sys.exit(0)

        source_provider, target_provider = _build_providers()

        migrated = 0
        failed = 0
        would_migrate = 0
        reason_counts: dict[str, int] = {}
        processed_in_batch = 0

        for media_file in candidates:
            if args.dry_run:
                try:
                    _read_and_identify(source_provider, media_file)
                    would_migrate += 1
                except MigrationSkip as skip:
                    failed += 1
                    reason_counts[skip.reason] = reason_counts.get(skip.reason, 0) + 1
                processed_in_batch += 1
                if processed_in_batch % args.batch_size == 0:
                    print(
                        f"[INFO] progress: {processed_in_batch}/{len(candidates)}",
                        flush=True,
                    )
                continue

            now = datetime.now(timezone.utc)
            try:
                result = migrate_one(source_provider, target_provider, tenant_id, media_file)
            except MigrationSkip as skip:
                media_file.migration_status = "failed"
                media_file.migration_attempted_at = now
                media_file.migration_error = skip.reason
                session.commit()
                failed += 1
                reason_counts[skip.reason] = reason_counts.get(skip.reason, 0) + 1
                processed_in_batch += 1
                if processed_in_batch % args.batch_size == 0:
                    print(
                        f"[INFO] progress: {processed_in_batch}/{len(candidates)}",
                        flush=True,
                    )
                continue

            # Upload already confirmed successful at this point (migrate_one
            # only returns on a confirmed Qiniu success). This try body
            # performs only the database persistence — no further storage
            # call — so a failure here is a genuine DB commit failure, not
            # a transient storage hiccup. No compensating Qiniu delete is
            # needed on failure: the uploaded object sits at a deterministic
            # key nothing yet references (storage_backend is still "local"),
            # so it is harmlessly overwritten by the next attempt rather
            # than orphaned (see module docstring, "Failure and
            # Resumability"). file_size/mime_type/checksum_sha256 all come
            # from `result` — derived from the exact bytes just uploaded,
            # never the row's pre-existing (download-time) file_size and
            # never inferred from an extension (RND-186 QA fix).
            try:
                media_file.storage_backend = _TARGET_BACKEND
                media_file.storage_ref = result.storage_ref
                media_file.file_size = result.file_size
                media_file.mime_type = result.mime_type
                media_file.checksum_sha256 = result.checksum_sha256
                media_file.bucket = result.bucket
                media_file.migration_status = "migrated"
                media_file.migration_attempted_at = now
                media_file.migration_error = None
                session.commit()
                migrated += 1
            except Exception:
                session.rollback()
                failed += 1
                reason_counts["db_commit_error"] = reason_counts.get("db_commit_error", 0) + 1

            processed_in_batch += 1
            if processed_in_batch % args.batch_size == 0:
                print(f"[INFO] progress: {processed_in_batch}/{len(candidates)}", flush=True)

        if args.dry_run:
            print(f"[INFO] would_migrate: {would_migrate}", flush=True)
            print(f"[INFO] would_fail: {failed}", flush=True)
        else:
            print(f"[INFO] migrated: {migrated}", flush=True)
            print(f"[INFO] failed: {failed}", flush=True)
        if reason_counts:
            diag = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items()))
            print(f"[INFO] failed_reasons: {diag}", flush=True)
        print(
            "[PASS] migrate_local_media_to_qiniu "
            f"{'dry-run ' if args.dry_run else ''}completed",
            flush=True,
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
