"""Bounded asynchronous refresh dispatch for WeCom external-contact events."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from enum import Enum

from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.services.external_contact_sync import refresh_external_contact

logger = logging.getLogger(__name__)

# A callback burst must not create one thread per customer. Overflow safely
# falls back to the periodic full external-contact reconciliation.
_MAX_PENDING_REFRESHES = 64
_dispatch_lock = threading.Lock()
_pending: deque[tuple[str, str, str]] = deque()
_pending_keys: set[tuple[str, str, str]] = set()
_worker_running = False


class ExternalContactRefreshDispatch(str, Enum):
    ACCEPTED = "accepted"
    COALESCED = "coalesced"
    SKIPPED_CAPACITY = "skipped-capacity"
    FAILED = "dispatch-failed"


def refresh_external_contact_from_callback(
    tenant_id: str, corp_id: str, external_userid: str
) -> None:
    """Fetch one current profile and commit only safe identity changes."""
    external_secret = os.environ.get("WECOM_EXTERNAL_CONTACT_SECRET", "").strip()
    if not external_secret:
        logger.info("external_contact_refresh result=skipped reason=not_configured")
        return

    try:
        with Session(get_engine()) as session:
            outcome = refresh_external_contact(
                session,
                tenant_id,
                corp_id,
                external_secret,
                external_userid,
            )
            session.commit()
    except Exception:  # noqa: BLE001 -- callbacks must not expose response/PII details
        logger.info("external_contact_refresh result=failed")
        return

    if not outcome.found:
        logger.info("external_contact_refresh result=skipped reason=not_found")
        return
    logger.info(
        "external_contact_refresh result=completed inserted=%d nickname_changed=%d "
        "follow_relationship_changes=%d",
        int(outcome.inserted),
        int(outcome.nickname_changed),
        outcome.follow_relationship_changes,
    )


def _drain_refreshes() -> None:
    global _worker_running
    while True:
        with _dispatch_lock:
            if not _pending:
                _worker_running = False
                return
            key = _pending.popleft()
        try:
            refresh_external_contact_from_callback(*key)
        finally:
            with _dispatch_lock:
                _pending_keys.discard(key)


def dispatch_external_contact_refresh(
    tenant_id: str, corp_id: str, external_userid: str
) -> ExternalContactRefreshDispatch:
    """Queue a coalesced refresh without blocking the callback acknowledgement."""
    global _worker_running
    key = (tenant_id, corp_id, external_userid)
    with _dispatch_lock:
        if key in _pending_keys:
            return ExternalContactRefreshDispatch.COALESCED
        if len(_pending) >= _MAX_PENDING_REFRESHES:
            return ExternalContactRefreshDispatch.SKIPPED_CAPACITY

        _pending.append(key)
        _pending_keys.add(key)
        if _worker_running:
            return ExternalContactRefreshDispatch.ACCEPTED
        _worker_running = True
        try:
            thread = threading.Thread(
                target=_drain_refreshes,
                name="wecom-external-contact-refresh",
                daemon=True,
            )
            thread.start()
        except Exception:  # noqa: BLE001 -- keep callback failure classification safe
            _pending.pop()
            _pending_keys.discard(key)
            _worker_running = False
            return ExternalContactRefreshDispatch.FAILED

    return ExternalContactRefreshDispatch.ACCEPTED
