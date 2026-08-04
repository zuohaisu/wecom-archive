"""Tenant-scoped persistence helpers for WeCom external contacts."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

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

    ``ExternalContact.name`` is a legacy compatibility label from RND-287,
    not the customer's canonical nickname. It may contain one employee's
    remark, so an existing non-blank value is never overwritten here. The
    RND-170 identity fields and per-employee remarks are maintained by the
    external-contact identity service. The caller owns the transaction.
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

    # Preserve an existing legacy label. Replacing it with a different
    # follow user's remark (or a real nickname) would globalize one employee's
    # local naming decision and regress existing conversation titles.
    if clean_name and not (isinstance(contact.name, str) and contact.name.strip()):
        contact.name = clean_name
    if contact.company != company:
        contact.company = company
    if contact.tags != tags_json:
        contact.tags = tags_json
    if contact.source != source:
        contact.source = source
    if contact.owner_wecom_userid != owner_wecom_userid:
        contact.owner_wecom_userid = owner_wecom_userid
    if contact.last_interaction_at != last_interaction_at:
        contact.last_interaction_at = last_interaction_at
    if contact.message_count != message_count:
        contact.message_count = message_count
    return contact
