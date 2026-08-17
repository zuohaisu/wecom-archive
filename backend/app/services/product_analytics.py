"""Privacy-minimal product-use event recording and platform read models."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import logging
import uuid

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ProductAnalyticsEvent, Tenant
from app.schemas.product_analytics import (
    ALL_EVENT_NAMES,
    CORE_WORKFLOW_EVENT_NAMES,
    FEEDBACK_SUBMITTED,
    FRONTEND_EVENT_NAMES,
    LANGUAGE_CHANGED,
    LOGIN_FAILED,
    LOGIN_SUCCEEDED,
    ProductAnalyticsEventIn,
    event_class,
)
from app.settings import get_product_analytics_settings

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 180
_MAX_WINDOW_DAYS = 93
_PROVISIONED_LIFECYCLE_STATES = ("active", "frozen", "suspended")
_FUNCTION_EVENT_NAMES = tuple(
    sorted(ALL_EVENT_NAMES - {LOGIN_SUCCEEDED, LOGIN_FAILED})
)
_EVENT_ATTRIBUTES = {
    "product.conversation.review_opened.v1": frozenset(),
    "product.directory.view_selected.v1": frozenset({"view_kind"}),
    "product.directory.subject_selected.v1": frozenset({"subject_kind"}),
    "product.conversation.detail_opened.v1": frozenset(),
    "product.conversation.older_messages_loaded.v1": frozenset(),
    "product.search.executed.v1": frozenset({"search_scope"}),
    "product.search.filter_applied.v1": frozenset(
        {"filter_dimension", "filter_action"}
    ),
    "product.media.preview_opened.v1": frozenset(),
    "product.settings.opened.v1": frozenset(),
}


class ProductAnalyticsValidationError(ValueError):
    """Raised for a rejected event or a bounded analytics query."""


class ProductAnalyticsNotFoundError(LookupError):
    """Raised when a requested tenant no longer exists."""


def product_analytics_is_enabled() -> bool:
    value = get_product_analytics_settings().product_analytics_enabled.strip().lower()
    return value in {"1", "true", "yes", "on"}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ProductAnalyticsValidationError("occurred_at must include a timezone")
    return value.astimezone(timezone.utc)


def _normalise_frontend_time(value: datetime, *, received_at: datetime) -> datetime:
    occurred_at = _as_utc(value)
    # A stale queued page or a client clock far in the future must not poison
    # a time-series.  The event is still useful, so safely use receipt time.
    if occurred_at < received_at - timedelta(days=1) or occurred_at > received_at + timedelta(minutes=5):
        return received_at
    return occurred_at


def _record(
    db: Session,
    *,
    event_id: str,
    event_name: str,
    tenant_id: str | None,
    admin_user_id: str | None,
    source: str,
    attributes: dict[str, str],
    occurred_at: datetime,
    received_at: datetime,
) -> bool:
    """Insert once; duplicate logical events are accepted but not counted."""
    try:
        with db.begin_nested():
            db.add(
                ProductAnalyticsEvent(
                    event_id=event_id,
                    event_name=event_name,
                    schema_version=1,
                    event_class=event_class(event_name),
                    source=source,
                    tenant_id=tenant_id,
                    admin_user_id=admin_user_id,
                    attributes=attributes,
                    occurred_at=occurred_at,
                    received_at=received_at,
                )
            )
            db.flush()
        return True
    except IntegrityError:
        # event_id is the contract's idempotency key.  Do not surface a 409
        # to a best-effort caller or count the replay a second time.
        return False


def record_frontend_event(
    db: Session,
    *,
    event: ProductAnalyticsEventIn,
    tenant_id: str,
    admin_user_id: str,
    received_at: datetime | None = None,
) -> bool:
    """Validate and persist one browser event in its dedicated request."""
    if not product_analytics_is_enabled():
        return False
    event_name = event.event_name
    if event_name not in FRONTEND_EVENT_NAMES:
        raise ProductAnalyticsValidationError("event is not browser-collectable")
    attributes = event.attributes()
    if set(attributes) != _EVENT_ATTRIBUTES[event_name]:
        raise ProductAnalyticsValidationError("event attributes do not match contract")
    now = _as_utc(received_at or datetime.now(timezone.utc))
    return _record(
        db,
        event_id=str(event.event_id),
        event_name=event_name,
        tenant_id=tenant_id,
        admin_user_id=admin_user_id,
        source="frontend",
        attributes=attributes,
        occurred_at=_normalise_frontend_time(event.occurred_at, received_at=now),
        received_at=now,
    )


def record_backend_event_best_effort(
    db: Session,
    *,
    event_name: str,
    tenant_id: str | None,
    admin_user_id: str | None,
    occurred_at: datetime | None = None,
) -> bool:
    """Record a backend fact without coupling a primary transaction to it.

    The caller can invoke this only *after* its login/preferences/feedback
    transaction succeeds.  A separately committed session prevents analytics
    storage failures from altering the business response.
    """
    if not product_analytics_is_enabled():
        return False
    if event_name not in {LOGIN_SUCCEEDED, LOGIN_FAILED, LANGUAGE_CHANGED, FEEDBACK_SUBMITTED}:
        raise ProductAnalyticsValidationError("event is not backend-collectable")
    now = datetime.now(timezone.utc)
    try:
        factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        with factory() as analytics_db:
            recorded = _record(
                analytics_db,
                event_id=str(uuid.uuid4()),
                event_name=event_name,
                tenant_id=tenant_id,
                admin_user_id=admin_user_id,
                source="backend",
                attributes={},
                occurred_at=_as_utc(occurred_at or now),
                received_at=now,
            )
            analytics_db.commit()
            return recorded
    except Exception:
        # Never include event attributes or identifiers in logs: analytics
        # failures are observability-only and must not become a data leak.
        logger.warning("product analytics backend event was dropped", exc_info=True)
        return False


def purge_expired_events(db: Session, *, at: datetime | None = None) -> int:
    """Delete raw analytics facts after the documented 180-day retention."""
    now = _as_utc(at or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=_RETENTION_DAYS)
    deleted = db.query(ProductAnalyticsEvent).filter(
        ProductAnalyticsEvent.occurred_at < cutoff
    ).delete(synchronize_session=False)
    return int(deleted)


def resolve_window(
    *,
    starts_at: datetime | None,
    ends_at: datetime | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    reference = _as_utc(now or datetime.now(timezone.utc))
    start = _as_utc(starts_at) if starts_at is not None else reference - timedelta(days=30)
    end = _as_utc(ends_at) if ends_at is not None else reference
    if start >= end or end - start > timedelta(days=_MAX_WINDOW_DAYS):
        raise ProductAnalyticsValidationError("invalid analytics window")
    return start, end


def validate_event_name(value: str | None) -> str | None:
    if value is not None and value not in ALL_EVENT_NAMES:
        raise ProductAnalyticsValidationError("invalid event filter")
    return value


def _event_statement(
    *,
    starts_at: datetime,
    ends_at: datetime,
    tenant_id: str | None,
    event_name: str | None,
):
    statement = select(ProductAnalyticsEvent).where(
        ProductAnalyticsEvent.occurred_at >= starts_at,
        ProductAnalyticsEvent.occurred_at < ends_at,
    )
    if tenant_id is not None:
        statement = statement.where(ProductAnalyticsEvent.tenant_id == tenant_id)
    if event_name is not None:
        statement = statement.where(ProductAnalyticsEvent.event_name == event_name)
    return statement


def _provisioned_tenants(db: Session, tenant_id: str | None) -> list[Tenant]:
    statement = select(Tenant).where(Tenant.lifecycle_status.in_(_PROVISIONED_LIFECYCLE_STATES))
    if tenant_id is not None:
        statement = statement.where(Tenant.id == tenant_id)
    return list(db.scalars(statement).all())


def _filtered_core_names(event_name: str | None) -> tuple[str, ...]:
    if event_name is not None:
        return (event_name,) if event_name in CORE_WORKFLOW_EVENT_NAMES else ()
    return tuple(CORE_WORKFLOW_EVENT_NAMES)


def _event_counts(
    db: Session,
    *,
    starts_at: datetime,
    ends_at: datetime,
    tenant_id: str | None,
    event_name: str | None,
) -> dict[str, tuple[int, int, int]]:
    statement = _event_statement(
        starts_at=starts_at,
        ends_at=ends_at,
        tenant_id=tenant_id,
        event_name=event_name,
    ).with_only_columns(
        ProductAnalyticsEvent.event_name,
        func.count(ProductAnalyticsEvent.event_id),
        func.count(func.distinct(ProductAnalyticsEvent.tenant_id)),
        func.count(func.distinct(ProductAnalyticsEvent.admin_user_id)),
    ).group_by(ProductAnalyticsEvent.event_name)
    return {
        name: (int(event_count), int(tenant_count), int(admin_count))
        for name, event_count, tenant_count, admin_count in db.execute(statement).all()
    }


def _login_counts(counts: dict[str, tuple[int, int, int]]) -> dict:
    success_count = counts.get(LOGIN_SUCCEEDED, (0, 0, 0))[0]
    failure_count = counts.get(LOGIN_FAILED, (0, 0, 0))[0]
    total = success_count + failure_count
    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "failure_rate": (failure_count / total) if total else None,
    }


def _tenant_rollups(
    db: Session,
    *,
    tenant_ids: set[str],
    starts_at: datetime,
    ends_at: datetime,
    event_name: str | None,
) -> dict[str, dict]:
    if not tenant_ids:
        return {}
    core_names = _filtered_core_names(event_name)
    statement = _event_statement(
        starts_at=starts_at,
        ends_at=ends_at,
        tenant_id=None,
        event_name=event_name,
    ).where(ProductAnalyticsEvent.tenant_id.in_(tenant_ids))
    last_login = func.max(
        case(
            (ProductAnalyticsEvent.event_name == LOGIN_SUCCEEDED, ProductAnalyticsEvent.occurred_at),
            else_=None,
        )
    )
    core_condition = ProductAnalyticsEvent.event_name.in_(core_names) if core_names else False
    last_product_event = func.max(
        case((core_condition, ProductAnalyticsEvent.occurred_at), else_=None)
    )
    active_admin_count = func.count(
        func.distinct(
            case((core_condition, ProductAnalyticsEvent.admin_user_id), else_=None)
        )
    )
    statement = statement.with_only_columns(
        ProductAnalyticsEvent.tenant_id,
        last_login,
        last_product_event,
        active_admin_count,
        func.count(ProductAnalyticsEvent.event_id),
    ).group_by(ProductAnalyticsEvent.tenant_id)
    return {
        tenant_id: {
            "last_login_at": last_login_at,
            "last_product_event_at": last_product_event_at,
            "active_admin_count": int(active_admin_count or 0),
            "event_count": int(event_count or 0),
        }
        for tenant_id, last_login_at, last_product_event_at, active_admin_count, event_count in db.execute(statement).all()
    }


def _trend_day_expression(db: Session):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.date(func.timezone("Asia/Shanghai", ProductAnalyticsEvent.occurred_at))
    # SQLite compatibility for the offline suite. Production uses the branch
    # above, which explicitly preserves the documented Asia/Shanghai day.
    return func.date(ProductAnalyticsEvent.occurred_at)


def _trends(
    db: Session,
    *,
    starts_at: datetime,
    ends_at: datetime,
    tenant_id: str | None,
    event_name: str | None,
) -> list[dict]:
    day = _trend_day_expression(db)
    base = _event_statement(
        starts_at=starts_at,
        ends_at=ends_at,
        tenant_id=tenant_id,
        event_name=event_name,
    )
    event_rows = db.execute(
        base.with_only_columns(day, ProductAnalyticsEvent.event_name, func.count())
        .group_by(day, ProductAnalyticsEvent.event_name)
        .order_by(day)
    ).all()
    core_names = _filtered_core_names(event_name)
    active_rows = []
    if core_names:
        active_rows = db.execute(
            base.where(ProductAnalyticsEvent.event_name.in_(core_names))
            .with_only_columns(day, func.count(func.distinct(ProductAnalyticsEvent.tenant_id)))
            .group_by(day)
            .order_by(day)
        ).all()
    by_day: dict[date, dict] = defaultdict(
        lambda: {
            "login_success_count": 0,
            "login_failure_count": 0,
            "active_tenant_count": 0,
            "event_counts": {},
        }
    )
    for raw_day, name, count in event_rows:
        parsed_day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
        item = by_day[parsed_day]
        item["event_counts"][name] = int(count)
        if name == LOGIN_SUCCEEDED:
            item["login_success_count"] = int(count)
        elif name == LOGIN_FAILED:
            item["login_failure_count"] = int(count)
    for raw_day, count in active_rows:
        parsed_day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
        by_day[parsed_day]["active_tenant_count"] = int(count)

    days = []
    current = starts_at.astimezone(timezone.utc).date()
    final = (ends_at - timedelta(microseconds=1)).astimezone(timezone.utc).date()
    while current <= final:
        values = by_day[current]
        days.append({"date": current, **values})
        current += timedelta(days=1)
    return days


def _function_adoption(
    counts: dict[str, tuple[int, int, int]], event_name: str | None) -> list[dict]:
    names = (event_name,) if event_name in _FUNCTION_EVENT_NAMES else _FUNCTION_EVENT_NAMES
    return [
        {
            "event_name": name,
            "event_count": counts.get(name, (0, 0, 0))[0],
            "tenant_count": counts.get(name, (0, 0, 0))[1],
            "admin_count": counts.get(name, (0, 0, 0))[2],
        }
        for name in names
    ]


def overview(
    db: Session,
    *,
    starts_at: datetime,
    ends_at: datetime,
    tenant_id: str | None = None,
    event_name: str | None = None,
) -> dict:
    validate_event_name(event_name)
    tenants = _provisioned_tenants(db, tenant_id)
    rollups = _tenant_rollups(
        db,
        tenant_ids={tenant.id for tenant in tenants},
        starts_at=starts_at,
        ends_at=ends_at,
        event_name=event_name,
    )
    counts = _event_counts(
        db,
        starts_at=starts_at,
        ends_at=ends_at,
        tenant_id=tenant_id,
        event_name=event_name,
    )
    active_tenant_count = sum(
        1 for tenant in tenants if rollups.get(tenant.id, {}).get("last_product_event_at") is not None
    )
    core_names = _filtered_core_names(event_name)
    active_admin_count = 0
    if core_names:
        statement = _event_statement(
            starts_at=starts_at,
            ends_at=ends_at,
            tenant_id=tenant_id,
            event_name=event_name,
        ).where(ProductAnalyticsEvent.event_name.in_(core_names))
        active_admin_count = int(
            db.scalar(
                statement.with_only_columns(
                    func.count(func.distinct(ProductAnalyticsEvent.admin_user_id))
                )
            )
            or 0
        )
    return {
        "starts_at": starts_at,
        "ends_at": ends_at,
        "event_name": event_name,
        "tenant_id": tenant_id,
        "provisioned_tenant_count": len(tenants),
        "active_tenant_count": active_tenant_count,
        "inactive_tenant_count": len(tenants) - active_tenant_count,
        "active_admin_count": active_admin_count,
        "login": _login_counts(counts),
        "function_adoption": _function_adoption(counts, event_name),
        "trends": _trends(
            db,
            starts_at=starts_at,
            ends_at=ends_at,
            tenant_id=tenant_id,
            event_name=event_name,
        ),
    }


def list_tenants(
    db: Session,
    *,
    starts_at: datetime,
    ends_at: datetime,
    page: int,
    page_size: int,
    activity_status: str = "all",
    tenant_id: str | None = None,
    event_name: str | None = None,
) -> dict:
    if activity_status not in {"all", "active", "inactive"}:
        raise ProductAnalyticsValidationError("invalid activity status")
    validate_event_name(event_name)
    tenants = _provisioned_tenants(db, tenant_id)
    rollups = _tenant_rollups(
        db,
        tenant_ids={tenant.id for tenant in tenants},
        starts_at=starts_at,
        ends_at=ends_at,
        event_name=event_name,
    )
    items = []
    for tenant in tenants:
        rollup = rollups.get(tenant.id, {})
        is_active = rollup.get("last_product_event_at") is not None
        status = "active" if is_active else "inactive"
        if activity_status != "all" and status != activity_status:
            continue
        items.append(
            {
                "tenant_id": tenant.id,
                "tenant_name": tenant.name,
                "tenant_slug": tenant.slug,
                "lifecycle_status": tenant.lifecycle_status,
                "last_login_at": rollup.get("last_login_at"),
                "last_product_event_at": rollup.get("last_product_event_at"),
                "active_admin_count": rollup.get("active_admin_count", 0),
                "event_count": rollup.get("event_count", 0),
                "activity_status": status,
            }
        )
    items.sort(
        key=lambda item: (
            item["activity_status"] != "inactive",
            item["last_product_event_at"] or datetime.min.replace(tzinfo=timezone.utc),
            item["tenant_name"],
        )
    )
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start : start + page_size], "page": page, "page_size": page_size, "total": total}


def tenant_detail(
    db: Session,
    *,
    tenant_id: str,
    starts_at: datetime,
    ends_at: datetime,
    event_name: str | None = None,
) -> dict:
    validate_event_name(event_name)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise ProductAnalyticsNotFoundError("tenant does not exist")
    rollup = _tenant_rollups(
        db,
        tenant_ids={tenant.id},
        starts_at=starts_at,
        ends_at=ends_at,
        event_name=event_name,
    ).get(tenant.id, {})
    counts = _event_counts(
        db,
        starts_at=starts_at,
        ends_at=ends_at,
        tenant_id=tenant.id,
        event_name=event_name,
    )
    return {
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "tenant_slug": tenant.slug,
        "lifecycle_status": tenant.lifecycle_status,
        "last_login_at": rollup.get("last_login_at"),
        "last_product_event_at": rollup.get("last_product_event_at"),
        "active_admin_count": rollup.get("active_admin_count", 0),
        "event_count": rollup.get("event_count", 0),
        "activity_status": (
            "active" if rollup.get("last_product_event_at") is not None else "inactive"
        ),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "login": _login_counts(counts),
        "function_adoption": _function_adoption(counts, event_name),
        "trends": _trends(
            db,
            starts_at=starts_at,
            ends_at=ends_at,
            tenant_id=tenant.id,
            event_name=event_name,
        ),
    }
