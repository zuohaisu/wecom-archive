"""Tenant-scoped WeCom external-contact synchronization service (RND-287/RND-170)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import exists, func, or_, select
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
from app.log_safety import configure_secret_safe_logging
from app.services.avatar_sync import (
    reconcile_internal_contact_avatars,
    sync_external_contact_avatar,
)
from app.services.external_contact_identity import (
    IdentitySyncResult,
    safe_display_nickname,
    sync_external_contact_identity,
)
from app.services.tenant_credentials import (
    TenantCredentialError,
    active_tenant_configs,
    active_tenant_ids,
    credentials_for_active_config,
    tenant_log_tag,
)

logger = logging.getLogger(__name__)


# Each batch is used in two ``IN`` clauses. Keep it comfortably below common
# SQLite parameter limits while still avoiding one archive scan per contact.
_INTERACTION_STATS_BATCH_SIZE = 300
_DEFAULT_RECONCILE_TENANT_LIMIT = 25


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


def _interaction_stats_for_external_contacts(
    session: Session, tenant_id: str, external_userids: Iterable[str]
) -> dict[str, tuple[Optional[datetime], Optional[int]]]:
    """Return tenant-scoped interaction stats for a full contact-sync batch.

    A full directory sync can include many contacts. Calculating the previous
    single-contact query for every record repeats a scan of the archive table
    for each contact. This performs one aggregate query per bounded batch
    instead. ``UNION`` (rather than ``UNION ALL``) preserves the single-contact
    query's OR semantics when a contact is both sender and recipient of one
    message.
    """
    normalized_ids = sorted(
        {cleaned for value in external_userids if (cleaned := _clean(value))}
    )
    stats: dict[str, tuple[Optional[datetime], Optional[int]]] = {}
    for offset in range(0, len(normalized_ids), _INTERACTION_STATS_BATCH_SIZE):
        batch = normalized_ids[offset : offset + _INTERACTION_STATS_BATCH_SIZE]
        sent = select(
            ArchiveMessage.sender.label("external_userid"),
            ArchiveMessage.id.label("message_id"),
            ArchiveMessage.created_at.label("created_at"),
        ).where(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.sender.in_(batch),
        )
        received = (
            select(
                ArchiveMessageRecipient.receiver_userid.label("external_userid"),
                ArchiveMessage.id.label("message_id"),
                ArchiveMessage.created_at.label("created_at"),
            )
            .join(
                ArchiveMessage,
                ArchiveMessage.id == ArchiveMessageRecipient.message_id,
            )
            .where(
                ArchiveMessage.tenant_id == tenant_id,
                ArchiveMessageRecipient.tenant_id == tenant_id,
                ArchiveMessageRecipient.receiver_userid.in_(batch),
            )
        )
        matching_messages = sent.union(received).subquery()
        rows = session.execute(
            select(
                matching_messages.c.external_userid,
                func.max(matching_messages.c.created_at),
                func.count(matching_messages.c.message_id),
            ).group_by(matching_messages.c.external_userid)
        )
        for external_userid, last_interaction_at, message_count in rows:
            stats[external_userid] = (last_interaction_at, int(message_count))
    return stats


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
    interaction_stats: Optional[tuple[Optional[datetime], Optional[int]]] = None,
    *,
    update_interaction_stats: bool = True,
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
    if update_interaction_stats and interaction_stats is None:
        interaction_stats = _interaction_stats(session, tenant_id, external_userid)
    if interaction_stats is None:
        interaction_stats = (None, None)
    last_interaction_at, message_count = interaction_stats
    contact = upsert_external_contact(
        session,
        tenant_id,
        external_userid,
        last_interaction_at=last_interaction_at,
        message_count=message_count,
        update_interaction_stats=update_interaction_stats,
        **values,
    )
    identity_result = sync_external_contact_identity(session, contact, detail)
    # Avatar failure is fully contained by sync_external_contact_avatar(): it
    # changes only this ancillary cache state and never invalidates identity,
    # remarks, or the surrounding external-contact transaction.
    sync_external_contact_avatar(contact, tenant_id, detail)
    return existed, identity_result


def refresh_external_contact(
    session: Session,
    tenant_id: str,
    corp_id: str,
    external_secret: str,
    external_userid: str,
    *,
    update_interaction_stats: bool = False,
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
            update_interaction_stats=update_interaction_stats,
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
    *,
    commit_every: Optional[int] = None,
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

    interaction_stats_by_external_userid = _interaction_stats_for_external_contacts(
        session, tenant_id, external_userids
    )
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
                    interaction_stats_by_external_userid.get(
                        external_userid, (None, None)
                    ),
                )
            if existed:
                summary.updated += 1
            else:
                summary.inserted += 1
            summary.nickname_changes += int(identity_result.nickname_changed)
            summary.follow_relationship_changes += identity_result.follow_relations_changed
        except Exception:  # noqa: BLE001 -- never log raw response/contact data
            summary.failed += 1

        if commit_every and summary.total % commit_every == 0:
            # Full reconciliation is intentionally resumable: each contact is
            # idempotent, so a long first directory import should expose prior
            # batches rather than hold one large transaction until the end.
            session.commit()

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


@dataclass
class ReconciliationSummary:
    """Aggregate-only outcomes for independent tenant reconciliation runs."""

    tenant_count: int = 0
    tenant_deferred: int = 0
    tenant_completed: int = 0
    tenant_failed: int = 0
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    nickname_changes: int = 0
    follow_relationship_changes: int = 0
    internal_avatar_selected: int = 0
    internal_avatar_ready: int = 0
    internal_avatar_unavailable: int = 0


def _positive_int(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _bounded_tenant_ids(active_ids: list[str], limit: int, at: datetime | None = None) -> list[str]:
    """Return a daily-rotated bounded tenant slice without a persistent cursor."""
    if len(active_ids) <= limit:
        return active_ids
    day = (at or datetime.now(timezone.utc)).date().toordinal()
    offset = day % len(active_ids)
    rotated = active_ids[offset:] + active_ids[:offset]
    return rotated[:limit]


def reconcile_active_tenants(
    session: Session,
    external_secret: str,
    oauth_secret: str,
    *,
    tenant_limit: int = _DEFAULT_RECONCILE_TENANT_LIMIT,
    at: datetime | None = None,
) -> ReconciliationSummary:
    """Reconcile a bounded, daily-rotated active-tenant slice.

    ``WECOM_EXTERNAL_CONTACT_SECRET`` and ``WECOM_OAUTH_SECRET`` remain
    separate integration configuration. They are never used to choose a
    tenant. The CorpID passed to WeCom is read only from that tenant's active
    encrypted TenantWecomConfig, and a failed tenant is rolled back before
    the next tenant begins. The rotation avoids starving later tenants while
    keeping each daily run bounded without adding a second scheduling state.
    """
    aggregate = ReconciliationSummary()
    active_ids = active_tenant_ids(session)
    selected_ids = _bounded_tenant_ids(active_ids, max(1, tenant_limit), at)
    aggregate.tenant_count = len(selected_ids)
    aggregate.tenant_deferred = len(active_ids) - len(selected_ids)
    selected_id_set = set(selected_ids)
    configs_by_tenant_id = {
        config.tenant_id: config
        for config in active_tenant_configs(session)
        if config.tenant_id in selected_id_set
    }
    configured_ids = set(configs_by_tenant_id)
    for missing_tenant_id in selected_ids:
        if missing_tenant_id in configured_ids:
            continue
        aggregate.tenant_failed += 1
        logger.warning(
            "external_contact_reconcile tenant=%s result=failed "
            "error_class=tenant_config_unavailable",
            tenant_log_tag(missing_tenant_id),
        )

    for tenant_id in selected_ids:
        config = configs_by_tenant_id.get(tenant_id)
        if config is None:
            continue
        tag = tenant_log_tag(config.tenant_id)
        try:
            # This is a fail-closed configuration authority check. The
            # external-contact API does not receive the archive secret, but
            # a tenant whose encrypted runtime config cannot be read must not
            # be selected through another tenant or a global CorpID.
            credentials = credentials_for_active_config(config)
        except TenantCredentialError as exc:
            session.rollback()
            aggregate.tenant_failed += 1
            logger.warning(
                "external_contact_reconcile tenant=%s result=failed error_class=%s",
                tag,
                exc.error_class,
            )
            continue

        try:
            summary = (
                sync_external_contacts(
                    session,
                    credentials.tenant_id,
                    credentials.corp_id,
                    external_secret,
                    commit_every=100,
                )
                if external_secret
                else RunSummary()
            )
            # The daily reconciliation remains the bounded fallback for
            # internal archive-seat avatars. It runs after the tenant-scoped
            # contact pass and uses the same tenant CorpID.
            internal_avatars = reconcile_internal_contact_avatars(
                session,
                credentials.tenant_id,
                credentials.corp_id,
                oauth_secret,
            )
            session.commit()
        except Exception:  # noqa: BLE001 -- one tenant must not affect another
            session.rollback()
            aggregate.tenant_failed += 1
            logger.warning(
                "external_contact_reconcile tenant=%s result=failed "
                "error_class=tenant_processing_failed",
                tag,
            )
            continue

        aggregate.tenant_completed += 1
        aggregate.total += summary.total
        aggregate.inserted += summary.inserted
        aggregate.updated += summary.updated
        aggregate.skipped += summary.skipped
        aggregate.failed += summary.failed
        aggregate.nickname_changes += summary.nickname_changes
        aggregate.follow_relationship_changes += summary.follow_relationship_changes
        aggregate.internal_avatar_selected += internal_avatars.selected
        aggregate.internal_avatar_ready += internal_avatars.ready
        aggregate.internal_avatar_unavailable += internal_avatars.unavailable

    return aggregate


def main() -> int:
    """Run the idempotent daily reconciliation for every active tenant."""
    configure_secret_safe_logging()
    logging.basicConfig(level=logging.INFO)
    try:
        database_url = _require_env("DATABASE_URL")
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    external_secret = os.environ.get("WECOM_EXTERNAL_CONTACT_SECRET", "").strip()
    oauth_secret = os.environ.get("WECOM_OAUTH_SECRET", "").strip()
    tenant_limit = _positive_int(
        os.environ.get("EXTERNAL_CONTACT_RECONCILE_TENANT_LIMIT", ""),
        _DEFAULT_RECONCILE_TENANT_LIMIT,
    )

    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            summary = reconcile_active_tenants(
                session,
                external_secret,
                oauth_secret,
                tenant_limit=tenant_limit,
            )
    except Exception as exc:
        logger.error("External-contact sync failed: %s", type(exc).__name__)
        return 1

    logger.info(
        "external_contact_reconcile status=completed tenant_count=%d "
        "tenant_deferred=%d tenant_completed=%d tenant_failed=%d total=%d inserted=%d updated=%d "
        "skipped=%d failed=%d nickname_changes=%d follow_relationship_changes=%d "
        "internal_avatar_selected=%d internal_avatar_ready=%d "
        "internal_avatar_unavailable=%d external_contact_configured=%s",
        summary.tenant_count,
        summary.tenant_deferred,
        summary.tenant_completed,
        summary.tenant_failed,
        summary.total,
        summary.inserted,
        summary.updated,
        summary.skipped,
        summary.failed,
        summary.nickname_changes,
        summary.follow_relationship_changes,
        summary.internal_avatar_selected,
        summary.internal_avatar_ready,
        summary.internal_avatar_unavailable,
        "true" if external_secret else "false",
    )
    return 1 if summary.tenant_count and not summary.tenant_completed else 0


if __name__ == "__main__":
    sys.exit(main())
