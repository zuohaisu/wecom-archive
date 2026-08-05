"""Database-backed aggregation coverage for RND-281 UsageService."""

from __future__ import annotations

import inspect
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, Contact, MediaFile, SyncState, Tenant


_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL"))


def test_module_import_and_functions_present() -> None:
    """The service remains importable even when no database is configured."""
    from app.services import usageservice

    for name in (
        "count_messages",
        "sum_storage",
        "count_monitored_employees",
        "get_archived_days",
        "sync_health",
    ):
        function = getattr(usageservice, name)
        assert callable(function)
        signature = inspect.signature(function)
        assert tuple(signature.parameters) == ("db", "tenant_id")
        assert signature.parameters["tenant_id"].default is None


def _msgtime(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _message(*, msgid: str, seq: int, tenant_id: str | None, msgtime: int) -> ArchiveMessage:
    return ArchiveMessage(
        msgid=msgid,
        seq=seq,
        publickey_ver=1,
        encrypt_random_key="test-key",
        encrypt_chat_msg="test-message",
        tenant_id=tenant_id,
        msgtime=msgtime,
        decrypt_status="success",
    )


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_usage_aggregations_are_tenant_scoped_and_global_when_unscoped() -> None:
    from app.services import usageservice

    engine = create_engine(os.environ["DATABASE_URL"])
    tenant_id = str(uuid.uuid4())
    prefix = f"rnd281-{tenant_id}"
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    error_updated_at = datetime(2026, 1, 4, 10, tzinfo=timezone.utc)
    syncing_updated_at = datetime(2026, 1, 4, 11, tzinfo=timezone.utc)
    media_ids: list[int] = []
    message_ids: list[int] = []
    contact_ids: list[int] = []
    sync_state_ids: list[int] = []

    with Session(engine) as db:
        try:
            global_message_count_before = usageservice.count_messages(db)
            global_storage_before = usageservice.sum_storage(db)
            global_employee_count_before = usageservice.count_monitored_employees(db)
            global_days_before = {
                datetime.fromtimestamp(msgtime / 1000.0, tz=timezone.utc).date()
                for msgtime in db.execute(
                    select(ArchiveMessage.msgtime).where(
                        ArchiveMessage.msgtime.isnot(None),
                        ArchiveMessage.decrypt_status == "success",
                    )
                ).scalars()
            }

            db.add(
                Tenant(
                    id=tenant_id,
                    name="RND-281 test",
                    slug=f"rnd281-{tenant_id}",
                    created_at=created_at,
                )
            )
            messages = [
                _message(
                    msgid=f"{prefix}-tenant-1",
                    seq=1,
                    tenant_id=tenant_id,
                    msgtime=_msgtime(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                ),
                _message(
                    msgid=f"{prefix}-tenant-2",
                    seq=2,
                    tenant_id=tenant_id,
                    msgtime=_msgtime(datetime(2026, 1, 3, tzinfo=timezone.utc)),
                ),
                _message(
                    msgid=f"{prefix}-tenant-3",
                    seq=3,
                    tenant_id=tenant_id,
                    msgtime=_msgtime(datetime(2026, 1, 3, 12, tzinfo=timezone.utc)),
                ),
                _message(
                    msgid=f"{prefix}-global",
                    seq=4,
                    tenant_id=None,
                    msgtime=_msgtime(datetime(2026, 1, 2, tzinfo=timezone.utc)),
                ),
            ]
            db.add_all(messages)
            db.flush()
            message_ids.extend(message.id for message in messages)

            media = [
                MediaFile(
                    sdkfileid=f"{prefix}-media-1",
                    archive_message_id=messages[0].id,
                    tenant_id=tenant_id,
                    file_size=10,
                    download_status="downloaded",
                ),
                MediaFile(
                    sdkfileid=f"{prefix}-media-2",
                    archive_message_id=messages[1].id,
                    tenant_id=tenant_id,
                    file_size=20,
                    download_status="downloaded",
                ),
                MediaFile(
                    sdkfileid=f"{prefix}-media-null-size",
                    archive_message_id=messages[2].id,
                    tenant_id=tenant_id,
                    file_size=None,
                ),
                MediaFile(
                    sdkfileid=f"{prefix}-media-global",
                    archive_message_id=messages[3].id,
                    tenant_id=None,
                    file_size=5,
                    download_status="downloaded",
                ),
            ]
            contacts = [
                Contact(wecom_userid=f"{prefix}-employee-1", tenant_id=tenant_id),
                Contact(wecom_userid=f"{prefix}-employee-2", tenant_id=tenant_id),
                Contact(wecom_userid=f"{prefix}-employee-2", tenant_id=None),
                Contact(wecom_userid=f"{prefix}-employee-3", tenant_id=None),
            ]
            sync_states = [
                SyncState(
                    corp_id=f"{prefix}-error",
                    tenant_id=tenant_id,
                    last_seq=7,
                    status="error",
                    error_message="RND-281 sync error",
                    updated_at=error_updated_at,
                ),
                SyncState(
                    corp_id=f"{prefix}-syncing",
                    tenant_id=tenant_id,
                    last_seq=11,
                    status="syncing",
                    updated_at=syncing_updated_at,
                ),
                SyncState(
                    corp_id=f"{prefix}-idle",
                    tenant_id=None,
                    last_seq=13,
                    status="idle",
                    updated_at=datetime(2026, 1, 4, 12, tzinfo=timezone.utc),
                ),
            ]
            db.add_all(media + contacts + sync_states)
            db.commit()
            media_ids.extend(row.id for row in media)
            contact_ids.extend(row.id for row in contacts)
            sync_state_ids.extend(row.id for row in sync_states)

            assert usageservice.count_messages(db, tenant_id) == 3
            assert usageservice.count_messages(db, None) == global_message_count_before + 4
            assert usageservice.sum_storage(db, tenant_id) == 30
            assert usageservice.sum_storage(db, None) == global_storage_before + 35
            assert usageservice.count_monitored_employees(db, tenant_id) == 2
            assert (
                usageservice.count_monitored_employees(db, None)
                == global_employee_count_before + 3
            )
            assert usageservice.get_archived_days(db, tenant_id) == 2
            assert usageservice.get_archived_days(db, None) == len(
                global_days_before
                | {
                    datetime(2026, 1, 1, tzinfo=timezone.utc).date(),
                    datetime(2026, 1, 2, tzinfo=timezone.utc).date(),
                    datetime(2026, 1, 3, tzinfo=timezone.utc).date(),
                }
            )
            assert usageservice.sync_health(db, tenant_id) == {
                "status": "error",
                "error_message": "RND-281 sync error",
                "last_seq": 11,
                "updated_at": syncing_updated_at.isoformat(),
            }
            assert usageservice.sync_health(db, None)["status"] == "error"
        finally:
            db.rollback()
            db.execute(delete(MediaFile).where(MediaFile.id.in_(media_ids)))
            db.execute(delete(Contact).where(Contact.id.in_(contact_ids)))
            db.execute(delete(SyncState).where(SyncState.id.in_(sync_state_ids)))
            db.execute(delete(ArchiveMessage).where(ArchiveMessage.id.in_(message_ids)))
            db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            db.commit()
