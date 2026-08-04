"""Tenant-scoped WeCom external-contact synchronization service (RND-287/RND-170)."""

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
from app.services.external_contact_identity import (
    IdentitySyncResult,
    safe_display_nickname,
    sync_external_contact_identity,
)

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    nickname_changes: int = 0
    follow_relationship_changes: int = 0


@dataclass(frozen=True)
class RefreshResult:
    """Safe per-contact refresh outcome; it never exposes contact PII."""

    found: bool
    inserted: bool = False
    nickname_changed: bool = False
    follow_relationship_changes: int = 0


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
    """Extract RND-287 compatibility/profile values without conflating identity.

    ``name`` remains a one-way legacy display seed: old consumers can retain
    their existing remark-first behavior while RND-170 stores the current
    real nickname and every employee remark in dedicated fields.
    """
    external = detail.get("external_contact") or {}
    follows = detail.get("follow_user") or []
    follows = [item for item in follows if isinstance(item, dict)]
    primary = follows[0] if follows else {}

    legacy_name = next(
        (safe_display_nickname(item.get("remark")) for item in follows if safe_display_nickname(item.get("remark"))),
        None,
    )
    legacy_name = legacy_name or safe_display_nickname(external.get("name"))
    tag_values = [tag_names[tag_id] for tag_id in _tag_ids(primary) if tag_id in tag_names]

    return {
        "name": legacy_name,
        "company": _clean(external.get("corp_name")) or _clean(external.get("corp_full_name")),
        "tags_json": json.dumps(tag_values, ensure_ascii=False),
        "source": _clean(primary.get("state")),
        "owner_wecom_userid": _clean(primary.get("userid")),
    }


def _persist_external_contact_detail(
    session: Session,
    tenant_id: str,
    external_userid: str,
    detail: dict,
    tag_names: dict[str, str],
) -> tuple[bool, IdentitySyncResult]:
    """Write one full detail payload in the caller's transaction/savepoint."""
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
    contact = upsert_external_contact(
        session,
        tenant_id,
        external_userid,
        last_interaction_at=last_interaction_at,
        message_count=message_count,
        **values,
    )
    identity_result = sync_external_contact_identity(session, contact, detail)
    return existed, identity_result


def refresh_external_contact(
    session: Session,
    tenant_id: str,
    corp_id: str,
    external_secret: str,
    external_userid: str,
) -> RefreshResult:
    """Fetch and persist one callback-targeted external contact.

    The caller supplies the tenant and transaction boundary. Failures are
    represented as ``found=False`` for a missing/unavailable detail payload;
    credential/network errors still raise so a caller can record a safe
    aggregate failure without mistaking it for a successful refresh.
    """
    clean_external_userid = _clean(external_userid)
    if not clean_external_userid:
        return RefreshResult(found=False)

    token = get_wecom_token(
        corp_id, external_secret, cache_key=f"{corp_id}:external_contact"
    )
    detail = wecom_contacts.get_external_contact(token, clean_external_userid)
    if not isinstance(detail, dict):
        return RefreshResult(found=False)
    tag_names = wecom_contacts.get_corp_tag_list(token) or {}

    with session.begin_nested():
        existed, identity_result = _persist_external_contact_detail(
            session,
            tenant_id,
            clean_external_userid,
            detail,
            tag_names,
        )
    return RefreshResult(
        found=True,
        inserted=not existed,
        nickname_changed=identity_result.nickname_changed,
        follow_relationship_changes=identity_result.follow_relations_changed,
    )


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
        if not isinstance(detail, dict):
            summary.failed += 1
            continue
        try:
            # One malformed record must not invalidate other contacts or leave
            # a PostgreSQL transaction unusable for the rest of this run.
            with session.begin_nested():
                existed, identity_result = _persist_external_contact_detail(
                    session,
                    tenant_id,
                    external_userid,
                    detail,
                    tag_names,
                )
            if existed:
                summary.updated += 1
            else:
                summary.inserted += 1
            summary.nickname_changes += int(identity_result.nickname_changed)
            summary.follow_relationship_changes += identity_result.follow_relations_changed
        except Exception:  # noqa: BLE001 -- never log raw response/contact data
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
    """Run the idempotent full refresh used for backfill/reconciliation."""
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
        "External-contact sync complete: total=%d inserted=%d updated=%d skipped=%d "
        "failed=%d nickname_changes=%d follow_relationship_changes=%d",
        summary.total,
        summary.inserted,
        summary.updated,
        summary.skipped,
        summary.failed,
        summary.nickname_changes,
        summary.follow_relationship_changes,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
