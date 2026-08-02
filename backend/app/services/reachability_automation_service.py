"""Best-effort, tenant-scoped reachability automation (RND-339).

Classification is deliberately delegated to ``app.reachability_audit``. This
module only freezes candidate sets and manages finding lifecycle records; it
does not duplicate the classifier or its status/reason decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Optional
from uuid import uuid4

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ReachabilityAuditRun, ReachabilityFinding
from app.reachability_audit import REACHABLE_STATUSES, build_message_reachability_report
from app.services.reachability_check_service import (
    _complete,
    create_or_reuse_run,
    mark_run_error,
    utc_now,
)

AutomationMode = Literal["incremental", "reconcile"]
AUTOMATION_MODES = frozenset({"incremental", "reconcile"})
RECONCILE_SCOPE_DAYS = 7


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ms(value: datetime) -> int:
    return int(_as_utc(value).timestamp() * 1000)


def _last_complete_incremental_watermark(db: Session, tenant_id: str) -> int:
    runs = (
        db.query(ReachabilityAuditRun)
        .filter(
            ReachabilityAuditRun.tenant_id == tenant_id,
            ReachabilityAuditRun.source == "incremental",
            ReachabilityAuditRun.status == "completed",
        )
        .order_by(ReachabilityAuditRun.completed_at.desc(), ReachabilityAuditRun.id.desc())
        .all()
    )
    for run in runs:
        if _complete(run):
            return run.scope_max_message_id
    return 0


def _success_max_id(db: Session, tenant_id: str) -> int:
    return (
        db.query(func.max(ArchiveMessage.id))
        .filter(ArchiveMessage.tenant_id == tenant_id, ArchiveMessage.decrypt_status == "success")
        .scalar()
        or 0
    )


def _incremental_candidate_ids(db: Session, tenant_id: str, lower: int, upper: int) -> list[int]:
    return [
        row[0]
        for row in (
            db.query(ArchiveMessage.id)
            .filter(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessage.decrypt_status == "success",
                ArchiveMessage.id > lower,
                ArchiveMessage.id <= upper,
            )
            .order_by(ArchiveMessage.id)
            .all()
        )
    ]


def _reconcile_candidate_ids(
    db: Session, tenant_id: str, scope_from: datetime, scope_to: datetime, upper: int
) -> list[int]:
    active_ids = [
        row[0]
        for row in (
            db.query(ReachabilityFinding.archive_message_id)
            .filter(ReachabilityFinding.tenant_id == tenant_id, ReachabilityFinding.status == "active")
            .all()
        )
    ]
    return [
        row[0]
        for row in (
            db.query(ArchiveMessage.id)
            .filter(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessage.decrypt_status == "success",
                ArchiveMessage.id <= upper,
                or_(
                    ArchiveMessage.msgtime.between(_ms(scope_from), _ms(scope_to)),
                    ArchiveMessage.id.in_(active_ids) if active_ids else False,
                ),
            )
            .order_by(ArchiveMessage.id)
            .all()
        )
    ]


def create_automation_run(
    db: Session, tenant_id: str, mode: AutomationMode, *, now: Optional[datetime] = None
) -> tuple[Optional[ReachabilityAuditRun], bool, list[int]]:
    """Freeze a mode-specific candidate list and atomically reserve the tenant run.

    A zero-new-message incremental invocation intentionally creates no run and
    therefore cannot overwrite a human health snapshot.
    """
    if mode not in AUTOMATION_MODES:
        raise ValueError("invalid automation mode")
    now = _as_utc(now or utc_now())
    upper = _success_max_id(db, tenant_id)
    scope_from = now - timedelta(days=RECONCILE_SCOPE_DAYS)
    if mode == "incremental":
        lower = _last_complete_incremental_watermark(db, tenant_id)
        candidate_ids = _incremental_candidate_ids(db, tenant_id, lower, upper)
        if not candidate_ids:
            return None, False, []
    else:
        candidate_ids = _reconcile_candidate_ids(db, tenant_id, scope_from, now, upper)

    run, created = create_or_reuse_run(
        db,
        tenant_id,
        now=now,
        source=mode,
        scope_from=scope_from,
        scope_to=now,
        scope_max_message_id=upper,
        matching_count=len(candidate_ids),
    )
    # A competing/manual active run is returned by the shared DB guard; its
    # candidate list is not ours and must never be scanned as automation.
    return run, created, candidate_ids if created else []


def _sample_for_message(
    db: Session, tenant_id: str, message_id: int, frozen_max_id: int
) -> dict:
    """Ask the existing classifier for exactly one frozen candidate.

    The old diagnostic API caps samples at 200. Paging it one message at a
    time preserves that public contract while allowing durable lifecycle
    processing without copying any classifier branch.
    """
    offset = (
        db.query(func.count(ArchiveMessage.id))
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.id < message_id,
            ArchiveMessage.id <= frozen_max_id,
        )
        .scalar()
    )
    report = build_message_reachability_report(
        db,
        tenant_id,
        scope_max_message_id=frozen_max_id,
        limit=1,
        offset=offset,
        include_samples=True,
        sample_limit=1,
        include_reason_counts=True,
    )
    samples = report.get("samples", [])
    if len(samples) != 1 or samples[0].get("message_db_id") != message_id:
        raise RuntimeError("frozen candidate could not be classified")
    return samples[0]


def _upsert_finding(
    db: Session,
    *,
    tenant_id: str,
    message_id: int,
    reason_code: str,
    run: ReachabilityAuditRun,
    seen_at: datetime,
) -> None:
    finding = (
        db.query(ReachabilityFinding)
        .filter(
            ReachabilityFinding.tenant_id == tenant_id,
            ReachabilityFinding.archive_message_id == message_id,
            ReachabilityFinding.reason_code == reason_code,
            ReachabilityFinding.algorithm_version == run.algorithm_version,
        )
        .with_for_update()
        .first()
    )
    if finding is None:
        db.add(
            ReachabilityFinding(
                public_id=str(uuid4()), tenant_id=tenant_id, archive_message_id=message_id,
                reason_code=reason_code, status="active", first_seen=seen_at, last_seen=seen_at,
                occurrence_count=1, first_run_id=run.id, last_run_id=run.id,
                algorithm_version=run.algorithm_version,
            )
        )
        return
    finding.status = "active"
    finding.resolved_at = None
    finding.last_seen = seen_at
    finding.last_run_id = run.id
    finding.occurrence_count += 1


def _resolve_recovered_findings(
    db: Session,
    *,
    tenant_id: str,
    classified_ids: set[int],
    active_issue_keys: set[tuple[int, str]],
    resolved_at: datetime,
) -> None:
    """Resolve only observations rechecked by a complete full reconciliation."""
    rows = (
        db.query(ReachabilityFinding)
        .filter(
            ReachabilityFinding.tenant_id == tenant_id,
            ReachabilityFinding.status == "active",
            ReachabilityFinding.archive_message_id.in_(classified_ids) if classified_ids else False,
        )
        .all()
    )
    for finding in rows:
        if (finding.archive_message_id, finding.reason_code) not in active_issue_keys:
            finding.status = "resolved"
            finding.resolved_at = resolved_at


def execute_automation_run(
    db: Session, tenant_id: str, public_id: str, candidate_ids: Iterable[int]
) -> str:
    """Classify a frozen set and commit run completion/watermark with findings."""
    run = (
        db.query(ReachabilityAuditRun)
        .filter(
            ReachabilityAuditRun.tenant_id == tenant_id,
            ReachabilityAuditRun.public_id == public_id,
        )
        .with_for_update()
        .first()
    )
    if run is None or run.status != "checking" or run.source not in AUTOMATION_MODES:
        return "not_active"
    ids = list(candidate_ids)
    if run.matching_count != len(ids):
        mark_run_error(db, tenant_id, public_id, "scope_mismatch")
        return "error"
    try:
        reachable = 0
        reason_counts: dict[str, int] = {}
        active_issue_keys: set[tuple[int, str]] = set()
        classified_ids: set[int] = set()
        seen_at = utc_now()
        for message_id in ids:
            sample = _sample_for_message(db, tenant_id, message_id, run.scope_max_message_id)
            classified_ids.add(message_id)
            reason_code = sample["reason_code"]
            reason_counts[reason_code] = reason_counts.get(reason_code, 0) + 1
            if sample["reachability_status"] in {status.value for status in REACHABLE_STATUSES}:
                reachable += 1
            else:
                active_issue_keys.add((message_id, reason_code))
                _upsert_finding(
                    db, tenant_id=tenant_id, message_id=message_id, reason_code=reason_code,
                    run=run, seen_at=seen_at,
                )
        if len(classified_ids) != run.matching_count or sum(reason_counts.values()) != run.matching_count:
            raise RuntimeError("incomplete frozen classification")
        # Resolution is deliberately after full coverage and in the same
        # transaction as completion. Incremental runs never enter this branch.
        if run.source == "reconcile":
            _resolve_recovered_findings(
                db, tenant_id=tenant_id, classified_ids=classified_ids,
                active_issue_keys=active_issue_keys, resolved_at=seen_at,
            )
        run.checked_count = len(classified_ids)
        run.reachable_count = reachable
        run.unreachable_count = len(classified_ids) - reachable
        run.reason_counts = reason_counts
        run.status = "completed"
        run.safe_error_code = None
        run.completed_at = seen_at
        db.commit()
        return "completed"
    except Exception:
        db.rollback()
        mark_run_error(db, tenant_id, public_id, "scan_failed")
        return "error"


def run_automation(db: Session, tenant_id: str, mode: AutomationMode) -> str:
    """Start and synchronously execute one best-effort local automation run."""
    run, created, candidate_ids = create_automation_run(db, tenant_id, mode)
    if run is None:
        return "no_data"
    if not created:
        return "locked"
    return execute_automation_run(db, tenant_id, run.public_id, candidate_ids)
