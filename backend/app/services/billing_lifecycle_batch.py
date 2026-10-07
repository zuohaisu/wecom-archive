"""Idempotent per-tenant billing lifecycle batch runner (RND-402).

Scans only commercial tenants — those that already have a ``Subscription``
row — and advances each tenant's authoritative Subscription / Tenant
service projection through :func:`reconcile_tenant_billing_lifecycle`.
Deliberate invariants:

* Legacy and self-host tenants without a Subscription are never scanned:
  a deployment not configured for commercialization is never
  accidentally frozen by this batch.
* ``suspended`` tenants are scanned (their Subscription can still
  legally move ``grace -> expired``) but the reconcile routine never
  clears a manual suspension — see ``billing_lifecycle``.
* Each tenant is reconciled inside its own transaction with a row lock
  (``with_for_update``), so concurrent batches serialize per tenant and
  one tenant's failure never aborts the others.
* Reconcile is replay-safe: running the batch twice applies no
  duplicate transitions and writes no duplicate audit rows.
* Bounded per-tenant retries absorb transient database errors; repeated
  failures are counted and isolated, never fatal to the batch.
* The summary and CLI output contain aggregate counts only — no tenant
  identifiers, subscription values, or message content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Subscription, Tenant
from app.services.billing_lifecycle import reconcile_tenant_billing_lifecycle
from app.settings import APP_EDITION_CLOUD, get_app_edition

DEFAULT_BATCH_RETRIES = 2


@dataclass
class LifecycleBatchSummary:
    scanned: int
    changed: int
    unchanged: int
    failed: int
    skipped_suspended: int
    subscription_changed: int
    tenant_changed: int
    # Transition counts keyed by "from->to" for tenant lifecycle status;
    # values are integers only, never identifiers.
    tenant_transitions: dict = field(default_factory=dict)


def _commercial_tenant_ids(db: Session, limit: int) -> list[str]:
    """Ids of tenants with a Subscription row, oldest first, bounded.

    The scan deliberately excludes tenants without a Subscription so the
    batch can never reproject (and freeze) a legacy/self-host tenant that
    was never commercialized.
    """
    return list(
        db.scalars(
            select(Tenant.id)
            .join(Subscription, Subscription.tenant_id == Tenant.id)
            .order_by(Tenant.created_at.asc(), Tenant.id.asc())
            .limit(max(1, limit))
        ).all()
    )


def _reconcile_one(
    db: Session,
    tenant_id: str,
    *,
    at: datetime,
    retries: int,
) -> tuple[bool, bool, bool]:
    """Reconcile one tenant in its own transaction with bounded retries.

    Returns ``(applied, subscription_changed, tenant_changed)``.
    ``applied`` is False when every attempt failed (the exception is
    swallowed here — the caller counts it as a failed tenant and moves
    on).
    """
    for attempt in range(max(1, retries)):
        try:
            result = reconcile_tenant_billing_lifecycle(db, tenant_id, at=at)
            db.commit()
            return (
                True,
                bool(result.subscription_changed),
                bool(result.tenant_changed),
            )
        except Exception:  # noqa: BLE001 -- failure isolation boundary
            db.rollback()
            if attempt + 1 >= max(1, retries):
                return False, False, False
    return False, False, False  # pragma: no cover -- loop above always returns


def run_lifecycle_batch_once(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 50,
    retries: int = DEFAULT_BATCH_RETRIES,
) -> LifecycleBatchSummary:
    """Advance every commercial tenant's billing lifecycle projection.

    Idempotent and safe to run on any cadence; one tenant's failure is
    counted and never aborts the scan.  The caller owns no surrounding
    transaction — this function commits per tenant.
    """
    if get_app_edition() != APP_EDITION_CLOUD:
        raise RuntimeError("billing lifecycle worker is cloud-only")
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)

    tenant_ids = _commercial_tenant_ids(db, limit)
    summary = LifecycleBatchSummary(
        scanned=len(tenant_ids),
        changed=0,
        unchanged=0,
        failed=0,
        skipped_suspended=0,
        subscription_changed=0,
        tenant_changed=0,
    )
    for tenant_id in tenant_ids:
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            summary.failed += 1
            continue
        previous_status = tenant.lifecycle_status
        if previous_status == "suspended":
            # Still reconcile the Subscription projection (grace ->
            # expired must advance), but the tenant service projection is
            # locked by the suspension and must never be reprojected here.
            try:
                result = reconcile_tenant_billing_lifecycle(
                    db, tenant_id, at=checked_at
                )
                db.commit()
                summary.skipped_suspended += 1
                if result.subscription_changed:
                    summary.subscription_changed += 1
                    summary.changed += 1
                else:
                    summary.unchanged += 1
            except Exception:  # noqa: BLE001 -- failure isolation boundary
                db.rollback()
                summary.failed += 1
            continue

        applied, subscription_changed, tenant_changed = _reconcile_one(
            db, tenant_id, at=checked_at, retries=retries
        )
        if not applied:
            summary.failed += 1
            continue
        after = db.get(Tenant, tenant_id)
        after_status = after.lifecycle_status if after is not None else previous_status
        if subscription_changed:
            summary.subscription_changed += 1
        if tenant_changed:
            summary.tenant_changed += 1
        if subscription_changed or tenant_changed:
            summary.changed += 1
            if after_status != previous_status:
                transition = f"{previous_status}->{after_status}"
                summary.tenant_transitions[transition] = (
                    summary.tenant_transitions.get(transition, 0) + 1
                )
        else:
            summary.unchanged += 1
    return summary


def batch_summary_line(summary: LifecycleBatchSummary) -> str:
    """One sanitized, identifier-free log line for the batch CLI."""
    return (
        f"scanned={summary.scanned} changed={summary.changed} "
        f"unchanged={summary.unchanged} failed={summary.failed} "
        f"skipped_suspended={summary.skipped_suspended} "
        f"subscription_changed={summary.subscription_changed} "
        f"tenant_changed={summary.tenant_changed}"
    )
