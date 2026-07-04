"""
Tenant-scoped contact display-name upsert helper (RND-130).

Populates contacts.name from WeCom metadata (or any other source) without
ever overwriting an existing non-blank name with a blank one, so a manual
correction or an earlier successful sync is never clobbered by a later
failed/partial lookup.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Contact


def upsert_contact_display_name(
    session: Session, tenant_id: str, wecom_userid: str, name: Optional[str]
) -> Optional[Contact]:
    """Create or update the tenant-scoped Contact row for wecom_userid.

    - A blank/None name is a no-op: it never overwrites an existing name, and
      it never creates a new contact row (there is nothing useful to store).
    - Relies on the existing UNIQUE(tenant_id, wecom_userid) constraint;
      updated_at advances automatically (onupdate=func.now()) whenever the
      name attribute actually changes.
    - Does not commit — caller controls the transaction boundary.

    Returns the Contact row (existing or newly created), or None when name
    is blank and no contact exists yet for (tenant_id, wecom_userid).
    """
    clean_name = name.strip() if isinstance(name, str) else None

    contact = (
        session.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid == wecom_userid)
        .first()
    )

    if not clean_name:
        return contact

    if contact is None:
        contact = Contact(tenant_id=tenant_id, wecom_userid=wecom_userid, name=clean_name)
        session.add(contact)
    elif contact.name != clean_name:
        contact.name = clean_name

    return contact
