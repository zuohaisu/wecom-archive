"""Safe, tenant-scoped current-name sync for WeCom customer groups (RND-340)."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.db.group_chat_metadata import GroupChatMetadataWrite, upsert_group_chat_metadata
from app.db.models import ArchiveMessage
from app.wecom_contacts import GroupChatMetadataLookup, fetch_group_chat_metadata

_GROUP_CHAT_SOURCE = "wecom_external_groupchat"
_MAX_GROUP_NAME_LENGTH = 128
_ERROR_STATUSES = frozenset(
    {"transport_error", "malformed_response", "api_error", "rate_limited"}
)


@dataclass(frozen=True)
class GroupChatSyncOutcome:
    action: str  # created | updated | skipped
    status: str


@dataclass
class GroupChatSyncSummary:
    scanned: int = 0
    resolved: int = 0
    unresolved: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0


def normalize_group_chat_name(value: object) -> Optional[str]:
    """Return a bounded visible group title or ``None`` for unusable input.

    Group names are external, user-controlled metadata.  Normalizing only for
    display/comparison prevents blank, control-only, bidi-only, and excessive
    values from replacing a good stored title while leaving the room ID intact.
    """
    if not isinstance(value, str):
        return None
    # Do not silently delete embedded control, formatting, or surrogate
    # characters: doing so could turn a provider value that is unsafe to
    # display (for example ``Sales\\x00Team``) into an accepted different
    # title.  Reject the complete input instead.  Check both forms because
    # NFKC is used for the stored/displayed representation.
    blocked_categories = {"Cc", "Cf", "Cs"}
    if any(unicodedata.category(char) in blocked_categories for char in value):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char) in blocked_categories for char in normalized):
        return None
    compact = " ".join(normalized.split())
    if not compact:
        return None
    if not any(unicodedata.category(char)[0] not in {"M", "Z", "C"} for char in compact):
        return None
    if len(compact) > _MAX_GROUP_NAME_LENGTH:
        return None
    return compact


def _valid_roomid(roomid: object) -> bool:
    """Validate only storage safety; never transform the archive correlation key."""
    return isinstance(roomid, str) and bool(roomid) and len(roomid) <= 64


def apply_group_chat_lookup(
    session: Session,
    *,
    tenant_id: str,
    roomid: object,
    lookup: GroupChatMetadataLookup,
    checked_at: Optional[datetime] = None,
) -> GroupChatSyncOutcome:
    """Apply one already-fetched lookup with no raw API payload persistence."""
    if not _valid_roomid(roomid):
        # A malformed archive value has no safe metadata record or formal API
        # request.  It remains on the existing display fallback.
        return GroupChatSyncOutcome(action="skipped", status="invalid_roomid")

    status = lookup.status
    name = normalize_group_chat_name(lookup.name) if status == "resolved" else None
    if status == "resolved" and name is None:
        status = "invalid_name"

    write: GroupChatMetadataWrite = upsert_group_chat_metadata(
        session,
        tenant_id=tenant_id,
        roomid=roomid,
        display_name=name,
        source=_GROUP_CHAT_SOURCE,
        sync_status=status,
        checked_at=checked_at or datetime.now(timezone.utc),
    )
    return GroupChatSyncOutcome(action=write.action, status=status)


def sync_group_chat_metadata(
    session: Session,
    *,
    tenant_id: str,
    roomid: object,
    access_token: str,
) -> GroupChatSyncOutcome:
    """Read official group metadata once, then persist only safe current state."""
    if not _valid_roomid(roomid):
        return GroupChatSyncOutcome(action="skipped", status="invalid_roomid")
    lookup = fetch_group_chat_metadata(access_token, roomid)
    return apply_group_chat_lookup(
        session,
        tenant_id=tenant_id,
        roomid=roomid,
        lookup=lookup,
    )


def backfill_group_chat_metadata(
    session: Session,
    *,
    tenant_id: str,
    access_token: str,
    limit: int,
    lookup: Callable[[str, str], GroupChatMetadataLookup] = fetch_group_chat_metadata,
) -> GroupChatSyncSummary:
    """Refresh a bounded, de-duplicated historical room slice for one tenant.

    The caller controls the batch limit and transaction boundary.  Repeated
    runs safely revisit known rooms so a renamed group is reflected, while the
    persistence layer suppresses same-state write amplification.
    """
    if limit < 1:
        raise ValueError("limit must be positive")

    roomids = [
        roomid
        for (roomid,) in (
            session.query(ArchiveMessage.roomid)
            .filter(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessage.roomid.isnot(None),
                ArchiveMessage.roomid != "",
            )
            .distinct()
            .order_by(ArchiveMessage.roomid.asc())
            .limit(limit)
            .all()
        )
    ]
    summary = GroupChatSyncSummary(scanned=len(roomids))
    for roomid in roomids:
        if not _valid_roomid(roomid):
            outcome = GroupChatSyncOutcome(action="skipped", status="invalid_roomid")
        else:
            outcome = apply_group_chat_lookup(
                session,
                tenant_id=tenant_id,
                roomid=roomid,
                lookup=lookup(access_token, roomid),
            )

        if outcome.status == "resolved":
            summary.resolved += 1
        else:
            summary.unresolved += 1
        if outcome.status in _ERROR_STATUSES:
            summary.errors += 1
        if outcome.action == "created":
            summary.created += 1
        elif outcome.action == "updated":
            summary.updated += 1
        else:
            summary.skipped += 1
    return summary
