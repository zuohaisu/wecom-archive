"""Durable, non-blocking dispatch for external-contact metadata refreshes.

WeCom callbacks and archive decryption must never call the external-contact
API themselves. They only coalesce an identifier into the database-backed
task source and touch one identifier-free signal. A systemd path unit or the
self-host container scheduler may consume it; a separate worker owns network I/O
and retry timing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy.orm import Session

from app.db.models import ExternalContactRefreshTask
from app.db.session import get_engine

logger = logging.getLogger(__name__)

_DEFAULT_SIGNAL_PATH = (
    "/srv/apps/wecom-archive-365/shared/run/wecom-external-contact-refresh.trigger"
)
_SOURCES = frozenset({"callback", "inbound-direct-message"})


class ExternalContactRefreshDispatch(str, Enum):
    """Safe aggregate outcomes visible to callers without exposing PII."""

    ACCEPTED = "accepted"
    COALESCED = "coalesced"
    FAILED = "dispatch-failed"


def _clean_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean if clean and len(clean) <= 64 else None


def _safe_source(value: object) -> str:
    return value if value in _SOURCES else "callback"


def enqueue_external_contact_refresh(
    session: Session,
    tenant_id: str,
    external_userid: str,
    *,
    source: str,
    now: datetime | None = None,
) -> ExternalContactRefreshDispatch:
    """Create or reactivate one durable refresh task in the caller's transaction."""
    clean_tenant_id = _clean_identifier(tenant_id)
    clean_external_userid = _clean_identifier(external_userid)
    if not clean_tenant_id or not clean_external_userid:
        return ExternalContactRefreshDispatch.FAILED

    observed_at = now or datetime.now(timezone.utc)
    task = (
        session.query(ExternalContactRefreshTask)
        .filter(
            ExternalContactRefreshTask.tenant_id == clean_tenant_id,
            ExternalContactRefreshTask.external_userid == clean_external_userid,
        )
        .first()
    )
    if task is None:
        session.add(
            ExternalContactRefreshTask(
                tenant_id=clean_tenant_id,
                external_userid=clean_external_userid,
                source=_safe_source(source),
                state="pending",
                next_attempt_at=observed_at,
            )
        )
        return ExternalContactRefreshDispatch.ACCEPTED

    # New events are evidence that a previous no-relation/API failure may no
    # longer apply. Make the existing task eligible now, without creating an
    # unbounded queue of duplicate identifiers.
    task.source = _safe_source(source)
    task.state = "pending"
    task.next_attempt_at = observed_at
    task.last_error_class = None
    return ExternalContactRefreshDispatch.COALESCED


def _signal_refresh_worker() -> bool:
    """Touch one identifier-free signal watched by the configured worker runner."""
    path = (
        os.environ.get("EXTERNAL_CONTACT_REFRESH_SIGNAL_PATH", "").strip()
        or _DEFAULT_SIGNAL_PATH
    )
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o750, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            os.utime(path, None)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def signal_external_contact_refresh_worker() -> bool:
    """Request a worker wake-up after a caller has committed task rows."""
    return _signal_refresh_worker()


def dispatch_external_contact_refresh(
    tenant_id: str,
    corp_id: str,
    external_userid: str,
    *,
    source: str = "callback",
) -> ExternalContactRefreshDispatch:
    """Persist a refresh request and return without doing external network I/O.

    ``corp_id`` remains part of the public callback seam for compatibility and
    tenant/corp binding happens before this function is called. The worker
    resolves credentials from its own trusted environment instead of carrying
    any credential or user identifier in a process argument or path signal.
    """
    del corp_id
    try:
        with Session(get_engine()) as session:
            outcome = enqueue_external_contact_refresh(
                session, tenant_id, external_userid, source=source
            )
            if outcome is ExternalContactRefreshDispatch.FAILED:
                return outcome
            session.commit()
    except Exception:  # noqa: BLE001 -- callback must fail closed without DB detail
        logger.info("external_contact_refresh_dispatch result=failed")
        return ExternalContactRefreshDispatch.FAILED

    # A signal failure does not discard the committed task: the periodic
    # worker timer will retry it. The caller can still acknowledge promptly.
    signal = signal_external_contact_refresh_worker()
    logger.info(
        "external_contact_refresh_dispatch result=%s signal=%s source=%s",
        outcome.value,
        "accepted" if signal else "deferred",
        _safe_source(source),
    )
    return outcome
