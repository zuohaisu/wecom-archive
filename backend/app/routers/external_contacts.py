"""Tenant-scoped external-contact identity list/detail APIs."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import (
    AdminUser,
    Contact,
    ExternalContact,
    ExternalContactFollow,
    ExternalContactNicknameHistory,
)
from app.db.session import get_db
from app.display_names import resolve_person_display_name
from app.schemas.external_contact import ExternalContactDetail, ExternalContactListPage
from app.services.external_contact_identity import (
    external_contact_search_predicate,
    load_external_contact_search_matches,
    normalized_search_term,
    resolve_external_contact_display_name,
    safe_display_nickname,
)
from app.services.listing_service import list_conversations

router = APIRouter()


def _tag_like_pattern(tag: str) -> str:
    """Match one JSON-serialized tag value, not a substring of another tag."""
    encoded_tag = json.dumps(tag, ensure_ascii=False)
    escaped_tag = (
        encoded_tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped_tag}%"


def _decode_tags(tags: Optional[str]) -> list[str]:
    """Return only the valid string tag values stored by the sync worker."""
    try:
        values = json.loads(tags) if tags else []
    except (TypeError, json.JSONDecodeError):
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _load_follow_remarks(
    db: Session, tenant_id: str, external_userids: set[str]
) -> dict[str, list[dict]]:
    if not external_userids:
        return {}
    rows = db.scalars(
        select(ExternalContactFollow).where(
            ExternalContactFollow.tenant_id == tenant_id,
            ExternalContactFollow.external_userid.in_(external_userids),
        )
    ).all()
    follow_userids = {row.follow_userid for row in rows}
    names = (
        {
            userid: name
            for userid, name in db.execute(
                select(Contact.wecom_userid, Contact.name).where(
                    Contact.tenant_id == tenant_id,
                    Contact.wecom_userid.in_(follow_userids),
                )
            )
        }
        if follow_userids
        else {}
    )
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row.external_userid, []).append(
            {
                "follow_userid": row.follow_userid,
                "follow_user_display_name": resolve_person_display_name(
                    row.follow_userid, names.get(row.follow_userid)
                ),
                "remark": safe_display_nickname(row.remark_normalized),
                "is_active": row.is_active,
                "observed_at": row.observed_at,
            }
        )
    for items in result.values():
        items.sort(key=lambda item: (not item["is_active"], item["follow_userid"]))
    return result


def _current_nickname(contact: ExternalContact) -> Optional[str]:
    return safe_display_nickname(
        contact.current_nickname_display or contact.current_nickname_raw
    )


def _display_name(contact: ExternalContact) -> str:
    # List/detail endpoints have no one employee's session context. A local
    # remark therefore cannot become their tenant-global title; expose the
    # customer-level nickname and return individual remarks separately.
    return resolve_external_contact_display_name(
        contact.external_userid,
        current_nickname=_current_nickname(contact),
    )


def _item_payload(
    contact: ExternalContact,
    follow_remarks: list[dict],
    search_matches: list[dict],
    owner_names: dict[str, Optional[str]],
) -> dict:
    return {
        "id": contact.id,
        "external_userid": contact.external_userid,
        "name": contact.name,
        "display_name": _display_name(contact),
        "current_nickname": _current_nickname(contact),
        "follow_remarks": follow_remarks,
        "search_matches": search_matches,
        "company": contact.company,
        "tags": _decode_tags(contact.tags),
        "source": contact.source,
        "owner_wecom_userid": contact.owner_wecom_userid,
        "owner_display_name": resolve_person_display_name(
            contact.owner_wecom_userid,
            owner_names.get(contact.owner_wecom_userid),
        ),
        "last_interaction_at": contact.last_interaction_at,
        "message_count": contact.message_count,
    }


def _owner_names(
    db: Session, tenant_id: str, contacts: list[ExternalContact]
) -> dict[str, Optional[str]]:
    owner_ids = {
        contact.owner_wecom_userid for contact in contacts if contact.owner_wecom_userid
    }
    if not owner_ids:
        return {}
    return {
        owner_id: name
        for owner_id, name in db.execute(
            select(Contact.wecom_userid, Contact.name).where(
                Contact.tenant_id == tenant_id,
                Contact.wecom_userid.in_(owner_ids),
            )
        )
    }


@router.get("/external-contacts", response_model=ExternalContactListPage)
def list_external_contacts(
    company: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    owner_wecom_userid: Optional[str] = Query(None),
    q: Optional[str] = Query(None, min_length=1, description="Match employee remark, current nickname, or nickname history"),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=200),
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(require_role()),
) -> ExternalContactListPage:
    """Return tenant-isolated identities for RND-341 and legacy list consumers."""
    _, tenant_id = auth
    statement = select(ExternalContact).where(ExternalContact.tenant_id == tenant_id)
    if company:
        statement = statement.where(ExternalContact.company.ilike(f"%{company}%"))
    if tags:
        statement = statement.where(
            ExternalContact.tags.like(_tag_like_pattern(tags), escape="\\")
        )
    if owner_wecom_userid:
        statement = statement.where(
            ExternalContact.owner_wecom_userid == owner_wecom_userid
        )
    if q is not None:
        statement = statement.where(external_contact_search_predicate(tenant_id, q))

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    contacts = db.scalars(
        statement.order_by(
            ExternalContact.last_interaction_at.desc().nullslast(), ExternalContact.id.asc()
        )
        .offset(offset)
        .limit(limit)
    ).all()
    external_userids = {contact.external_userid for contact in contacts}
    follow_remarks = _load_follow_remarks(db, tenant_id, external_userids)
    search_matches = (
        load_external_contact_search_matches(
            db,
            tenant_id,
            q,
            external_userids=external_userids,
            limit=max(limit, 1),
        )
        if normalized_search_term(q) is not None
        else {}
    )
    owner_names = _owner_names(db, tenant_id, contacts)

    return ExternalContactListPage(
        items=[
            _item_payload(
                contact,
                follow_remarks.get(contact.external_userid, []),
                search_matches.get(contact.external_userid, []),
                owner_names,
            )
            for contact in contacts
        ],
        total=total,
        has_more=offset + len(contacts) < total,
    )


@router.get("/external-contacts/{external_userid}", response_model=ExternalContactDetail)
def get_external_contact_detail(
    external_userid: str,
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(require_role()),
) -> ExternalContactDetail:
    """Return one tenant-scoped identity, employee remarks, history, and sessions."""
    _, tenant_id = auth
    contact = db.scalar(
        select(ExternalContact).where(
            ExternalContact.tenant_id == tenant_id,
            ExternalContact.external_userid == external_userid,
        )
    )
    if contact is None:
        raise HTTPException(status_code=404, detail="External contact not found")

    follow_remarks = _load_follow_remarks(db, tenant_id, {contact.external_userid})
    owner_names = _owner_names(db, tenant_id, [contact])
    history = db.scalars(
        select(ExternalContactNicknameHistory)
        .where(
            ExternalContactNicknameHistory.tenant_id == tenant_id,
            ExternalContactNicknameHistory.external_userid == contact.external_userid,
        )
        .order_by(
            ExternalContactNicknameHistory.observed_at.asc(),
            ExternalContactNicknameHistory.id.asc(),
        )
    ).all()
    payload = _item_payload(
        contact,
        follow_remarks.get(contact.external_userid, []),
        [],
        owner_names,
    )
    return ExternalContactDetail(
        **payload,
        nickname_history=[
            {
                "old_nickname": safe_display_nickname(
                    item.old_nickname_display or item.old_nickname_raw
                ),
                "new_nickname": safe_display_nickname(
                    item.new_nickname_display or item.new_nickname_raw
                ),
                "observed_at": item.observed_at,
            }
            for item in history
        ],
        conversations=list_conversations(db, tenant_id, entity_id=external_userid),
    )
