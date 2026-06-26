#!/usr/bin/env python3
"""
Mock archive ingestion pipeline for development use.

Inserts fake archive messages into PostgreSQL so development can continue
before the paid WeCom archive service is renewed.  Uses fake user IDs and
fake content only — no real SDK polling, no real decryption, no media download.

Usage (from backend/):
    python scripts/mock_ingest.py
    python scripts/mock_ingest.py --query-sender mock_user_alice
    python scripts/mock_ingest.py --query-text kickoff

Required environment variable:
    DATABASE_URL   PostgreSQL connection string (e.g. postgresql://user:pw@host/db)

Idempotent: re-running never creates duplicate rows (keyed on msgid).
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running from backend/ without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, or_
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient

# ---------------------------------------------------------------------------
# Mock data — fake user IDs and fake content only
# ---------------------------------------------------------------------------

MOCK_CORP_ID = "mock_corp_365"

MOCK_MESSAGES = [
    {
        "msgid": "mock_msg_001",
        "seq": 1001,
        "sender": "mock_user_alice",
        "tolist": ["mock_user_bob", "mock_user_carol"],
        "roomid": "mock_room_01",
        "msgtype": "text",
        "content_text": "Hello team, the project kickoff is scheduled for next Monday.",
        "msgtime": 1719360000000,
    },
    {
        "msgid": "mock_msg_002",
        "seq": 1002,
        "sender": "mock_user_bob",
        "tolist": ["mock_user_alice"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "Sounds good Alice, I will prepare the agenda.",
        "msgtime": 1719360060000,
    },
    {
        "msgid": "mock_msg_003",
        "seq": 1003,
        "sender": "mock_user_carol",
        "tolist": ["mock_user_alice", "mock_user_bob"],
        "roomid": "mock_room_01",
        "msgtype": "text",
        "content_text": "Looking forward to the kickoff meeting.",
        "msgtime": 1719360120000,
    },
]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _upsert_messages(session: Session) -> dict[str, str]:
    """Insert mock messages that do not already exist. Returns {msgid: status}."""
    results: dict[str, str] = {}

    for data in MOCK_MESSAGES:
        existing = (
            session.query(ArchiveMessage)
            .filter(ArchiveMessage.msgid == data["msgid"])
            .first()
        )
        if existing:
            results[data["msgid"]] = "skipped (already exists)"
            continue

        msg = ArchiveMessage(
            msgid=data["msgid"],
            seq=data["seq"],
            # Encrypted envelope — placeholder values; no real key or cipher used.
            publickey_ver=0,
            raw_encrypted_payload=None,
            encrypt_random_key="MOCK_KEY_PLACEHOLDER",
            encrypt_chat_msg="MOCK_CIPHER_PLACEHOLDER",
            # Decryption state — mock data is pre-normalised so mark as success.
            decrypt_status="success",
            decrypted_payload={
                "msgtype": data["msgtype"],
                "from": data["sender"],
                "tolist": data["tolist"],
                "roomid": data["roomid"],
                "msgtime": data["msgtime"],
                "text": {"content": data["content_text"]},
                "_mock": True,
            },
            # Extracted fields
            content_text=data["content_text"],
            msgtype=data["msgtype"],
            sender=data["sender"],
            roomid=data["roomid"],
            msgtime=data["msgtime"],
            tolist=data["tolist"],
            sdkfileid=None,
        )
        session.add(msg)
        session.flush()  # populate msg.id before inserting recipients

        for recipient in data["tolist"]:
            session.add(
                ArchiveMessageRecipient(
                    message_id=msg.id,
                    receiver_userid=recipient,
                    receiver_type="user",
                )
            )

        results[data["msgid"]] = "inserted"

    session.commit()
    return results


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def _query_messages(
    session: Session,
    sender: str | None,
    text: str | None,
) -> list[ArchiveMessage]:
    """Return messages matching sender (exact) or text (case-insensitive substring)."""
    q = session.query(ArchiveMessage)

    conditions = []
    if sender:
        conditions.append(ArchiveMessage.sender == sender)
    if text:
        conditions.append(ArchiveMessage.content_text.ilike(f"%{text}%"))

    if conditions:
        q = q.filter(or_(*conditions))

    return q.order_by(ArchiveMessage.msgtime).all()


def _print_message(msg: ArchiveMessage) -> None:
    recipients = msg.tolist or []
    room = msg.roomid or "(1:1)"
    print(
        f"  msgid={msg.msgid}  seq={msg.seq}  room={room}\n"
        f"    sender={msg.sender} → to={recipients}\n"
        f"    content_text: {msg.content_text}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query-sender",
        metavar="USERID",
        help="After ingest, query messages by this sender (exact match).",
    )
    parser.add_argument(
        "--query-text",
        metavar="TEXT",
        help="After ingest, query messages containing this text (case-insensitive).",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(database_url)

    with Session(engine) as session:
        # --- Ingest ---
        print("[INFO] Inserting mock archive messages …")
        results = _upsert_messages(session)
        for msgid, status in results.items():
            print(f"  {msgid}: {status}")

        inserted = sum(1 for s in results.values() if s == "inserted")
        skipped = sum(1 for s in results.values() if s.startswith("skipped"))
        print(f"[INFO] Done — {inserted} inserted, {skipped} skipped (idempotent).")

        # --- Query ---
        if args.query_sender or args.query_text:
            label_parts = []
            if args.query_sender:
                label_parts.append(f"sender={args.query_sender!r}")
            if args.query_text:
                label_parts.append(f"text={args.query_text!r}")
            print(f"\n[QUERY] Searching: {', '.join(label_parts)}")
            matches = _query_messages(session, args.query_sender, args.query_text)
            if matches:
                print(f"  Found {len(matches)} message(s):")
                for m in matches:
                    _print_message(m)
            else:
                print("  No messages matched.")
        else:
            # Default: show summary of all mock messages in DB.
            print("\n[QUERY] All mock messages currently in database:")
            all_mock = (
                session.query(ArchiveMessage)
                .filter(
                    ArchiveMessage.msgid.in_(
                        [m["msgid"] for m in MOCK_MESSAGES]
                    )
                )
                .order_by(ArchiveMessage.msgtime)
                .all()
            )
            for m in all_mock:
                _print_message(m)


if __name__ == "__main__":
    main()
