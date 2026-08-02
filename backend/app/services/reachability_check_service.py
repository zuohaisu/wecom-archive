"""Persistent, tenant-scoped reachability-check orchestration (RND-337)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ReachabilityAuditRun
from app.reachability_audit import MAX_SCAN_LIMIT, build_message_reachability_report

ALGORITHM_VERSION = "reachability-v1"
DEFAULT_SCOPE_DAYS = 7
STALE_AFTER = timedelta(minutes=15)
ACTIVE_STATUS = "checking"
RUN_SOURCES = frozenset({"manual", "incremental", "reconcile"})
FULL_SNAPSHOT_SOURCES = frozenset({"manual", "reconcile"})
SAFE_ERROR_CODES = frozenset({
    "process_start_failed", "scan_failed", "scope_mismatch", "page_incomplete", "stale_checking",
})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _epoch_ms(value: datetime) -> int:
    return int(_as_utc(value).timestamp() * 1000)


def _candidate_query(db: Session, tenant_id: str, scope_from: datetime, scope_to: datetime):
    return db.query(ArchiveMessage).filter(
        ArchiveMessage.tenant_id == tenant_id,
        ArchiveMessage.decrypt_status == "success",
        ArchiveMessage.msgtime >= _epoch_ms(scope_from),
        ArchiveMessage.msgtime <= _epoch_ms(scope_to),
    )


def _mark_stale_runs(db: Session, tenant_id: str, now: datetime) -> int:
    return (
        db.query(ReachabilityAuditRun)
        .filter(
            ReachabilityAuditRun.tenant_id == tenant_id,
            ReachabilityAuditRun.status == ACTIVE_STATUS,
            ReachabilityAuditRun.started_at <= now - STALE_AFTER,
        )
        .update(
            {
                ReachabilityAuditRun.status: "incomplete",
                ReachabilityAuditRun.safe_error_code: "stale_checking",
                ReachabilityAuditRun.completed_at: now,
            },
            synchronize_session=False,
        )
    )


def create_or_reuse_run(
    db: Session,
    tenant_id: str,
    *,
    now: Optional[datetime] = None,
    source: str = "manual",
    scope_from: Optional[datetime] = None,
    scope_to: Optional[datetime] = None,
    scope_max_message_id: Optional[int] = None,
    matching_count: Optional[int] = None,
) -> tuple[ReachabilityAuditRun, bool]:
    """Atomically create one tenant-active frozen run, or reuse it.

    RND-339 supplies its already frozen candidate boundary/count for
    incremental and reconcile work. The RND-337 manual behaviour remains the
    default and therefore remains backwards compatible.
    """
    if source not in RUN_SOURCES:
        raise ValueError("invalid reachability run source")
    now = _as_utc(now or utc_now())
    _mark_stale_runs(db, tenant_id, now)
    active = (
        db.query(ReachabilityAuditRun)
        .filter(ReachabilityAuditRun.tenant_id == tenant_id, ReachabilityAuditRun.status == ACTIVE_STATUS)
        .order_by(ReachabilityAuditRun.created_at.desc()).first()
    )
    if active is not None:
        if db.dirty:
            db.commit()
        return active, False

    scope_to = _as_utc(scope_to or now)
    scope_from = _as_utc(scope_from or (scope_to - timedelta(days=DEFAULT_SCOPE_DAYS)))
    if scope_max_message_id is None or matching_count is None:
        candidates = _candidate_query(db, tenant_id, scope_from, scope_to)
        # A later insert has a greater DB id and is deliberately excluded.
        scope_max_message_id = candidates.with_entities(func.max(ArchiveMessage.id)).scalar() or 0
        matching_count = candidates.filter(ArchiveMessage.id <= scope_max_message_id).count() if scope_max_message_id else 0

    run = ReachabilityAuditRun(
        public_id=str(uuid4()), tenant_id=tenant_id, status=ACTIVE_STATUS,
        source=source, algorithm_version=ALGORITHM_VERSION,
        scope_from=scope_from, scope_to=scope_to,
        scope_max_message_id=scope_max_message_id, matching_count=matching_count,
        checked_count=0, reachable_count=0, unreachable_count=0, reason_counts={}, started_at=now,
    )
    db.add(run)
    try:
        db.commit()
        db.refresh(run)
        return run, True
    except IntegrityError:
        # The partial unique index is the cross-process concurrency guard.
        db.rollback()
        active = (
            db.query(ReachabilityAuditRun)
            .filter(ReachabilityAuditRun.tenant_id == tenant_id, ReachabilityAuditRun.status == ACTIVE_STATUS)
            .order_by(ReachabilityAuditRun.created_at.desc()).first()
        )
        if active is None:
            raise
        return active, False


def _complete(run: ReachabilityAuditRun) -> bool:
    reasons = run.reason_counts if isinstance(run.reason_counts, dict) else None
    return bool(
        run.status == "completed"
        and reasons is not None
        and run.checked_count == run.matching_count
        and run.reachable_count + run.unreachable_count == run.checked_count
        and all(
            isinstance(key, str) and isinstance(value, int) and value >= 0
            for key, value in reasons.items()
        )
        and sum(reasons.values()) == run.checked_count
    )


def state_for_run(run: ReachabilityAuditRun) -> tuple[str, bool]:
    """Return only one of the six externally permitted states."""
    if run.status == ACTIVE_STATUS:
        return "checking", False
    complete = _complete(run)
    if not complete:
        return "error" if run.status == "error" else "incomplete", False
    if run.matching_count == 0:
        return "no_data", True
    if run.unreachable_count == 0:
        return "healthy", True
    return "attention", True


def snapshot_for_run(run: ReachabilityAuditRun) -> dict[str, Any]:
    state, complete = state_for_run(run)
    return {
        "public_run_id": run.public_id,
        "state": state,
        "complete": complete,
        "scope": {"from_at": run.scope_from, "to_at": run.scope_to},
        "counts": {
            "matching": run.matching_count,
            "checked": run.checked_count,
            "reachable": run.reachable_count,
            "unreachable": run.unreachable_count,
        },
        "reason_counts": run.reason_counts if isinstance(run.reason_counts, dict) else {},
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "last_checked_at": run.completed_at if complete else None,
        "algorithm_version": run.algorithm_version,
        "safe_error_code": run.safe_error_code,
    }


def latest_snapshot(
    db: Session, tenant_id: str, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    now = _as_utc(now or utc_now())
    if _mark_stale_runs(db, tenant_id, now):
        db.commit()
    # Incremental checks are intentionally local evidence: a clean slice
    # cannot promote whole-archive health. Human/manual and full reconcile
    # snapshots are the only sources that may provide the normal latest view.
    run = (
        db.query(ReachabilityAuditRun)
        .filter(
            ReachabilityAuditRun.tenant_id == tenant_id,
            ReachabilityAuditRun.source.in_(FULL_SNAPSHOT_SOURCES),
        )
        .order_by(ReachabilityAuditRun.created_at.desc()).first()
    )
    if run is not None:
        snapshot = snapshot_for_run(run)
        # A local negative observation immediately invalidates a previously
        # healthy full snapshot; only another complete full run can restore it.
        if snapshot["state"] == "healthy":
            from app.db.models import ReachabilityFinding
            has_active_finding = (
                db.query(ReachabilityFinding.id)
                .filter(
                    ReachabilityFinding.tenant_id == tenant_id,
                    ReachabilityFinding.status == "active",
                ).first() is not None
            )
            if has_active_finding:
                snapshot["state"] = "attention"
                snapshot["complete"] = False
                snapshot["last_checked_at"] = None
        return snapshot

    scope_to = now
    scope_from = now - timedelta(days=DEFAULT_SCOPE_DAYS)
    has_candidates = _candidate_query(db, tenant_id, scope_from, scope_to).first() is not None
    return {
        "public_run_id": None,
        "state": "incomplete" if has_candidates else "no_data",
        "complete": False,
        "scope": {"from_at": scope_from, "to_at": scope_to},
        "counts": {"matching": 0, "checked": 0, "reachable": 0, "unreachable": 0},
        "reason_counts": {},
        "created_at": None,
        "started_at": None,
        "completed_at": None,
        "last_checked_at": None,
        "algorithm_version": ALGORITHM_VERSION,
        "safe_error_code": None,
    }


def mark_run_error(db: Session, tenant_id: str, public_id: str, code: str) -> None:
    """Persist an allowlisted failure code without exposing raw exception text."""
    code = code if code in SAFE_ERROR_CODES else "scan_failed"
    run = (
        db.query(ReachabilityAuditRun)
        .filter(
            ReachabilityAuditRun.tenant_id == tenant_id,
            ReachabilityAuditRun.public_id == public_id,
            ReachabilityAuditRun.status == ACTIVE_STATUS,
        )
        .first()
    )
    if run is not None:
        run.status = "error"
        run.safe_error_code = code
        run.completed_at = utc_now()
        db.commit()


def _mark_incomplete(db: Session, run: ReachabilityAuditRun, code: str) -> str:
    run.status = "incomplete"
    run.safe_error_code = code
    run.completed_at = utc_now()
    db.commit()
    return "incomplete"


def execute_run(db: Session, public_id: str) -> str:
    """Run one frozen check. Re-entry never adds to already persisted counts."""
    # This lookup only resolves the tenant. All subsequent reads/writes carry
    # both public_id and tenant_id explicitly.
    resolved = db.query(ReachabilityAuditRun).filter(ReachabilityAuditRun.public_id == public_id).first()
    if resolved is None:
        return "not_found"
    tenant_id = resolved.tenant_id
    run = (
        db.query(ReachabilityAuditRun)
        .filter(
            ReachabilityAuditRun.tenant_id == tenant_id,
            ReachabilityAuditRun.public_id == public_id,
        )
        .with_for_update()
        .first()
    )
    if run is None:
        return "not_found"
    if run.status != ACTIVE_STATUS:
        return "not_active"

    try:
        offset = 0
        checked = reachable = unreachable = 0
        reason_counts: dict[str, int] = {}
        while True:
            report = build_message_reachability_report(
                db,
                tenant_id,
                msgtime_from=_epoch_ms(run.scope_from),
                msgtime_to=_epoch_ms(run.scope_to),
                scope_max_message_id=run.scope_max_message_id,
                limit=MAX_SCAN_LIMIT,
                offset=offset,
                include_reason_counts=True,
            )
            if report["matching_total"] != run.matching_count:
                return _mark_incomplete(db, run, "scope_mismatch")
            page_checked = report["scanned_count"]
            checked += page_checked
            reachable += report["reachable_count"]
            unreachable += report["unreachable_count"]
            for reason, count in report["counts_by_reason"].items():
                reason_counts[reason] = reason_counts.get(reason, 0) + count
            offset += page_checked
            if not report["has_more"]:
                break
            if page_checked == 0:
                return _mark_incomplete(db, run, "page_incomplete")

        if checked != run.matching_count or sum(reason_counts.values()) != checked:
            return _mark_incomplete(db, run, "page_incomplete")
        run.checked_count = checked
        run.reachable_count = reachable
        run.unreachable_count = unreachable
        run.reason_counts = reason_counts
        run.status = "completed"
        run.safe_error_code = None
        run.completed_at = utc_now()
        db.commit()
        return "completed"
    except Exception:
        db.rollback()
        mark_run_error(db, tenant_id, public_id, "scan_failed")
        return "error"
