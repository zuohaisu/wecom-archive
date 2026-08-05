"""Tenant-scoped persistence helpers for current WeCom group-chat metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import GroupChatMetadata


@dataclass(frozen=True)
class GroupChatMetadataWrite:
    action: str  # created | updated | skipped
    metadata: GroupChatMetadata


def upsert_group_chat_metadata(
    session: Session,
    *,
    tenant_id: str,
    roomid: str,
    display_name: Optional[str],
    source: str,
    sync_status: str,
    checked_at: datetime,
) -> GroupChatMetadataWrite:
    """Persist one current group result without cross-tenant writes or churn.

    The database uniqueness constraint is the concurrency boundary.  A racing
    insert retries as a locked tenant/room lookup; unchanged state does not
    touch ``updated_at`` or ``last_checked_at``.
    """
    metadata = (
        session.query(GroupChatMetadata)
        .filter(
            GroupChatMetadata.tenant_id == tenant_id,
            GroupChatMetadata.roomid == roomid,
        )
        .with_for_update()
        .first()
    )
    if metadata is None:
        created = GroupChatMetadata(
            tenant_id=tenant_id,
            roomid=roomid,
            display_name=display_name,
            source=source,
            sync_status=sync_status,
            last_checked_at=checked_at,
        )
        try:
            with session.begin_nested():
                session.add(created)
                session.flush()
            return GroupChatMetadataWrite(action="created", metadata=created)
        except IntegrityError:
            metadata = (
                session.query(GroupChatMetadata)
                .filter(
                    GroupChatMetadata.tenant_id == tenant_id,
                    GroupChatMetadata.roomid == roomid,
                )
                .with_for_update()
                .one()
            )

    changed = False
    # Invalid/missing metadata must never erase a usable previously observed
    # name.  A valid current name is the only input allowed to replace it.
    if display_name is not None and metadata.display_name != display_name:
        metadata.display_name = display_name
        changed = True
    if metadata.source != source:
        metadata.source = source
        changed = True
    if metadata.sync_status != sync_status:
        metadata.sync_status = sync_status
        changed = True
    if changed:
        metadata.last_checked_at = checked_at
        return GroupChatMetadataWrite(action="updated", metadata=metadata)
    return GroupChatMetadataWrite(action="skipped", metadata=metadata)


def load_group_chat_display_names(
    session: Session, tenant_id: str, roomids: Iterable[Optional[str]]
) -> dict[str, str]:
    """Return only usable current names for one tenant and requested rooms."""
    requested = {roomid for roomid in roomids if isinstance(roomid, str) and roomid}
    if not requested:
        return {}
    return {
        roomid: display_name
        for roomid, display_name in (
            session.query(GroupChatMetadata.roomid, GroupChatMetadata.display_name)
            .filter(
                GroupChatMetadata.tenant_id == tenant_id,
                GroupChatMetadata.roomid.in_(requested),
                GroupChatMetadata.display_name.isnot(None),
            )
            .all()
        )
        if isinstance(display_name, str) and display_name
    }
