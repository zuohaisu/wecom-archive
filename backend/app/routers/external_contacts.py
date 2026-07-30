"""Tenant-scoped external-contact list and filters (RND-288)."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import AdminUser, Contact, ExternalContact
from app.db.session import get_db
from app.display_names import resolve_person_display_name
from app.schemas.external_contact import ExternalContactListPage

router = APIRouter()


def _tag_like_pattern(tag: str) -> str:
    """Match one JSON-serialized tag value, not a substring of another tag."""
    encoded_tag = json.dumps(tag, ensure_ascii=False)
    escaped_tag = (
        encoded_tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped_tag}%"


@router.get("/external-contacts", response_model=ExternalContactListPage)
def list_external_contacts(
    company: Optional[str] = Query(None),
    tags: Optional[str] = Query(None),
    owner_wecom_userid: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=200),
    db: Session = Depends(get_db),
    auth: tuple[AdminUser, str] = Depends(require_role()),
) -> ExternalContactListPage:
    """Return external contacts belonging only to the authenticated tenant."""
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

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    contacts = db.scalars(
        statement.order_by(
            ExternalContact.last_interaction_at.desc().nullslast(), ExternalContact.id.asc()
        )
        .offset(offset)
        .limit(limit)
    ).all()

    owner_ids = {contact.owner_wecom_userid for contact in contacts if contact.owner_wecom_userid}
    owner_names = (
        {
            owner_id: name
            for owner_id, name in db.execute(
                select(Contact.wecom_userid, Contact.name).where(
                    Contact.tenant_id == tenant_id,
                    Contact.wecom_userid.in_(owner_ids),
                )
            )
        }
        if owner_ids
        else {}
    )

    return ExternalContactListPage(
        items=[
            {
                "id": contact.id,
                "external_userid": contact.external_userid,
                "name": contact.name,
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
            for contact in contacts
        ],
        total=total,
        has_more=offset + len(contacts) < total,
    )


def _decode_tags(tags: Optional[str]) -> list[str]:
    """Return only the valid string tag values stored by the sync worker."""
    try:
        values = json.loads(tags) if tags else []
    except (TypeError, json.JSONDecodeError):
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []
