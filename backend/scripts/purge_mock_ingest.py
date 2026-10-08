"""Purge the rows scripts/mock_ingest.py wrote into a database (GH-100
follow-up: the internal-staff directory faithfully lists archive
participants, so mock rows ingested into a real environment surface there).

Scope, tenant-scoped to mock_ingest's DEFAULT_TENANT_ID:
- archive_messages whose msgid starts with 'mock_' — WeCom msgids are
  opaque server-assigned strings, so the prefix is unambiguous;
- their archive_message_recipients, media_files and archive_favorites
  (favorites cascade on the message; counted for the report);
- the MOCK_CONTACTS registry rows.

sync_states and audit_log are intentionally never touched: mock_ingest
does not write them, and the audit trail is append-only history.

Dry run by default — prints what would be deleted. ``--apply`` executes
the deletions in one transaction. Run from backend/ with DATABASE_URL set:

    python scripts/purge_mock_ingest.py            # dry run
    python scripts/purge_mock_ingest.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.db.models import (
    ArchiveFavorite,
    ArchiveMessage,
    ArchiveMessageRecipient,
    Contact,
    MediaFile,
)
from scripts.mock_ingest import DEFAULT_TENANT_ID, MOCK_CONTACTS

MOCK_MSGID_PREFIX = "mock_"


def _mock_message_ids(session) -> list[int]:
    return list(
        session.scalars(
            select(ArchiveMessage.id).where(
                ArchiveMessage.tenant_id == DEFAULT_TENANT_ID,
                ArchiveMessage.msgid.like(MOCK_MSGID_PREFIX + "%"),
            )
        )
    )


def _plan(session) -> dict[str, int]:
    message_ids = _mock_message_ids(session)
    plan: dict[str, int] = {"messages": len(message_ids)}
    if not message_ids:
        plan["recipients"] = 0
        plan["media_files"] = 0
        plan["favorites"] = 0
        plan["contacts"] = 0
        return plan
    plan["recipients"] = len(list(session.scalars(
        select(ArchiveMessageRecipient.id).where(
            ArchiveMessageRecipient.message_id.in_(message_ids)
        )
    )))
    plan["media_files"] = len(list(session.scalars(
        select(MediaFile.id).where(
            MediaFile.archive_message_id.in_(message_ids)
        )
    )))
    plan["favorites"] = len(list(session.scalars(
        select(ArchiveFavorite.id).where(
            ArchiveFavorite.archive_message_id.in_(message_ids)
        )
    )))
    plan["contacts"] = len(list(session.scalars(
        select(Contact.id).where(
            Contact.tenant_id == DEFAULT_TENANT_ID,
            Contact.wecom_userid.in_(MOCK_CONTACTS),
        )
    )))
    return plan


def _purge(session) -> dict[str, int]:
    message_ids = _mock_message_ids(session)
    counts: dict[str, int] = {}
    if message_ids:
        counts["favorites"] = session.query(ArchiveFavorite).filter(
            ArchiveFavorite.archive_message_id.in_(message_ids)
        ).delete(synchronize_session=False)
        counts["media_files"] = session.query(MediaFile).filter(
            MediaFile.archive_message_id.in_(message_ids)
        ).delete(synchronize_session=False)
        counts["recipients"] = session.query(ArchiveMessageRecipient).filter(
            ArchiveMessageRecipient.message_id.in_(message_ids)
        ).delete(synchronize_session=False)
        counts["messages"] = session.query(ArchiveMessage).filter(
            ArchiveMessage.id.in_(message_ids)
        ).delete(synchronize_session=False)
    else:
        counts["messages"] = 0
    counts["contacts"] = session.query(Contact).filter(
        Contact.tenant_id == DEFAULT_TENANT_ID,
        Contact.wecom_userid.in_(MOCK_CONTACTS),
    ).delete(synchronize_session=False)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="execute the deletions (default: dry run)",
    )
    args = parser.parse_args()

    from sqlalchemy.orm import Session

    from app.db.session import get_engine

    with Session(get_engine()) as session:
        plan = _plan(session)
        print(f"tenant={DEFAULT_TENANT_ID}")
        for key in ("messages", "recipients", "media_files", "favorites", "contacts"):
            print(f"  {key}: {plan[key]}")
        if not args.apply:
            print("dry run — nothing deleted; re-run with --apply to purge")
            return
        counts = _purge(session)
        session.commit()
        print(f"purged: {counts}")


if __name__ == "__main__":
    main()
