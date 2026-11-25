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

from app.db.models import ExternalContactRefreshTask
from app.services.external_contact_sync import refresh_external_contact

_MAX_RETRY_DELAY = timedelta(hours=24)
# 5 * 2**29 minutes already exceeds timedelta's range, so the exponent MUST be
# clamped before the timedelta is built: the previous expression raised
# OverflowError for any task whose attempt_count reached ~30, and that
# exception aborted the whole tenant batch on every run (GH-243). 2**16
# minutes is far beyond _MAX_RETRY_DELAY, so the cap is reached well before
# this bound.
_MAX_BACKOFF_EXPONENT = 16
# Hard retry budget: a task that has failed this many times is not going to
# converge by waiting longer (a deleted/never-resolvable external contact
# stays unresolvable). Dead-lettering it keeps one such row from pinning the
# tenant's backlog forever. At the 24h cap this is several months of retries.
_MAX_ATTEMPTS = 100
_TERMINAL_STATE = "dead"


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
    exponent = min(max(0, attempt_count - 1), _MAX_BACKOFF_EXPONENT)
    delay = min(timedelta(minutes=5 * (2 ** exponent)), _MAX_RETRY_DELAY)
    return now + delay


def run_external_contact_refresh_queue(
    session: Session,
    tenant_id: str,
    corp_id: str,
    external_secret: str,
    *,
    limit: int = 25,
    now: datetime | None = None,
) -> RefreshQueueSummary:
    """Refresh one tenant's bounded ready-task batch independently.

    Callers must resolve the active TenantWecomConfig before calling this
    worker. Accepting both the selected tenant id and its CorpID prevents the
    queue from deriving ownership from an ambient global selector.
    """
    summary = RefreshQueueSummary()
    if not tenant_id or not corp_id or not external_secret.strip():
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
            task.last_error_class = "unavailable"
            if task.attempt_count >= _MAX_ATTEMPTS:
                # Give up instead of retrying forever (GH-243): the terminal
                # state keeps this row out of the ready queue, so a single
                # unresolvable external userid cannot pin the tenant's batch
                # or grow the attempt counter without bound.
                task.state = _TERMINAL_STATE
                task.next_attempt_at = observed_at
            else:
                task.state = "pending"
                task.next_attempt_at = _retry_at(task.attempt_count, observed_at)
            summary.unavailable += 1
        session.commit()

    return summary
