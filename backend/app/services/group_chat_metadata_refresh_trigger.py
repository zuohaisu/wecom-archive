"""Bounded fail-soft refreshes for newly observed archived group rooms."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from enum import Enum

from sqlalchemy.orm import Session

from app.auth import get_wecom_token
from app.db.models import GroupChatMetadata
from app.db.session import get_engine
from app.services.group_chat_metadata import (
    apply_group_chat_lookup,
    sync_group_chat_metadata,
)
from app.wecom_contacts import GroupChatMetadataLookup

logger = logging.getLogger(__name__)

_MAX_PENDING_REFRESHES = 64
_dispatch_lock = threading.Lock()
_pending: deque[tuple[str, str, str]] = deque()
_pending_keys: set[tuple[str, str, str]] = set()
_worker_running = False


class GroupChatRefreshDispatch(str, Enum):
    ACCEPTED = "accepted"
    COALESCED = "coalesced"
    SKIPPED_CAPACITY = "skipped-capacity"
    FAILED = "dispatch-failed"


def _needs_initial_refresh(session: Session, tenant_id: str, roomid: str) -> bool:
    metadata = (
        session.query(GroupChatMetadata)
        .filter(
            GroupChatMetadata.tenant_id == tenant_id,
            GroupChatMetadata.roomid == roomid,
        )
        .first()
    )
    return metadata is None or metadata.display_name is None or metadata.sync_status != "resolved"


def refresh_group_chat_metadata_from_archive(
    tenant_id: str, corp_id: str, roomid: str
) -> None:
    """Resolve one newly observed room without delaying archive decryption."""
    try:
        with Session(get_engine()) as session:
            if not _needs_initial_refresh(session, tenant_id, roomid):
                logger.info("group_chat_metadata_refresh result=skipped reason=already_resolved")
                return

            external_secret = os.environ.get("WECOM_EXTERNAL_CONTACT_SECRET", "").strip()
            if not external_secret:
                outcome = apply_group_chat_lookup(
                    session,
                    tenant_id=tenant_id,
                    roomid=roomid,
                    lookup=GroupChatMetadataLookup(name=None, status="not_configured"),
                )
            else:
                try:
                    token = get_wecom_token(
                        corp_id, external_secret, cache_key=f"{corp_id}:external_contact"
                    )
                except Exception:  # noqa: BLE001 -- no credential/provider detail is safe here
                    outcome = apply_group_chat_lookup(
                        session,
                        tenant_id=tenant_id,
                        roomid=roomid,
                        lookup=GroupChatMetadataLookup(name=None, status="token_unavailable"),
                    )
                else:
                    outcome = sync_group_chat_metadata(
                        session,
                        tenant_id=tenant_id,
                        roomid=roomid,
                        access_token=token,
                    )
            session.commit()
    except Exception:  # noqa: BLE001 -- archive data is already committed; never block it
        logger.info("group_chat_metadata_refresh result=failed")
        return

    logger.info(
        "group_chat_metadata_refresh result=completed action=%s status=%s",
        outcome.action,
        outcome.status,
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
            refresh_group_chat_metadata_from_archive(*key)
        finally:
            with _dispatch_lock:
                _pending_keys.discard(key)


def dispatch_group_chat_metadata_refresh(
    tenant_id: str, corp_id: str, roomid: str
) -> GroupChatRefreshDispatch:
    """Queue one coalesced room lookup; capacity overflow remains retryable."""
    global _worker_running
    key = (tenant_id, corp_id, roomid)
    with _dispatch_lock:
        if key in _pending_keys:
            return GroupChatRefreshDispatch.COALESCED
        if len(_pending) >= _MAX_PENDING_REFRESHES:
            return GroupChatRefreshDispatch.SKIPPED_CAPACITY
        _pending.append(key)
        _pending_keys.add(key)
        if _worker_running:
            return GroupChatRefreshDispatch.ACCEPTED
        _worker_running = True
        try:
            threading.Thread(
                target=_drain_refreshes,
                name="wecom-group-chat-metadata-refresh",
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001 -- dispatch must be fail-soft
            _pending.pop()
            _pending_keys.discard(key)
            _worker_running = False
            return GroupChatRefreshDispatch.FAILED
    return GroupChatRefreshDispatch.ACCEPTED
