"""Small, durable external-contact refresh worker.

The callback/decrypt paths only create ``external_contact_refresh_tasks``.
This module is the sole owner of external-contact API I/O for those tasks and
commits progress one task at a time so a slow or unavailable customer record
never holds a long transaction or blocks another refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import ExternalContactRefreshTask, Tenant, TenantWecomConfig
from app.services.external_contact_sync import refresh_external_contact

_MAX_RETRY_DELAY = timedelta(hours=24)


@dataclass
class RefreshQueueSummary:
    selected: int = 0
    refreshed: int = 0
    unavailable: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalise SQLite's naive test values without changing PostgreSQL data."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _retry_at(attempt_count: int, now: datetime) -> datetime:
    """Return bounded backoff without retaining raw upstream error details."""
    delay = timedelta(minutes=5 * (2 ** max(0, attempt_count - 1)))
    return now + min(delay, _MAX_RETRY_DELAY)


def _active_tenant_id(session: Session, corp_id: str) -> str | None:
    row = (
        session.query(TenantWecomConfig.tenant_id)
        .join(Tenant, Tenant.id == TenantWecomConfig.tenant_id)
        .filter(
            TenantWecomConfig.corp_id == corp_id,
            TenantWecomConfig.is_active.is_(True),
            # RND-402: contact refresh is part of the archive sync
            # pipeline; a frozen/suspended tenant must not refresh.
            Tenant.lifecycle_status == "active",
        )
        .first()
    )
    return row[0] if row is not None else None


def run_external_contact_refresh_queue(
    session: Session,
    corp_id: str,
    external_secret: str,
    *,
    limit: int = 25,
    now: datetime | None = None,
) -> RefreshQueueSummary:
    """Refresh a bounded ready-task batch and commit each task independently."""
    summary = RefreshQueueSummary()
    tenant_id = _active_tenant_id(session, corp_id)
    if tenant_id is None or not external_secret.strip():
        return summary

    observed_at = now or _utc_now()
    task_ids = [
        task_id
        for (task_id,) in (
            session.query(ExternalContactRefreshTask.id)
            .filter(
                ExternalContactRefreshTask.tenant_id == tenant_id,
                ExternalContactRefreshTask.state == "pending",
                ExternalContactRefreshTask.next_attempt_at <= observed_at,
            )
            .order_by(ExternalContactRefreshTask.next_attempt_at, ExternalContactRefreshTask.id)
            .limit(max(1, limit))
            .all()
        )
    ]
    # Do not retain a read transaction while network requests execute.
    session.commit()

    for task_id in task_ids:
        task = session.get(ExternalContactRefreshTask, task_id)
        if (
            task is None
            or task.state != "pending"
            or _as_utc(task.next_attempt_at) > observed_at
        ):
            continue

        summary.selected += 1
        task.attempt_count += 1
        task.last_attempt_at = observed_at
        session.commit()

        try:
            outcome = refresh_external_contact(
                session,
                tenant_id,
                corp_id,
                external_secret,
                task.external_userid,
                update_interaction_stats=False,
            )
        except Exception:  # noqa: BLE001 -- only fixed classifications leave this worker
            outcome = None

        if outcome is not None and outcome.found:
            session.delete(task)
            summary.refreshed += 1
        else:
            task.state = "pending"
            task.last_error_class = "unavailable"
            task.next_attempt_at = _retry_at(task.attempt_count, observed_at)
            summary.unavailable += 1
        session.commit()

    return summary
