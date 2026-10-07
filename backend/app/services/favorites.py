"""Tenant-scoped, shared favorites for archived messages and media (RND-366).

The relation stores only stable target references and action metadata. Message
previews are projected from active ArchiveMessage rows at read time, and media
bytes remain behind the existing authenticated media routes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, cast, func, literal, or_, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.types import Integer, String

from app.audit import AuditAction, AuditObjectType, write_audit
from app.conversation_membership import (
    _derive_conversation_membership,
    _fetch_conversation_messages_compact,
    _load_recipients_map,
    _staff_ids_for_participants,
)
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import (
    AdminUser,
    ArchiveFavorite,
    ArchiveMessage,
    ArchiveMessageRecipient,
    Contact,
    ExternalContact,
    MediaFile,
)
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.schemas.favorites import (
    FavoriteBatchIn,
    FavoriteItemOut,
    FavoriteMutationIn,
    FavoriteMutationItemOut,
    FavoriteMutationOut,
    FavoriteObjectIn,
    FavoritePageOut,
    FavoriteStatusIn,
    FavoriteStatusItemOut,
    FavoriteStatusOut,
)
from app.services.message_deletion import active_message_filter

MAX_FAVORITE_BATCH = 100
_PREVIEW_LENGTH = 160


class FavoriteServiceError(RuntimeError):
    """Base error that is safe to map to a coarse HTTP error code."""


class FavoriteAuditUnavailable(FavoriteServiceError):
    """The audit row could not be persisted; the mutation must roll back."""


class FavoriteWriteUnavailable(FavoriteServiceError):
    """A database write failed or a concurrent outcome could not be resolved."""


@dataclass(frozen=True)
class _Target:
    object_type: str
    object_id: str
    archive_message_id: Optional[int] = None
    media_file_id: Optional[int] = None

    @property
    def key(self) -> tuple[str, str]:
        return self.object_type, self.object_id


def _normalize_target(item: FavoriteObjectIn) -> _Target:
    if item.object_type == "message":
        return _Target("message", item.object_id)
    media_id = int(item.object_id)
    return _Target("media", str(media_id), media_file_id=media_id)


def _unique_targets(items: list[FavoriteObjectIn]) -> tuple[list[_Target], dict[tuple[str, str], Optional[str]]]:
    targets: list[_Target] = []
    source_by_key: dict[tuple[str, str], Optional[str]] = {}
    for item in items:
        target = _normalize_target(item)
        if target.key not in source_by_key:
            targets.append(target)
            source_by_key[target.key] = item.source_page
    # This is also the transaction's row-lock order. Sorting the deduplicated
    # identities prevents reversed overlapping batches from acquiring the
    # same target locks in opposite order.
    targets.sort(key=lambda target: target.key)
    return targets, source_by_key


def _resolve_target(
    db: Session, tenant_id: str, target: _Target, *, lock: bool = False
) -> Optional[_Target]:
    if target.object_type == "message":
        statement = select(ArchiveMessage.id).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgid == target.object_id,
            active_message_filter(),
        )
        if lock:
            statement = statement.with_for_update()
        message_id = db.scalar(statement)
        if message_id is None:
            return None
        return _Target("message", target.object_id, archive_message_id=int(message_id))

    statement = (
        select(MediaFile.id, MediaFile.archive_message_id)
        .join(
            ArchiveMessage,
            and_(
                ArchiveMessage.id == MediaFile.archive_message_id,
                ArchiveMessage.tenant_id == MediaFile.tenant_id,
            ),
        )
        .where(
            MediaFile.id == target.media_file_id,
            MediaFile.tenant_id == tenant_id,
            ArchiveMessage.tenant_id == tenant_id,
            active_message_filter(),
        )
    )
    if lock:
        # Lock only the canonical media target. Locking the joined parent
        # ArchiveMessage as well can create cross-target cycles with message
        # favorites in a batch; permanent purge must delete this MediaFile
        # first, so this row lock still stabilizes the target lifecycle.
        statement = statement.with_for_update(of=MediaFile)
    row = db.execute(statement).one_or_none()
    if row is None:
        return None
    return _Target("media", str(row.id), media_file_id=int(row.id))


def _favorite_statement(tenant_id: str, target: _Target):
    identity = (
        ArchiveFavorite.archive_message_id == target.archive_message_id
        if target.object_type == "message"
        else ArchiveFavorite.media_file_id == target.media_file_id
    )
    return select(ArchiveFavorite).where(
        ArchiveFavorite.tenant_id == tenant_id,
        ArchiveFavorite.object_type == target.object_type,
        identity,
    )


def _is_favorited(db: Session, tenant_id: str, target: _Target) -> bool:
    return db.scalar(
        select(ArchiveFavorite.id).where(
            ArchiveFavorite.tenant_id == tenant_id,
            ArchiveFavorite.object_type == target.object_type,
            ArchiveFavorite.canceled_at.is_(None),
            (
                ArchiveFavorite.archive_message_id == target.archive_message_id
                if target.object_type == "message"
                else ArchiveFavorite.media_file_id == target.media_file_id
            ),
        )
    ) is not None


def _apply_one(
    db: Session,
    *,
    tenant_id: str,
    actor_id: str,
    target: _Target,
    action: str,
    source_page: Optional[str],
    now: datetime,
) -> str:
    resolved = _resolve_target(db, tenant_id, target, lock=True)
    if resolved is None:
        return "not_found"

    favorite = db.scalar(_favorite_statement(tenant_id, resolved).with_for_update())
    if action == "favorite":
        if favorite is not None and favorite.canceled_at is None:
            return "already_favorited"
        if favorite is not None:
            favorite.favorited_by_admin_user_id = actor_id
            favorite.favorited_at = now
            favorite.canceled_at = None
            favorite.canceled_by_admin_user_id = None
            favorite.source_page = source_page
            return "favorited"

        favorite = ArchiveFavorite(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            object_type=resolved.object_type,
            archive_message_id=resolved.archive_message_id,
            media_file_id=resolved.media_file_id,
            favorited_by_admin_user_id=actor_id,
            favorited_at=now,
            source_page=source_page,
        )
        try:
            with db.begin_nested():
                db.add(favorite)
                db.flush()
            return "favorited"
        except IntegrityError:
            # The tenant/object unique constraint serializes simultaneous
            # first-favorite requests. Re-read the winner under a row lock;
            # if the target disappeared during a concurrent purge, fail as
            # not-found rather than exposing a database constraint detail.
            favorite = db.scalar(_favorite_statement(tenant_id, resolved).with_for_update())
            if favorite is None:
                if _resolve_target(db, tenant_id, target) is None:
                    return "not_found"
                raise FavoriteWriteUnavailable("favorite_write_unavailable") from None
            if favorite.canceled_at is None:
                return "already_favorited"
            favorite.favorited_by_admin_user_id = actor_id
            favorite.favorited_at = now
            favorite.canceled_at = None
            favorite.canceled_by_admin_user_id = None
            favorite.source_page = source_page
            return "favorited"

    if favorite is None or favorite.canceled_at is not None:
        return "already_unfavorited"
    favorite.canceled_at = now
    favorite.canceled_by_admin_user_id = actor_id
    return "unfavorited"


def mutate_favorites(
    db: Session,
    *,
    tenant_id: str,
    actor_id: str,
    payload: FavoriteBatchIn,
    now: Optional[datetime] = None,
) -> FavoriteMutationOut:
    """Apply an idempotent batch and require its aggregate audit to persist.

    Per-target not-found results are non-disclosing (unknown, cross-tenant,
    soft-deleted and purged objects all look the same). Database or audit
    failures abort the whole outer transaction at the HTTP boundary.
    """
    if len(payload.items) > MAX_FAVORITE_BATCH:
        raise ValueError("batch_too_large")
    targets, source_by_key = _unique_targets(payload.items)
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    results_by_key: dict[tuple[str, str], str] = {}
    for target in targets:
        results_by_key[target.key] = _apply_one(
            db,
            tenant_id=tenant_id,
            actor_id=actor_id,
            target=target,
            action=payload.action,
            source_page=source_by_key[target.key],
            now=at,
        )

    result_items: list[FavoriteMutationItemOut] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.items:
        target = _normalize_target(item)
        duplicate = target.key in seen
        seen.add(target.key)
        result_items.append(
            FavoriteMutationItemOut(
                object_type=target.object_type,
                object_id=target.object_id,
                result=results_by_key[target.key],
                duplicate=duplicate,
            )
        )

    outcomes = list(results_by_key.values())
    applied = sum(value in ("favorited", "unfavorited") for value in outcomes)
    not_found = outcomes.count("not_found")
    unchanged = len(outcomes) - applied - not_found
    if not_found and applied:
        audit_outcome = "partial"
    elif not_found:
        audit_outcome = "not_found"
    elif applied:
        audit_outcome = "changed"
    else:
        audit_outcome = "unchanged"
    action = (
        AuditAction.FAVORITE_ADDED
        if payload.action == "favorite"
        else AuditAction.FAVORITE_REMOVED
    )
    audit_ok = write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor_id,
        action=action,
        object_type=AuditObjectType.FAVORITE,
        detail={
            "outcome": audit_outcome,
            "requested": len(payload.items),
            "unique": len(targets),
            "applied": applied,
            "unchanged": unchanged,
            "not_found": not_found,
        },
    )
    if not audit_ok:
        raise FavoriteAuditUnavailable("favorite_audit_unavailable")

    return FavoriteMutationOut(
        requested=len(payload.items),
        unique=len(targets),
        applied=applied,
        unchanged=unchanged,
        not_found=not_found,
        items=result_items,
    )


def mutate_favorite(
    db: Session,
    *,
    tenant_id: str,
    actor_id: str,
    payload: FavoriteMutationIn,
    action: str,
    now: Optional[datetime] = None,
) -> FavoriteMutationOut:
    batch = FavoriteBatchIn(action=action, items=[payload])
    return mutate_favorites(
        db, tenant_id=tenant_id, actor_id=actor_id, payload=batch, now=now
    )


def get_favorite_statuses(
    db: Session, *, tenant_id: str, payload: FavoriteStatusIn
) -> FavoriteStatusOut:
    targets, _sources = _unique_targets(payload.items)
    status_by_key: dict[tuple[str, str], tuple[str, Optional[bool]]] = {}
    for target in targets:
        resolved = _resolve_target(db, tenant_id, target)
        if resolved is None:
            status_by_key[target.key] = ("not_found", None)
        else:
            status_by_key[target.key] = ("found", _is_favorited(db, tenant_id, resolved))

    result_items: list[FavoriteStatusItemOut] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.items:
        target = _normalize_target(item)
        duplicate = target.key in seen
        seen.add(target.key)
        outcome, is_favorited = status_by_key[target.key]
        result_items.append(
            FavoriteStatusItemOut(
                object_type=target.object_type,
                object_id=target.object_id,
                result=outcome,
                is_favorited=is_favorited,
                duplicate=duplicate,
            )
        )
    return FavoriteStatusOut(
        requested=len(payload.items), unique=len(targets), items=result_items
    )


def _target_message_ids_for_conversation(
    db: Session,
    tenant_id: str,
    conversation_id: str,
    *,
    mode: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> set[int]:
    try:
        rows = _fetch_conversation_messages_compact(
            db,
            conversation_id,
            tenant_id,
            mode=mode,
            entity_id=entity_id,
            active_only=True,
        )
    except HTTPException as exc:
        raise ValueError("invalid_conversation") from exc
    return {int(row.id) for row in rows}


def list_favorites(
    db: Session,
    *,
    tenant_id: str,
    limit: int = 50,
    offset: int = 0,
    object_type: Optional[str] = None,
    conversation_id: Optional[str] = None,
    conversation_mode: Optional[str] = None,
    conversation_entity_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    contact_filter: Optional[str] = None,
    favorited_by: Optional[str] = None,
    favorited_by_name: Optional[str] = None,
    favorited_since: Optional[datetime] = None,
    favorited_until: Optional[datetime] = None,
    message_since_ms: Optional[int] = None,
    message_until_ms: Optional[int] = None,
    media_type: Optional[str] = None,
    staff_filter: Optional[str] = None,
) -> FavoritePageOut:
    """Return a stable server-paginated union of live message/media targets."""
    if favorited_since and favorited_until and favorited_since > favorited_until:
        raise ValueError("invalid_favorite_time_range")
    if message_since_ms is not None and message_until_ms is not None and message_since_ms > message_until_ms:
        raise ValueError("invalid_message_time_range")
    if media_type is not None and media_type not in {"image", "video", "voice", "file"}:
        raise ValueError("invalid_media_type")
    if media_type is not None and object_type == "message":
        raise ValueError("invalid_media_type")

    conversation_message_ids = None
    if conversation_id is not None:
        conversation_message_ids = _target_message_ids_for_conversation(
            db,
            tenant_id,
            conversation_id,
            mode=conversation_mode,
            entity_id=conversation_entity_id,
        )
        if not conversation_message_ids:
            return FavoritePageOut(items=[], total=0, limit=limit, offset=offset, has_more=False)

    common_conditions = [
        ArchiveFavorite.tenant_id == tenant_id,
        ArchiveFavorite.canceled_at.is_(None),
    ]
    if object_type:
        common_conditions.append(ArchiveFavorite.object_type == object_type)
    if favorited_by:
        common_conditions.append(
            ArchiveFavorite.favorited_by_admin_user_id == favorited_by
        )
    if favorited_by_name:
        actor_pattern = f"%{favorited_by_name.strip()}%"
        actor_ids_by_name = select(AdminUser.id).where(
            AdminUser.tenant_id == tenant_id,
            or_(AdminUser.name.ilike(actor_pattern), AdminUser.id.ilike(actor_pattern)),
        )
        common_conditions.append(
            ArchiveFavorite.favorited_by_admin_user_id.in_(actor_ids_by_name)
        )
    if favorited_since:
        common_conditions.append(ArchiveFavorite.favorited_at >= favorited_since)
    if favorited_until:
        common_conditions.append(ArchiveFavorite.favorited_at <= favorited_until)
    if message_since_ms is not None:
        common_conditions.append(ArchiveMessage.msgtime >= message_since_ms)
    if message_until_ms is not None:
        common_conditions.append(ArchiveMessage.msgtime <= message_until_ms)
    if contact_id:
        recipient_ids = select(ArchiveMessageRecipient.message_id).where(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.receiver_userid == contact_id,
        )
        common_conditions.append(
            or_(ArchiveMessage.sender == contact_id, ArchiveMessage.id.in_(recipient_ids))
        )
    if contact_filter:
        contact_pattern = f"%{contact_filter.strip()}%"
        external_contact_ids = select(ExternalContact.external_userid).where(
            ExternalContact.tenant_id == tenant_id,
            or_(
                ExternalContact.external_userid == contact_filter,
                ExternalContact.current_nickname_display.ilike(contact_pattern),
                ExternalContact.name.ilike(contact_pattern),
            ),
        )
        known_contact_ids = select(Contact.wecom_userid).where(
            Contact.tenant_id == tenant_id,
            or_(Contact.wecom_userid == contact_filter, Contact.name.ilike(contact_pattern)),
            ~Contact.wecom_userid.like("staff\\_%", escape="\\"),
            Contact.wecom_userid.notin_(
                select(AdminUser.wecom_user_id).where(AdminUser.tenant_id == tenant_id)
            ),
        )
        matching_contact_ids = union_all(external_contact_ids, known_contact_ids).subquery()
        recipient_ids = select(ArchiveMessageRecipient.message_id).where(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.receiver_userid.in_(select(matching_contact_ids.c[0])),
        )
        common_conditions.append(
            or_(
                ArchiveMessage.sender.in_(select(matching_contact_ids.c[0])),
                ArchiveMessage.id.in_(recipient_ids),
            )
        )
    if staff_filter:
        known_staff = staff_filter.startswith("staff_") or db.scalar(
            select(AdminUser.id).where(
                AdminUser.tenant_id == tenant_id,
                AdminUser.wecom_user_id == staff_filter,
            )
        ) is not None
        if not known_staff:
            return FavoritePageOut(items=[], total=0, limit=limit, offset=offset, has_more=False)
        recipient_ids = select(ArchiveMessageRecipient.message_id).where(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.receiver_userid == staff_filter,
        )
        common_conditions.append(
            or_(ArchiveMessage.sender == staff_filter, ArchiveMessage.id.in_(recipient_ids))
        )
    if conversation_message_ids is not None:
        common_conditions.append(ArchiveMessage.id.in_(conversation_message_ids))

    message_query = (
        select(
            ArchiveFavorite.id.label("favorite_id"),
            literal("message", type_=String(16)).label("object_type"),
            ArchiveMessage.msgid.label("object_id"),
            ArchiveFavorite.favorited_by_admin_user_id.label("favorited_by_admin_user_id"),
            ArchiveFavorite.favorited_at.label("favorited_at"),
            ArchiveFavorite.source_page.label("source_page"),
            ArchiveMessage.id.label("message_row_id"),
            ArchiveMessage.msgid.label("message_id"),
            ArchiveMessage.msgtime.label("message_time_ms"),
            ArchiveMessage.roomid.label("room_id"),
            ArchiveMessage.sender.label("sender_id"),
            ArchiveMessage.msgtype.label("message_type"),
            func.substr(ArchiveMessage.content_text, 1, _PREVIEW_LENGTH).label("preview"),
            literal(None, type_=Integer).label("media_file_id"),
            literal(None, type_=String(32)).label("media_type"),
            literal(None, type_=String(128)).label("media_mime_type"),
            literal(None, type_=Integer).label("media_size_bytes"),
            literal(None, type_=String(16)).label("media_download_status"),
        )
        .select_from(ArchiveFavorite)
        .join(
            ArchiveMessage,
            and_(
                ArchiveFavorite.archive_message_id == ArchiveMessage.id,
                ArchiveFavorite.tenant_id == ArchiveMessage.tenant_id,
            ),
        )
        .where(
            *common_conditions,
            ArchiveFavorite.object_type == "message",
            active_message_filter(),
        )
    )
    media_query = (
        select(
            ArchiveFavorite.id.label("favorite_id"),
            literal("media", type_=String(16)).label("object_type"),
            cast(MediaFile.id, String).label("object_id"),
            ArchiveFavorite.favorited_by_admin_user_id.label("favorited_by_admin_user_id"),
            ArchiveFavorite.favorited_at.label("favorited_at"),
            ArchiveFavorite.source_page.label("source_page"),
            ArchiveMessage.id.label("message_row_id"),
            ArchiveMessage.msgid.label("message_id"),
            ArchiveMessage.msgtime.label("message_time_ms"),
            ArchiveMessage.roomid.label("room_id"),
            ArchiveMessage.sender.label("sender_id"),
            ArchiveMessage.msgtype.label("message_type"),
            func.substr(ArchiveMessage.content_text, 1, _PREVIEW_LENGTH).label("preview"),
            MediaFile.id.label("media_file_id"),
            MediaFile.file_type.label("media_type"),
            MediaFile.mime_type.label("media_mime_type"),
            MediaFile.file_size.label("media_size_bytes"),
            MediaFile.download_status.label("media_download_status"),
        )
        .select_from(ArchiveFavorite)
        .join(
            MediaFile,
            and_(
                ArchiveFavorite.media_file_id == MediaFile.id,
                ArchiveFavorite.tenant_id == MediaFile.tenant_id,
            ),
        )
        .join(
            ArchiveMessage,
            and_(
                MediaFile.archive_message_id == ArchiveMessage.id,
                MediaFile.tenant_id == ArchiveMessage.tenant_id,
            ),
        )
        .where(
            *common_conditions,
            ArchiveFavorite.object_type == "media",
            MediaFile.tenant_id == tenant_id,
            ArchiveMessage.tenant_id == tenant_id,
            active_message_filter(),
        )
    )
    if media_type is not None:
        media_query = media_query.where(MediaFile.file_type == media_type)
    if object_type == "message":
        combined = message_query.subquery("favorite_items")
    elif object_type == "media" or media_type is not None:
        combined = media_query.subquery("favorite_items")
    else:
        combined = union_all(message_query, media_query).subquery("favorite_items")
    total = int(db.scalar(select(func.count()).select_from(combined)) or 0)
    rows = db.execute(
        select(combined)
        .order_by(combined.c.favorited_at.desc(), combined.c.favorite_id.asc())
        .offset(offset)
        .limit(limit)
    ).mappings().all()

    message_ids = [int(row["message_row_id"]) for row in rows]
    recipients_by_message = _load_recipients_map(db, tenant_id, message_ids)
    participant_ids = {
        participant
        for row in rows
        for participant in [row["sender_id"], *recipients_by_message.get(int(row["message_row_id"]), [])]
        if participant
    }
    staff_ids = _staff_ids_for_participants(db, tenant_id, participant_ids)
    person_names = {
        user_id: name
        for user_id, name in db.execute(
            select(Contact.wecom_userid, Contact.name).where(
                Contact.tenant_id == tenant_id,
                Contact.wecom_userid.in_(participant_ids),
            )
        )
    } if participant_ids else {}
    for user_id, name in db.execute(
        select(AdminUser.wecom_user_id, AdminUser.name).where(
            AdminUser.tenant_id == tenant_id,
            AdminUser.wecom_user_id.in_(participant_ids),
        )
    ) if participant_ids else []:
        if name:
            person_names.setdefault(user_id, name)
    external_names = {
        user_id: name
        for user_id, name in db.execute(
            select(
                ExternalContact.external_userid,
                func.coalesce(
                    ExternalContact.current_nickname_display,
                    ExternalContact.name,
                ),
            ).where(
                ExternalContact.tenant_id == tenant_id,
                ExternalContact.external_userid.in_(participant_ids),
            )
        )
    } if participant_ids else {}
    actor_ids = {row["favorited_by_admin_user_id"] for row in rows if row["favorited_by_admin_user_id"]}
    actor_names = {
        admin_id: name
        for admin_id, name in db.execute(
            select(AdminUser.id, AdminUser.name).where(
                AdminUser.tenant_id == tenant_id,
                AdminUser.id.in_(actor_ids),
            )
        )
    } if actor_ids else {}
    room_names = load_group_chat_display_names(
        db, tenant_id, (row["room_id"] for row in rows)
    )

    items: list[FavoriteItemOut] = []
    for row in rows:
        recipients = recipients_by_message.get(int(row["message_row_id"]), [])
        conversation_id, conversation_type, staff, contacts = _derive_conversation_membership(
            row["sender_id"],
            row["room_id"],
            recipients,
            lambda user_id: user_id in staff_ids,
        )
        staff_id = sorted(staff)[0] if staff else None
        contact_id = sorted(contacts)[0] if contacts else None
        staff_name = (
            resolve_person_display_name(staff_id, person_names.get(staff_id))
            if staff_id else None
        )
        contact_name = (
            resolve_person_display_name(
                contact_id,
                external_names.get(contact_id) or person_names.get(contact_id),
            )
            if contact_id else None
        )
        if conversation_type == "group":
            conversation_name = resolve_room_display_name(
                conversation_id, room_names.get(conversation_id)
            )
        elif staff_name and contact_name:
            conversation_name = f"{staff_name} ↔ {contact_name}"
        else:
            conversation_name = staff_name or contact_name or conversation_id
        focus_entity_type = "staff" if staff_id else ("contact" if contact_id else None)
        focus_entity_id = staff_id or contact_id
        sender_id = row["sender_id"]
        items.append(
            FavoriteItemOut(
                favorite_id=row["favorite_id"],
                object_type=row["object_type"],
                object_id=row["object_id"],
                favorited_by_admin_user_id=row["favorited_by_admin_user_id"],
                favorited_by_name=actor_names.get(row["favorited_by_admin_user_id"]),
                favorited_at=row["favorited_at"],
                source_page=row["source_page"],
                message_time_ms=row["message_time_ms"],
                message_id=row["message_id"],
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                conversation_type=conversation_type,
                staff_id=staff_id,
                staff_name=staff_name,
                contact_id=contact_id,
                contact_name=contact_name,
                focus_entity_type=focus_entity_type,
                focus_entity_id=focus_entity_id,
                sender_id=sender_id,
                sender_name=resolve_person_display_name(
                    sender_id,
                    external_names.get(sender_id) or person_names.get(sender_id),
                ) if sender_id else None,
                message_type=row["message_type"],
                preview=row["preview"],
                media_file_id=row["media_file_id"],
                media_type=row["media_type"],
                media_mime_type=row["media_mime_type"],
                media_size_bytes=row["media_size_bytes"],
                media_download_status=row["media_download_status"],
            )
        )
    return FavoritePageOut(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
    )
