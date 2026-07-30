#!/usr/bin/env python3
"""Idempotently seed fake, tenant-scoped data for local analytics development.

Run from backend/ after sourcing .env:
    python scripts/seed_dev_analytics.py

All identities, message text, and attachment identifiers are synthetic. The
script creates an active password-login account for the fake tenant:
``dev-analytics@example.test`` / ``dev-analytics``.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.db.models import AdminUser, ArchiveMessage, Contact, MediaFile, Tenant

TENANT_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_ID = "00000000-0000-0000-0000-000000000101"


def main() -> None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is required")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=120)
    engine = create_engine(url)
    with Session(engine) as db:
        tenant = db.get(Tenant, TENANT_ID)
        if tenant is None:
            tenant = Tenant(id=TENANT_ID, name="Analytics Demo", slug="analytics-demo", created_at=start)
            db.add(tenant)
        else:
            # The existing general mock fixture may have created this tenant
            # today; analytics needs a historical archive span to exercise its cards.
            tenant.created_at = start

        admin = db.get(AdminUser, ADMIN_ID)
        if admin is None:
            db.add(AdminUser(
                id=ADMIN_ID, tenant_id=TENANT_ID, wecom_user_id="dev_analytics_admin",
                name="Analytics Demo Admin", email="dev-analytics@example.test",
                password_hash=hash_password("dev-analytics"), role="admin", status="active",
            ))

        for userid, name in (("demo_alex", "Demo Alex"), ("demo_blair", "Demo Blair"),
                             ("demo_casey", "Demo Casey"), ("demo_drew", "Demo Drew")):
            if db.scalar(select(Contact.id).where(Contact.tenant_id == TENANT_ID, Contact.wecom_userid == userid)) is None:
                db.add(Contact(tenant_id=TENANT_ID, wecom_userid=userid, name=name))
        db.flush()
        # The pre-existing generic mock fixture includes a few 2024 rows.
        # Keep all fixture data inside the demo archive window so the A1
        # archived-days primitive produces a useful non-zero card.
        db.execute(
            update(ArchiveMessage)
            .where(
                ArchiveMessage.tenant_id == TENANT_ID,
                ArchiveMessage.msgid.like("mock_%"),
                ArchiveMessage.msgtime < int((now - timedelta(days=120)).timestamp() * 1000),
            )
            .values(msgtime=int((now - timedelta(days=89)).timestamp() * 1000))
        )

        types = ("text", "image", "file", "voice", "video", "link", "markdown", "location", "weapp", "sys", "unknown_demo")
        created = 0
        media_created = 0
        for day_offset in range(90):
            day = now - timedelta(days=day_offset)
            # Spread messages over every hour so hourly distribution is meaningful.
            for hour in range(0, 24, 3):
                index = day_offset * 8 + hour // 3
                msgid = f"dev-analytics-{day_offset:03d}-{hour:02d}"
                if db.scalar(select(ArchiveMessage.id).where(ArchiveMessage.tenant_id == TENANT_ID, ArchiveMessage.msgid == msgid)):
                    continue
                msgtype = types[index % len(types)]
                message = ArchiveMessage(
                    msgid=msgid, seq=900000 + index, publickey_ver=0,
                    encrypt_random_key="DEV_FAKE_KEY", encrypt_chat_msg="DEV_FAKE_CIPHER",
                    decrypt_status="success", msgtype=msgtype,
                    content_text=f"Synthetic {msgtype} analytics fixture #{index}",
                    sender=("demo_alex" if index % 2 else "demo_blair"),
                    roomid=("dev_analytics_group" if index % 3 else None),
                    msgtime=int(day.replace(hour=hour).timestamp() * 1000),
                    tolist=["demo_casey"], tenant_id=TENANT_ID,
                )
                db.add(message)
                db.flush()
                created += 1
                if msgtype in ("image", "file", "voice", "video"):
                    size = {"image": 2_500_000, "file": 8_000_000, "voice": 900_000, "video": 24_000_000}[msgtype]
                    db.add(MediaFile(
                        sdkfileid=f"dev-analytics-media-{index}", archive_message_id=message.id,
                        tenant_id=TENANT_ID, file_type=msgtype, file_size=size + index * 100,
                        download_status="downloaded", storage_backend="local",
                        storage_ref=f"/tmp/dev-analytics-{index}.{msgtype}",
                    ))
                    media_created += 1
        db.commit()
    print(f"Seed complete: {created} messages and {media_created} media rows inserted (idempotent).")


if __name__ == "__main__":
    main()
