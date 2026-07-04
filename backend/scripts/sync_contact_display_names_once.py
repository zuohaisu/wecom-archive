#!/usr/bin/env python3
"""
One-shot contact display-name enrichment for RND-130.

Collects every distinct participant ID seen in the tenant's archive (message
senders and recipients — this naturally includes monitored accounts, since
they appear as senders/recipients like any other participant), looks up a
human-readable name via WeCom metadata APIs, and upserts contacts.name.

Idempotent — re-running only fills gaps: any contact that already has a
non-blank name is skipped, and upsert_contact_display_name() never
overwrites a non-blank name with a blank one.

Usage (from backend/):
    python scripts/sync_contact_display_names_once.py

Required environment variables:
    DATABASE_URL          PostgreSQL connection string
    WECOM_CORP_ID         WeCom corporation ID
    WECOM_OAUTH_SECRET    WeCom app secret used for user/get (internal members)

Optional environment variables:
    WECOM_EXTERNAL_CONTACT_SECRET
        WeCom external-contact secret. When set, IDs not resolved as internal
        members are looked up via externalcontact/get. When unset, external
        lookups are skipped entirely (internal-only enrichment).

Exit codes:
    0  Success (individual unresolved IDs are not a failure)
    1  Fatal initialisation failure (env, DB, or access_token)

Safety constraints:
    - Never logs access_token, corp_id/secret, or any wecom_userid.
    - Never touches archive_messages / archive_message_recipients — reads
      only, for ID collection.
    - Only aggregate counts are printed.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import get_wecom_token
from app.db.contacts import upsert_contact_display_name
from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact, TenantWecomConfig
from app.wecom_contacts import fetch_external_contact_display_name, fetch_member_display_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _require_tenant_id(session: Session, corp_id: str) -> str:
    """Return the active tenant_id for *corp_id*, or exit 1.

    Mirrors sync_wecom_archive_once._require_tenant_id: sync must never
    proceed without a valid tenant.
    """
    try:
        row = (
            session.query(TenantWecomConfig)
            .filter(
                TenantWecomConfig.corp_id == corp_id,
                TenantWecomConfig.is_active == True,  # noqa: E712
            )
            .first()
        )
    except Exception as exc:
        print(
            f"[FAIL] Tenant resolution DB error ({type(exc).__name__}). "
            "Run alembic upgrade head and bootstrap_default_tenant.py first.",
            flush=True,
        )
        sys.exit(1)

    if row is None:
        print(
            "[FAIL] No active tenant found for this corp. "
            "Run bootstrap_default_tenant.py after migration 0002.",
            flush=True,
        )
        sys.exit(1)

    return row.tenant_id


def _collect_participant_ids(session: Session, tenant_id: str) -> list[str]:
    """Return distinct, non-blank participant IDs seen in the tenant's archive."""
    sender_ids = {
        row[0]
        for row in session.query(ArchiveMessage.sender)
        .filter(ArchiveMessage.sender.isnot(None), ArchiveMessage.tenant_id == tenant_id)
        .distinct()
        .all()
    }
    recipient_ids = {
        row[0]
        for row in session.query(ArchiveMessageRecipient.receiver_userid)
        .filter(ArchiveMessageRecipient.tenant_id == tenant_id)
        .distinct()
        .all()
    }
    return sorted({uid.strip() for uid in (sender_ids | recipient_ids) if uid and uid.strip()})


def _resolve_display_name(
    uid: str, access_token: str, external_token: Optional[str]
) -> tuple[Optional[str], str]:
    """Return (name, source) where source is 'member', 'external', or 'unresolved'.

    Tries the internal member API first; falls back to the external contact
    API only when a token for it is configured. Never raises — any lookup
    failure surfaces as an 'unresolved' result.
    """
    name = fetch_member_display_name(access_token, uid)
    if name:
        return name, "member"

    if external_token:
        name = fetch_external_contact_display_name(external_token, uid)
        if name:
            return name, "external"

    return None, "unresolved"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")
    oauth_secret = _require_env("WECOM_OAUTH_SECRET")
    external_secret = _optional_env("WECOM_EXTERNAL_CONTACT_SECRET", "")

    engine = create_engine(database_url)

    with Session(engine) as session:
        tenant_id = _require_tenant_id(session, corp_id)
        participant_ids = _collect_participant_ids(session, tenant_id)

    print(f"[INFO] participant_ids scanned: {len(participant_ids)}", flush=True)

    try:
        access_token = get_wecom_token(corp_id, oauth_secret)
    except Exception:
        print("[FAIL] Failed to obtain WeCom access_token", flush=True)
        sys.exit(1)

    external_token = None
    if external_secret:
        try:
            external_token = get_wecom_token(
                corp_id, external_secret, cache_key=f"{corp_id}:external_contact"
            )
        except Exception:
            print(
                "[INFO] Failed to obtain external-contact access_token; "
                "external lookups disabled for this run",
                flush=True,
            )
            external_token = None

    resolved_member = 0
    resolved_external = 0
    skipped_already_named = 0
    unresolved = 0

    with Session(engine) as session:
        for uid in participant_ids:
            existing = (
                session.query(Contact)
                .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid == uid)
                .first()
            )
            if existing is not None and existing.name and existing.name.strip():
                skipped_already_named += 1
                continue

            name, source = _resolve_display_name(uid, access_token, external_token)
            if source == "member":
                resolved_member += 1
            elif source == "external":
                resolved_external += 1
            else:
                unresolved += 1
                continue

            upsert_contact_display_name(session, tenant_id, uid, name)

        try:
            session.commit()
        except Exception as exc:
            print(f"[FAIL] Database commit failed: {type(exc).__name__}", flush=True)
            sys.exit(1)

    print(f"[INFO] resolved_via_member_api: {resolved_member}", flush=True)
    print(f"[INFO] resolved_via_external_api: {resolved_external}", flush=True)
    print(f"[INFO] skipped_already_named: {skipped_already_named}", flush=True)
    print(f"[INFO] unresolved: {unresolved}", flush=True)
    print("[PASS] sync_contact_display_names_once completed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
