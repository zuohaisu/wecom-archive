"""Tenant-scoped WeCom external-contact synchronization service (RND-287)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session

from app import wecom_contacts
from app.auth import get_wecom_token
from app.db.external_contacts import upsert_external_contact
from app.db.models import (
    ArchiveMessage,
    ArchiveMessageRecipient,
    ExternalContact,
    TenantWecomConfig,
)

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0


def _clean(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _interaction_stats(
    session: Session, tenant_id: str, external_userid: str
) -> tuple[Optional[datetime], Optional[int]]:
    """Return archive-derived interaction stats, or empty values when absent."""
    recipient_match = exists().where(
        ArchiveMessageRecipient.message_id == ArchiveMessage.id,
        ArchiveMessageRecipient.tenant_id == tenant_id,
        ArchiveMessageRecipient.receiver_userid == external_userid,
    )
    count, last_interaction_at = (
        session.query(func.count(ArchiveMessage.id), func.max(ArchiveMessage.created_at))
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            or_(ArchiveMessage.sender == external_userid, recipient_match),
        )
        .one()
    )
    return (last_interaction_at, int(count)) if count else (None, None)


def _tag_ids(follow_user: dict) -> list[str]:
    result: list[str] = []
    for tag in follow_user.get("tags", []) or []:
        if isinstance(tag, str):
            tag_id = _clean(tag)
        elif isinstance(tag, dict):
            tag_id = _clean(tag.get("id") or tag.get("tag_id"))
        else:
            tag_id = None
        if tag_id:
            result.append(tag_id)
    return result


def _payload_values(detail: dict, tag_names: dict[str, str]) -> dict:
    """Normalize a WeCom detail response without retaining its raw PII payload."""
    external = detail.get("external_contact") or {}
    follows = detail.get("follow_user") or []
    follows = [item for item in follows if isinstance(item, dict)]
    primary = follows[0] if follows else {}

    name = next((_clean(item.get("remark")) for item in follows if _clean(item.get("remark"))), None)
    name = name or _clean(external.get("name"))
    tag_values = [tag_names[tag_id] for tag_id in _tag_ids(primary) if tag_id in tag_names]

    return {
        "name": name,
        "company": _clean(external.get("corp_name")) or _clean(external.get("corp_full_name")),
        "tags_json": json.dumps(tag_values, ensure_ascii=False),
        "source": _clean(primary.get("state")),
        "owner_wecom_userid": _clean(primary.get("userid")),
    }


def sync_external_contacts(
    session: Session,
    tenant_id: str,
    corp_id: str,
    external_secret: str,
) -> RunSummary:
    """Synchronize one tenant; callers provide credentials and transaction scope."""
    summary = RunSummary()
    token = get_wecom_token(
        corp_id, external_secret, cache_key=f"{corp_id}:external_contact"
    )
    owners = wecom_contacts.list_follow_userids(token)
    if owners is None:
        summary.failed += 1
        return summary

    tag_names = wecom_contacts.get_corp_tag_list(token) or {}
    external_userids: set[str] = set()
    for owner in owners:
        ids = wecom_contacts.list_external_userids_by_user(token, owner)
        if ids is None:
            summary.failed += 1
            continue
        external_userids.update(ids)

    for external_userid in sorted(external_userids):
        summary.total += 1
        detail = wecom_contacts.get_external_contact(token, external_userid)
        if detail is None:
            summary.failed += 1
            continue
        try:
            existed = (
                session.query(ExternalContact.id)
                .filter(
                    ExternalContact.tenant_id == tenant_id,
                    ExternalContact.external_userid == external_userid,
                )
                .first()
                is not None
            )
            values = _payload_values(detail, tag_names)
            last_interaction_at, message_count = _interaction_stats(
                session, tenant_id, external_userid
            )
            upsert_external_contact(
                session,
                tenant_id,
                external_userid,
                last_interaction_at=last_interaction_at,
                message_count=message_count,
                **values,
            )
            if existed:
                summary.updated += 1
            else:
                summary.inserted += 1
        except Exception:
            # A malformed individual record must not prevent other contacts
            # from syncing; response bodies and identifiers are never logged.
            summary.failed += 1

    return summary


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not set or empty: {name}")
    return value


def _require_tenant_id(session: Session, corp_id: str) -> str:
    row = (
        session.query(TenantWecomConfig)
        .filter(
            TenantWecomConfig.corp_id == corp_id,
            TenantWecomConfig.is_active == True,  # noqa: E712
        )
        .first()
    )
    if row is None:
        raise RuntimeError("No active tenant found for this corp")
    return row.tenant_id


def main() -> int:
    """Run the manual module CLI; scheduled invocation remains out of scope."""
    logging.basicConfig(level=logging.INFO)
    try:
        database_url = _require_env("DATABASE_URL")
        corp_id = _require_env("WECOM_CORP_ID")
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    external_secret = os.environ.get("WECOM_EXTERNAL_CONTACT_SECRET", "").strip()
    if not external_secret:
        logger.info("External-contact API is not enabled; sync skipped")
        return 0

    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            tenant_id = _require_tenant_id(session, corp_id)
            summary = sync_external_contacts(session, tenant_id, corp_id, external_secret)
            session.commit()
    except Exception as exc:
        logger.error("External-contact sync failed: %s", type(exc).__name__)
        return 1

    logger.info(
        "External-contact sync complete: total=%d inserted=%d updated=%d skipped=%d failed=%d",
        summary.total,
        summary.inserted,
        summary.updated,
        summary.skipped,
        summary.failed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
