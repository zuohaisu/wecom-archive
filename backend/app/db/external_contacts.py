"""Tenant-scoped persistence helpers for WeCom external contacts."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import ExternalContact


def upsert_external_contact(
    session: Session,
    tenant_id: str,
    external_userid: str,
    name: Optional[str],
    company: Optional[str],
    tags_json: Optional[str],
    source: Optional[str],
    owner_wecom_userid: Optional[str],
    last_interaction_at: Optional[datetime],
    message_count: Optional[int],
) -> ExternalContact:
    """Create or update an external contact scoped to one tenant.

    A partial API response must not replace a usable existing name with an
    empty value. The caller owns the transaction and commits separately.
    """
    contact = (
        session.query(ExternalContact)
        .filter(
            ExternalContact.tenant_id == tenant_id,
            ExternalContact.external_userid == external_userid,
        )
        .first()
    )
    clean_name = name.strip() if isinstance(name, str) and name.strip() else None

    if contact is None:
        contact = ExternalContact(
            tenant_id=tenant_id,
            external_userid=external_userid,
            name=clean_name,
            company=company,
            tags=tags_json,
            source=source,
            owner_wecom_userid=owner_wecom_userid,
            last_interaction_at=last_interaction_at,
            message_count=message_count,
        )
        session.add(contact)
        return contact

    if clean_name:
        contact.name = clean_name
    contact.company = company
    contact.tags = tags_json
    contact.source = source
    contact.owner_wecom_userid = owner_wecom_userid
    contact.last_interaction_at = last_interaction_at
    contact.message_count = message_count
    # Refresh timestamp even when the upstream payload is unchanged; a repeat
    # run is still a successful observation of this external contact.
    contact.updated_at = func.now()
    return contact
