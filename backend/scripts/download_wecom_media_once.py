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
    python scripts/download_wecom_media_once.py --since-hours 72 --newest-first --limit 20 --retry --trigger-source timer

Required environment variables (only DATABASE_URL plus either
WECOM_TENANT_ID or WECOM_CORP_ID are needed for --count-only, since that
mode never touches the SDK or filesystem):
    DATABASE_URL          PostgreSQL connection string
    WECOM_TENANT_ID       Per-tenant mode: resolves the active tenant config
                          row and uses its stored CorpID / archive secret
                          (FIELD_ENCRYPTION_KEY must be set)
    WECOM_CORP_ID         Legacy mode: WeCom corporation ID (resolves the
                          active tenant)
    WECOM_SDK_LIB_PATH    Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_ARCHIVE_SECRET  WeCom conversation archive secret (legacy mode)
    STORAGE_LOCAL_PATH    Media storage root (same variable RND-144 reads)

Optional environment variables:
    WECOM_MEDIA_TIMEOUT      Per-chunk SDK call timeout in seconds (default 30)
    MEDIA_DOWNLOAD_LOCK_PATH Lock file path (default:
                             /srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock)
    EVENT_MEDIA_DOWNLOAD_RETRY_COUNT
                             Durable retry cap used with --retry (default 3)
    EVENT_MEDIA_DOWNLOAD_BACKOFF_SECONDS
                             Exponential retry backoff base (default 30)

Tenant scoping: resolved server-side from WECOM_TENANT_ID or WECOM_CORP_ID
via tenant_wecom_configs — there is no --tenant-id flag; this codebase's
rule is that tenant_id is never accepted from caller-supplied input.

Concurrency: acquires a non-blocking process-level file lock (fcntl.flock)
before touching the database at all. If another invocation already holds
the lock, this exits 0 immediately without selecting candidates or
downloading anything — never blocks waiting for the lock. Because there is
now only one pipeline, there is only one lock: a single systemd
timer/service (or cron entry) should own this script, though an operator
can still run --types-narrowed invocations sequentially without
contention (they share the same lock, so overlapping runs safely no-op
rather than double-processing). With ``--retry``, every actual SDK attempt
is recorded before download and failed rows obey the durable cap/backoff;
fresh/pending media remains eligible independently of old failures.

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
    - Every exit emits one aggregate lifecycle line with result, safe error
      class, completed_at, duration, CPU, and RSS; exception/configuration
      text is never echoed into that line or a failure line.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import resource
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import AuditAction, AuditObjectType
from app.db.models import ArchiveMessage, AuditLog, MediaFile, Tenant, TenantWecomConfig
from app.media_download import (
    GENERIC_DOWNLOAD_MSGTYPES,
    count_candidates_with_existing_media_row,
    select_candidates,
    select_nested_media_candidates,
)
from app.media_storage import (
    LocalStorageProvider,
    MediaStorageConfigurationError,
    MediaStorageProvider,
    get_configured_write_backend_name,
    get_media_storage_provider,
)
from app.sdk import wecom_sdk
from app.services.media_worker import (  # noqa: F401 -- re-exported for backward-compat imports
    MediaDownloadSummary,
    _persist_download_outcome,
    _safe_delete_after_commit_failure,
    build_within_window_count,
    download_media_candidates,
)
from app.services.service_access import WORKER_MEDIA, tenant_service_denial
from app.services.tenant_credentials import (
    TenantCredentialError,
    config_for_tenant,
    resolve_tenant_archive_credentials,
    tenant_log_tag,
)
from app.settings import get_event_media_download_settings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_DEFAULT_LIMIT = 10
_DEFAULT_TIMEOUT = 30
# The single lock for the single pipeline (RND-147's original path, kept
# unchanged so an existing deployment's lock directory/permissions need no
# migration).
_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock"
_SUPPORTED_STORAGE_PROVIDERS = frozenset({"local", "qiniu_kodo"})


class MediaWorkerExit(SystemExit):
    """A safe, classified expected CLI exit.

    Expected failures must reach ``main`` without their original exception or
    configuration value, so its final lifecycle line can be emitted for every
    exit path without leaking details to stdout or the systemd journal.
    """

    def __init__(self, code: int, error_class: str) -> None:
        super().__init__(code)
        self.error_class = error_class


class _SafeArgumentParser(argparse.ArgumentParser):
    """Avoid echoing arbitrary CLI text (which can be a pasted URL/token)."""

    def error(self, _message: str) -> None:
        _fail("invalid_arguments", "Invalid command arguments")


def _fail(error_class: str, message: str) -> None:
    """Emit a fixed safe failure and preserve its machine-readable class."""
    print(f"[FAIL] media_worker error_class={error_class} {message}", flush=True)
    raise MediaWorkerExit(1, error_class)


def _gate_tenant_service(session: Session, tenant_id: str) -> None:
    """RND-402: deny the media capability for a frozen/suspended tenant.

    Runs at tenant resolution, before any candidate selection or SDK
    work. On denial it writes one sanitized Audit row (only the stable
    error code, capability class, and lifecycle status — never message
    content or credentials), prints an identifier-free info line, and
    exits 0 (a safe no-op, matching the no-work path).
    """
    status = None
    row = session.query(Tenant).filter(Tenant.id == tenant_id).first()
    if row is not None:
        status = row.lifecycle_status
    denial = tenant_service_denial(status, WORKER_MEDIA)
    if denial is None:
        return
    try:
        session.add(
            AuditLog(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                admin_user_id=None,
                action=AuditAction.SERVICE_ACCESS_DENIED,
                object_type=AuditObjectType.TENANT,
                object_id=tenant_id,
                detail={
                    "capability": WORKER_MEDIA,
                    "error_code": denial,
                    "lifecycle_status": status,
                },
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 -- an audit failure must never expose DB detail
        session.rollback()
    print(
        f"[INFO] media_worker tenant={tenant_log_tag(tenant_id)} trigger=skipped "
        f"error_code={denial}",
        flush=True,
    )
    print("[PASS] tenant service gate denies media downloads — exiting without work", flush=True)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        _fail("configuration_missing", f"Environment variable not set or empty: {name}")
    return value


def _optional_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _fail("configuration_invalid", f"Environment variable {name} is not a valid integer")


# ---------------------------------------------------------------------------
# Concurrent-run guard — non-blocking fcntl.flock
# ---------------------------------------------------------------------------


def _acquire_lock(lock_path: str) -> Optional[int]:  # noqa: UP045 -- Python 3.9 runtime compatibility
    lock_dir = os.path.dirname(lock_path)
    try:
        if lock_dir and not os.path.isdir(lock_dir):
            os.makedirs(lock_dir, mode=0o750, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
    except OSError:
        _fail("lock_unavailable", "Cannot create or open media worker lock")

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return None
    except OSError:
        os.close(lock_fd)
        _fail("lock_unavailable", "Cannot acquire media worker lock")
    return lock_fd


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


def _require_tenant_id(session: Session, corp_id: str) -> str:
    try:
        row = (
            session.query(TenantWecomConfig)
            .filter(
                TenantWecomConfig.corp_id == corp_id,
                TenantWecomConfig.is_active.is_(True),
            )
            .first()
        )
    except Exception:  # noqa: BLE001 -- DB detail can contain a connection string
        _fail("database_unavailable", "Unable to resolve active tenant")

    if row is None:
        _fail(
            "tenant_unavailable",
            "No active tenant found. Run bootstrap_default_tenant.py first.",
        )
    return row.tenant_id


# ---------------------------------------------------------------------------
# --types parsing
# ---------------------------------------------------------------------------


def _parse_types(raw: Optional[str]) -> frozenset[str]:  # noqa: UP045 -- Python 3.9 runtime compatibility
    """Parse --types into a validated msgtype set, defaulting to every
    type this script supports. Rejects any type outside
    GENERIC_DOWNLOAD_MSGTYPES loudly (never silently drops an unknown
    type from the requested set)."""
    if not raw or not raw.strip():
        return GENERIC_DOWNLOAD_MSGTYPES
    requested = frozenset(t.strip() for t in raw.split(",") if t.strip())
    unknown = requested - GENERIC_DOWNLOAD_MSGTYPES
    if unknown:
        # Command arguments are not trusted log data; do not echo an invalid
        # value that could have been pasted from a credential or URL.
        _fail("invalid_arguments", "--types contains an unsupported message type")
    return requested


# ---------------------------------------------------------------------------
# Storage provider
# ---------------------------------------------------------------------------


def _media_storage_provider(backend_name: str) -> MediaStorageProvider:
    # Check the deployment selector before it reaches a factory exception.
    # A malformed value could itself be a filesystem path, signed URL, or
    # pasted credential, so it must never be rendered by a worker boundary.
    if backend_name not in _SUPPORTED_STORAGE_PROVIDERS:
        _fail("storage_configuration", "Media storage provider is unsupported")

    try:
        provider = get_media_storage_provider(backend_name)
    except MediaStorageConfigurationError:
        # Do not print provider exception text: a malformed configuration can
        # itself be a filesystem path, signed URL, or pasted credential.
        _fail("storage_configuration", "Media storage configuration is invalid")

    if isinstance(provider, LocalStorageProvider) and provider.root is None:
        _fail("storage_configuration", "STORAGE_LOCAL_PATH is not set")
    return provider


def _since_ms_cutoff(since_hours: float) -> int:
    return int(time.time() * 1000) - int(since_hours * 3600 * 1000)


# ---------------------------------------------------------------------------
# Trigger attribution and durable retry policy
# ---------------------------------------------------------------------------

_TRIGGER_SOURCES = frozenset({"archive-complete", "callback", "manual", "timer"})


def _safe_trigger_source(raw: str) -> str:
    return raw if raw in _TRIGGER_SOURCES else "manual"


def _positive(raw: str, default: int) -> int:
    try:
        return max(1, int(raw.strip()))
    except (AttributeError, ValueError):
        return default


def _retry_settings() -> tuple[int, int]:
    """Read the durable retry budget shared by event and timer runs."""
    settings = get_event_media_download_settings()
    return (
        _positive(settings.event_media_download_retry_count, 3),
        _positive(settings.event_media_download_backoff_seconds, 30),
    )


def _retry_is_eligible(media_file: MediaFile, retry_count: int, backoff_seconds: int) -> bool:
    """Return whether a failed row is within its retry budget and backoff."""
    attempts = int(media_file.download_attempts or 0)
    if attempts >= retry_count:
        return False
    attempted_at = media_file.updated_at
    if attempted_at is None:
        return True
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    delay = backoff_seconds * min(2 ** max(attempts - 1, 0), 8)
    return (datetime.now(timezone.utc) - attempted_at).total_seconds() >= delay


def _filter_retryable_candidates(
    session: Session,
    tenant_id: str,
    candidates: list[ArchiveMessage],
    nested_item_candidates: list,
    retry_count: int,
    backoff_seconds: int,
) -> tuple[list[ArchiveMessage], list, int]:
    """Remove exhausted/backing-off failed rows without hiding fresh media.

    Candidate selection remains in the existing unified pipeline.  This is a
    post-selection policy filter only, shared by every top-level and nested
    supported media type before the normal media worker touches the SDK.
    """
    sdkfileids = {message.sdkfileid for message in candidates if message.sdkfileid}
    sdkfileids.update(ref["sdkfileid"] for _message, ref in nested_item_candidates)
    if not sdkfileids:
        return candidates, nested_item_candidates, 0

    rows = (
        session.query(MediaFile)
        .filter(MediaFile.tenant_id == tenant_id, MediaFile.sdkfileid.in_(sdkfileids))
        .all()
    )
    by_sdkfileid = {row.sdkfileid: row for row in rows}
    skipped = 0

    def _eligible(sdkfileid: str) -> bool:
        nonlocal skipped
        row = by_sdkfileid.get(sdkfileid)
        if row is None or row.download_status != "failed":
            return True
        if _retry_is_eligible(row, retry_count, backoff_seconds):
            return True
        skipped += 1
        return False

    return (
        [message for message in candidates if _eligible(message.sdkfileid)],
        [(message, ref) for message, ref in nested_item_candidates if _eligible(ref["sdkfileid"])],
        skipped,
    )


def _remaining_retryable_count(
    session: Session, tenant_id: str, sdkfileids: set[str], retry_count: int
) -> int:
    """Aggregate-only count for the completed-run log; never exposes IDs."""
    if not sdkfileids:
        return 0
    return (
        session.query(MediaFile)
        .filter(
            MediaFile.tenant_id == tenant_id,
            MediaFile.sdkfileid.in_(sdkfileids),
            MediaFile.download_status == "failed",
            MediaFile.download_attempts < retry_count,
        )
        .count()
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    started = time.monotonic()
    started_cpu = time.process_time()
    source = "manual"
    result = "failed"
    error_class = "unexpected_failure"
    exit_code = 1
    parser = _SafeArgumentParser(
        description="One-shot WeCom generic media download — image/voice/video/file/emotion"
    )
    parser.add_argument("--count-only", action="store_true", help="Report candidate counts only; perform no writes")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help=f"Max messages to process this run (default {_DEFAULT_LIMIT})")
    parser.add_argument("--retry", action="store_true", help="Also retry eligible failed media within the durable budget")
    parser.add_argument(
        "--trigger-source",
        type=str,
        default="manual",
        help="Safe scheduler attribution: archive-complete, callback, manual, or timer",
    )
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

    try:
        args = parser.parse_args()
        args.trigger_source = _safe_trigger_source(args.trigger_source)
        source = args.trigger_source
        if args.limit <= 0:
            _fail("invalid_arguments", "--limit must be a positive integer")

        if args.since_hours is not None and args.since_hours <= 0:
            _fail("invalid_arguments", "--since-hours must be a positive number")

        msgtypes = _parse_types(args.types)

        lock_path = os.environ.get("MEDIA_DOWNLOAD_LOCK_PATH", "").strip() or _DEFAULT_LOCK_PATH
        lock_fd = _acquire_lock(lock_path)
        if lock_fd is None:
            print("[INFO] candidate_total: 0", flush=True)
            print("[INFO] candidate_selected: 0", flush=True)
            print(
                f"[INFO] media_worker trigger_source={source} "
                "trigger=skipped-locked attempted=0 succeeded=0 failed=0 retryable=0 skipped=1",
                flush=True,
            )
            print(
                "[PASS] another instance already holds the run lock — exiting without "
                "selecting candidates",
                flush=True,
            )
            result = "skipped"
            error_class = "none"
            exit_code = 0
        else:
            try:
                _run(args, msgtypes)
                result = "completed"
                error_class = "none"
                exit_code = 0
            finally:
                _release_lock(lock_fd)
    except MediaWorkerExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        result = "failed"
        error_class = exc.error_class
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        result = "completed" if exit_code == 0 else "failed"
        error_class = "none" if exit_code == 0 else "invalid_arguments" if exit_code == 2 else "unclassified_exit"
    except Exception:  # noqa: BLE001 -- safe CLI boundary intentionally hides provider detail
        # Do not let an unexpected SDK/storage/DB exception print a traceback
        # that could contain a path or provider detail. The durable pending or
        # failed row remains the task source of truth for timer reconciliation.
        print(
            f"[FAIL] media_worker trigger_source={source} "
            "error_class=unexpected_failure",
            flush=True,
        )
        exit_code = 1
        result = "failed"
        error_class = "unexpected_failure"
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        cpu_ms = int((time.process_time() - started_cpu) * 1000)
        peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        completed_at = datetime.now(timezone.utc).isoformat()
        print(
            f"[INFO] media_worker trigger_source={source} lifecycle=ended "
            f"result={result} exit_code={exit_code} error_class={error_class} "
            f"completed_at={completed_at} duration_ms={duration_ms} cpu_ms={cpu_ms} "
            f"peak_rss_kb={peak_rss_kb}",
            flush=True,
        )

    raise SystemExit(exit_code)


def _run(args: argparse.Namespace, msgtypes: frozenset[str]) -> None:
    database_url = _require_env("DATABASE_URL")

    engine = create_engine(database_url)

    tenant_id_env = os.environ.get("WECOM_TENANT_ID", "").strip()
    with Session(engine) as session:
        if tenant_id_env:
            config = config_for_tenant(session, tenant_id_env)
            if config is None:
                _fail(
                    "tenant_config_unavailable",
                    "Tenant archive configuration is unavailable",
                )
            corp_id = config.corp_id
            tenant_id = config.tenant_id
        else:
            corp_id = _require_env("WECOM_CORP_ID")
            tenant_id = _require_tenant_id(session, corp_id)

        # RND-402: authoritative worker gate at tenant resolution. A
        # frozen/suspended tenant never downloads, even if a signal or
        # timer already fired for it.
        _gate_tenant_service(session, tenant_id)

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
        candidates: list[ArchiveMessage] = list(actionable) + [m for m, _mf in stale_repairs]

        print(f"[INFO] types: {sorted(msgtypes)}", flush=True)
        print(f"[INFO] candidate_total: {total_eligible}", flush=True)
        print(f"[INFO] candidate_selected: {len(candidates)}", flush=True)
        if stale_repairs:
            print(f"[INFO] stale_downloaded_repair_selected: {len(stale_repairs)}", flush=True)

        nested_item_candidates: list = []
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

        retry_count, backoff_seconds = _retry_settings()
        retry_policy_skipped = 0
        if args.retry:
            candidates, nested_item_candidates, retry_policy_skipped = _filter_retryable_candidates(
                session,
                tenant_id,
                candidates,
                nested_item_candidates,
                retry_count,
                backoff_seconds,
            )
            if retry_policy_skipped:
                print(f"[INFO] retry_policy_skipped: {retry_policy_skipped}", flush=True)

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
            print(
                f"[INFO] media_worker trigger_source={args.trigger_source} "
                f"trigger=no-work attempted=0 succeeded=0 failed=0 retryable=0 "
                f"skipped={retry_policy_skipped}",
                flush=True,
            )
            print("[PASS] no candidates to process", flush=True)
            sys.exit(0)

        lib_path = _require_env("WECOM_SDK_LIB_PATH")
        if tenant_id_env:
            try:
                secret = resolve_tenant_archive_credentials(
                    session, tenant_id_env
                ).archive_secret
            except TenantCredentialError as exc:
                _fail(
                    exc.error_class,
                    "Tenant archive credentials are unavailable",
                )
        else:
            secret = _require_env("WECOM_ARCHIVE_SECRET")
        timeout = _optional_int_env("WECOM_MEDIA_TIMEOUT", _DEFAULT_TIMEOUT)
        write_backend_name = get_configured_write_backend_name()
        storage_provider = _media_storage_provider(write_backend_name)

        try:
            lib = wecom_sdk.load_sdk(lib_path)
        except FileNotFoundError:
            _fail("sdk_load_failed", "SDK library was not found")
        except OSError:
            _fail("sdk_load_failed", "SDK library could not be loaded")

        try:
            wecom_sdk.configure_sdk(lib)
            wecom_sdk.configure_sdk_media_data(lib)
        except AttributeError:
            _fail("sdk_configuration_failed", "SDK media-download symbols are unavailable")

        handle = wecom_sdk.new_sdk(lib)
        if not handle:
            _fail("sdk_initialization_failed", "SDK returned a null handle")

        init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
        if init_ret != 0:
            print(
                f"[FAIL] media_worker error_class=sdk_initialization_failed sdk_return_code={init_ret}",
                flush=True,
            )
            try:
                wecom_sdk.destroy_sdk(lib, handle)
            except Exception:  # noqa: BLE001, S110 -- SDK cleanup must not replace a safe init failure
                pass
            raise MediaWorkerExit(1, "sdk_initialization_failed")

        selected_sdkfileids = {message.sdkfileid for message in candidates if message.sdkfileid}
        selected_sdkfileids.update(
            ref["sdkfileid"] for _message, ref in nested_item_candidates
        )
        summary = download_media_candidates(
            session,
            tenant_id,
            lib,
            handle,
            storage_provider,
            write_backend_name,
            timeout,
            candidates,
            nested_item_candidates,
            record_attempt=True,
            enforce_quota=True,
        )

        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:  # noqa: BLE001, S110 -- SDK cleanup must not change media persistence truth
            pass

        succeeded = summary.downloaded + summary.nested_downloaded
        failed = summary.failed + summary.nested_failed
        quota_blocked = summary.quota_blocked + summary.nested_quota_blocked
        retryable = _remaining_retryable_count(
            session, tenant_id, selected_sdkfileids, retry_count
        )
        skipped = summary.skipped + retry_policy_skipped
        print(
            f"[INFO] media_worker trigger_source={args.trigger_source} "
            f"trigger=completed attempted={summary.attempted} succeeded={succeeded} "
            f"failed={failed} quota_blocked={quota_blocked} "
            f"retryable={retryable} skipped={skipped}",
            flush=True,
        )
        print(f"[INFO] downloaded: {summary.downloaded}", flush=True)
        print(f"[INFO] failed: {summary.failed}", flush=True)
        print(f"[INFO] quota_blocked: {summary.quota_blocked}", flush=True)
        if summary.reason_counts:
            diag = ", ".join(f"{k}={v}" for k, v in sorted(summary.reason_counts.items()))
            print(f"[INFO] failed_reasons: {diag}", flush=True)
        if not args.skip_nested:
            print(f"[INFO] nested_downloaded: {summary.nested_downloaded}", flush=True)
            print(f"[INFO] nested_failed: {summary.nested_failed}", flush=True)
            print(
                f"[INFO] nested_quota_blocked: {summary.nested_quota_blocked}",
                flush=True,
            )
            if summary.nested_reason_counts:
                nested_diag = ", ".join(
                    f"{k}={v}" for k, v in sorted(summary.nested_reason_counts.items())
                )
                print(f"[INFO] nested_failed_reasons: {nested_diag}", flush=True)
        print("[PASS] download_wecom_media_once completed", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
