#!/usr/bin/env python3
"""
Mock archive ingestion pipeline for development use.

Inserts fake archive messages and contacts into PostgreSQL so development can
continue before the paid WeCom archive service is renewed.  Uses fake user IDs
and fake content only — no real SDK polling, no real decryption, no media
download.

Dataset supports the Conversation Review Console (RND-96/RND-97/RND-98):
  - Two monitored accounts: staff_yingzi, staff_xiaobudian
  - Three contacts: contact_zhangsan, contact_lisi, contact_wangwu
  - Two direct conversations (Yingzi ↔ ZhangSan; Yingzi ↔ LiSi)
  - One extra direct conversation (Xiaobudian ↔ ZhangSan) to prove cross-account
  - Two group conversations: after_sales_group_001, delivery_support_group_001
  - ZhangSan appears under both monitored accounts → validates contact-centered view

Usage (from backend/):
    python scripts/mock_ingest.py
    python scripts/mock_ingest.py --query-sender staff_yingzi
    python scripts/mock_ingest.py --query-text 订单

Required environment variable:
    DATABASE_URL   PostgreSQL connection string (e.g. postgresql://user:pw@host/db)

Idempotent: re-running never creates duplicate rows (keyed on msgid / wecom_userid).
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running from backend/ without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, or_
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact

# ---------------------------------------------------------------------------
# Mock data — fake user IDs and fake content only
# ---------------------------------------------------------------------------

MOCK_CORP_ID = "mock_corp_365"

# Contact registry: wecom_userid → display name.
# Includes both monitored-account staff and external contacts so the
# Conversation Review Console can resolve display names without inference.
MOCK_CONTACTS = {
    "staff_yingzi": "365客服英子",
    "staff_xiaobudian": "365客服小不点",
    "contact_zhangsan": "张三",
    "contact_lisi": "李四",
    "contact_wangwu": "王五",
}

MOCK_MESSAGES = [
    # ------------------------------------------------------------------
    # Legacy messages — kept for idempotency with earlier development DBs.
    # These use the original placeholder IDs (alice/bob/carol).
    # ------------------------------------------------------------------
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
    # ------------------------------------------------------------------
    # Direct conversation: staff_yingzi (365客服英子) ↔ contact_zhangsan (张三)
    # roomid: None (1:1 direct message)
    # Theme: order status inquiry → scan progress → follow-up
    # Timestamps: 2026-06-01 09:00–10:10 UTC
    # ------------------------------------------------------------------
    {
        "msgid": "mock_yz_zs_001",
        "seq": 2001,
        "sender": "contact_zhangsan",
        "tolist": ["staff_yingzi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "你好，我想查询一下我的订单状态，订单号是 ORD-20260601-088。",
        "msgtime": 1779901200000,  # 2026-06-01 09:00 UTC
    },
    {
        "msgid": "mock_yz_zs_002",
        "seq": 2002,
        "sender": "staff_yingzi",
        "tolist": ["contact_zhangsan"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "您好张先生，已收到您的查询，帮您确认一下，稍等片刻。",
        "msgtime": 1779902400000,  # 09:20
    },
    {
        "msgid": "mock_yz_zs_003",
        "seq": 2003,
        "sender": "staff_yingzi",
        "tolist": ["contact_zhangsan"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "已确认，您的样品目前正在进行3D扫描处理，预计明天下午完成。",
        "msgtime": 1779904200000,  # 09:50
    },
    {
        "msgid": "mock_yz_zs_004",
        "seq": 2004,
        "sender": "contact_zhangsan",
        "tolist": ["staff_yingzi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "好的，谢谢！完成后麻烦及时通知我。",
        "msgtime": 1779904800000,  # 10:00
    },
    {
        "msgid": "mock_yz_zs_005",
        "seq": 2005,
        "sender": "staff_yingzi",
        "tolist": ["contact_zhangsan"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "好的，完成后第一时间通知您。",
        "msgtime": 1779905400000,  # 10:10
    },
    # ------------------------------------------------------------------
    # Direct conversation: staff_yingzi (365客服英子) ↔ contact_lisi (李四)
    # roomid: None (1:1 direct message)
    # Theme: CAD design file received → progress check → timeline
    # Timestamps: 2026-06-01 14:00–15:10 UTC
    # ------------------------------------------------------------------
    {
        "msgid": "mock_yz_ls_001",
        "seq": 2011,
        "sender": "contact_lisi",
        "tolist": ["staff_yingzi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "英子你好，我发过来的CAD设计文件收到了吗？",
        "msgtime": 1779919200000,  # 2026-06-01 14:00 UTC
    },
    {
        "msgid": "mock_yz_ls_002",
        "seq": 2012,
        "sender": "staff_yingzi",
        "tolist": ["contact_lisi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "您好李先生，已收到文件，共3个CAD设计文件，正在安排工程师处理。",
        "msgtime": 1779920400000,  # 14:20
    },
    {
        "msgid": "mock_yz_ls_003",
        "seq": 2013,
        "sender": "contact_lisi",
        "tolist": ["staff_yingzi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "大概什么时候能出初稿？",
        "msgtime": 1779921600000,  # 14:40
    },
    {
        "msgid": "mock_yz_ls_004",
        "seq": 2014,
        "sender": "staff_yingzi",
        "tolist": ["contact_lisi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "预计48小时内完成初稿，完成后会立即发给您确认。",
        "msgtime": 1779922800000,  # 15:00
    },
    {
        "msgid": "mock_yz_ls_005",
        "seq": 2015,
        "sender": "contact_lisi",
        "tolist": ["staff_yingzi"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "好的，辛苦了。",
        "msgtime": 1779923400000,  # 15:10
    },
    # ------------------------------------------------------------------
    # Direct conversation: staff_xiaobudian (365客服小不点) ↔ contact_zhangsan (张三)
    # roomid: None (1:1 direct message)
    # Theme: re-scan request → rescan complete → confirmation
    # Timestamps: 2026-06-02 09:00–11:10 UTC
    #
    # Zhang San interacts with BOTH staff_yingzi AND staff_xiaobudian.
    # This is the key fixture that validates contact-centered cross-account aggregation.
    # ------------------------------------------------------------------
    {
        "msgid": "mock_xbd_zs_001",
        "seq": 2021,
        "sender": "contact_zhangsan",
        "tolist": ["staff_xiaobudian"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "你好，之前发给我的扫描文件分辨率有点低，可以重新扫描一次吗？",
        "msgtime": 1779987600000,  # 2026-06-02 09:00 UTC
    },
    {
        "msgid": "mock_xbd_zs_002",
        "seq": 2022,
        "sender": "staff_xiaobudian",
        "tolist": ["contact_zhangsan"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "您好张先生，已收到您的反馈，我们会安排重新高清扫描，请稍等。",
        "msgtime": 1779988800000,  # 09:20
    },
    {
        "msgid": "mock_xbd_zs_003",
        "seq": 2023,
        "sender": "staff_xiaobudian",
        "tolist": ["contact_zhangsan"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "重新扫描已完成，高清版本已上传，请查收。",
        "msgtime": 1779994800000,  # 11:00
    },
    {
        "msgid": "mock_xbd_zs_004",
        "seq": 2024,
        "sender": "contact_zhangsan",
        "tolist": ["staff_xiaobudian"],
        "roomid": None,
        "msgtype": "text",
        "content_text": "收到了，这次清晰多了，谢谢！",
        "msgtime": 1779995400000,  # 11:10
    },
    # ------------------------------------------------------------------
    # Group conversation: after_sales_group_001 (售后服务群)
    # Members: staff_yingzi, staff_xiaobudian, contact_wangwu
    # Theme: sample received → warehoused → scan scheduled
    # Timestamps: 2026-06-01 11:00–12:40 UTC
    # ------------------------------------------------------------------
    {
        "msgid": "mock_asg_001",
        "seq": 2031,
        "sender": "staff_yingzi",
        "tolist": ["staff_xiaobudian", "contact_wangwu"],
        "roomid": "after_sales_group_001",
        "msgtype": "text",
        "content_text": "王五您好，已确认收到您发来的样品，正在安排入库。",
        "msgtime": 1779908400000,  # 2026-06-01 11:00 UTC
    },
    {
        "msgid": "mock_asg_002",
        "seq": 2032,
        "sender": "contact_wangwu",
        "tolist": ["staff_yingzi", "staff_xiaobudian"],
        "roomid": "after_sales_group_001",
        "msgtype": "text",
        "content_text": "好的，谢谢！入库后麻烦告知一下我。",
        "msgtime": 1779909600000,  # 11:20
    },
    {
        "msgid": "mock_asg_003",
        "seq": 2033,
        "sender": "staff_xiaobudian",
        "tolist": ["staff_yingzi", "contact_wangwu"],
        "roomid": "after_sales_group_001",
        "msgtype": "text",
        "content_text": "样品已入库，编号为 WH-2026-0601-007，可以开始扫描流程了。",
        "msgtime": 1779912000000,  # 12:00
    },
    {
        "msgid": "mock_asg_004",
        "seq": 2034,
        "sender": "contact_wangwu",
        "tolist": ["staff_yingzi", "staff_xiaobudian"],
        "roomid": "after_sales_group_001",
        "msgtype": "text",
        "content_text": "太好了，期待扫描结果！",
        "msgtime": 1779913200000,  # 12:20
    },
    {
        "msgid": "mock_asg_005",
        "seq": 2035,
        "sender": "staff_yingzi",
        "tolist": ["staff_xiaobudian", "contact_wangwu"],
        "roomid": "after_sales_group_001",
        "msgtype": "text",
        "content_text": "扫描完成后会在群里同步进度，请放心。",
        "msgtime": 1779914400000,  # 12:40
    },
    # ------------------------------------------------------------------
    # Group conversation: delivery_support_group_001 (发货支持群)
    # Members: staff_xiaobudian, staff_yingzi, contact_zhangsan
    # Theme: shipment notification → logistics follow-up → closing
    # Timestamps: 2026-06-02 14:00–15:20 UTC
    #
    # Zhang San also appears here alongside the delivery_support group,
    # further demonstrating multi-conversation cross-account coverage.
    # ------------------------------------------------------------------
    {
        "msgid": "mock_dsg_001",
        "seq": 2041,
        "sender": "staff_xiaobudian",
        "tolist": ["staff_yingzi", "contact_zhangsan"],
        "roomid": "delivery_support_group_001",
        "msgtype": "text",
        "content_text": "张三先生您好，您的货物已于今日发出，快递单号：SF1234567890，顺丰快递。",
        "msgtime": 1780005600000,  # 2026-06-02 14:00 UTC
    },
    {
        "msgid": "mock_dsg_002",
        "seq": 2042,
        "sender": "contact_zhangsan",
        "tolist": ["staff_xiaobudian", "staff_yingzi"],
        "roomid": "delivery_support_group_001",
        "msgtype": "text",
        "content_text": "好的，收到了，谢谢通知！我会关注快递状态。",
        "msgtime": 1780006800000,  # 14:20
    },
    {
        "msgid": "mock_dsg_003",
        "seq": 2043,
        "sender": "staff_yingzi",
        "tolist": ["staff_xiaobudian", "contact_zhangsan"],
        "roomid": "delivery_support_group_001",
        "msgtype": "text",
        "content_text": "张先生，收到货物后请检查一下外包装是否完好，如有问题请及时联系我们。",
        "msgtime": 1780008000000,  # 14:40
    },
    {
        "msgid": "mock_dsg_004",
        "seq": 2044,
        "sender": "contact_zhangsan",
        "tolist": ["staff_xiaobudian", "staff_yingzi"],
        "roomid": "delivery_support_group_001",
        "msgtype": "text",
        "content_text": "明白，货到了会第一时间检查，有问题马上联系。",
        "msgtime": 1780009200000,  # 15:00
    },
    {
        "msgid": "mock_dsg_005",
        "seq": 2045,
        "sender": "staff_xiaobudian",
        "tolist": ["staff_yingzi", "contact_zhangsan"],
        "roomid": "delivery_support_group_001",
        "msgtype": "text",
        "content_text": "好的，有任何问题随时在群里联系我们，很高兴为您服务！",
        "msgtime": 1780010400000,  # 15:20
    },
]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _upsert_contacts(session: Session) -> dict[str, str]:
    """Insert mock contacts that do not already exist. Returns {wecom_userid: status}."""
    results: dict[str, str] = {}

    for wecom_userid, name in MOCK_CONTACTS.items():
        existing = (
            session.query(Contact)
            .filter(Contact.wecom_userid == wecom_userid)
            .first()
        )
        if existing:
            results[wecom_userid] = "skipped (already exists)"
            continue

        session.add(Contact(wecom_userid=wecom_userid, name=name))
        results[wecom_userid] = "inserted"

    session.commit()
    return results


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
        # --- Contacts ---
        print("[INFO] Inserting mock contacts …")
        contact_results = _upsert_contacts(session)
        for wecom_userid, status in contact_results.items():
            print(f"  {wecom_userid}: {status}")

        c_inserted = sum(1 for s in contact_results.values() if s == "inserted")
        c_skipped = sum(1 for s in contact_results.values() if s.startswith("skipped"))
        print(f"[INFO] Contacts done — {c_inserted} inserted, {c_skipped} skipped.")

        # --- Messages ---
        print("[INFO] Inserting mock archive messages …")
        results = _upsert_messages(session)
        for msgid, status in results.items():
            print(f"  {msgid}: {status}")

        inserted = sum(1 for s in results.values() if s == "inserted")
        skipped = sum(1 for s in results.values() if s.startswith("skipped"))
        print(f"[INFO] Messages done — {inserted} inserted, {skipped} skipped (idempotent).")

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
