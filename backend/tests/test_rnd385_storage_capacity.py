from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ArchiveMessage,
    BillingPlan,
    MediaFile,
    MediaQuotaBlock,
    PlanEntitlement,
    Subscription,
    Tenant,
    TenantStorageDaily,
)
from app.media_storage import LocalStorageProvider
from app.sdk import wecom_sdk
from app.services.entitlements import ANNUAL_PLAN_CODE
from app.services.media_worker import download_media_candidates
from app.services.storage_capacity import (
    StorageCapacityConfigurationError,
    capacity_from_values,
    check_storage_write,
    measure_storage_capacity,
)
from app.services.tenant_storage_rollup import refresh_tenant_storage_daily
from tests.fakes import FakeWecomSdk, install_fake_sdk

NOW = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)
GIB = 1024**3


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _tables():
    return [
        Tenant.__table__,
        BillingPlan.__table__,
        PlanEntitlement.__table__,
        Subscription.__table__,
        ArchiveMessage.__table__,
        MediaFile.__table__,
        MediaQuotaBlock.__table__,
        TenantStorageDaily.__table__,
    ]


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # The production-only FTS expression cannot be compiled by SQLite.
    # Remove it only while constructing this isolated compatibility schema;
    # all other ArchiveMessage columns, constraints, and indexes stay real.
    fts_index = next(
        index
        for index in ArchiveMessage.__table__.indexes
        if index.name == "ix_archive_messages_content_text_fts"
    )
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(engine, tables=_tables())
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result() as db:
        db.add_all(
            [
                Tenant(id="tenant-a", name="A", slug="tenant-a"),
                Tenant(id="tenant-b", name="B", slug="tenant-b"),
                BillingPlan(
                    id="rnd385-plan",
                    code=ANNUAL_PLAN_CODE,
                    display_name="年度基础套餐",
                    is_active=True,
                    amount_cents=9900,
                    currency="CNY",
                    billing_period_months=12,
                    storage_quota_bytes=100,
                ),
                Subscription(
                    id="rnd385-sub-a",
                    tenant_id="tenant-a",
                    plan_id="rnd385-plan",
                    status="active",
                    starts_at=NOW - timedelta(days=1),
                    ends_at=NOW + timedelta(days=365),
                    source="test",
                    renewal_count=0,
                    revision=1,
                ),
            ]
        )
        db.commit()
    return result


@pytest.mark.parametrize(
    ("used", "state"),
    [(79, "normal"), (80, "warning_80"), (90, "warning_90"), (100, "full"), (101, "over_limit")],
)
def test_capacity_thresholds_are_exact_without_float_drift(used, state) -> None:
    result = capacity_from_values(
        tenant_id="tenant-a",
        quota_bytes=100,
        used_bytes=used,
        measured_at=NOW,
        plan_code=ANNUAL_PLAN_CODE,
        subscription_status="active",
        entitled=True,
    )
    assert result.state == state
    assert result.remaining_bytes == max(100 - used, 0)
    assert result.utilization_basis_points == used * 100
    assert result.can_accept_new_media is (used < 100)


def test_unknown_usage_and_inactive_subscription_fail_closed() -> None:
    unknown = capacity_from_values(
        tenant_id="tenant-a",
        quota_bytes=100,
        used_bytes=None,
        measured_at=NOW,
        plan_code=ANNUAL_PLAN_CODE,
        subscription_status="active",
        entitled=True,
    )
    inactive = capacity_from_values(
        tenant_id="tenant-a",
        quota_bytes=0,
        used_bytes=12,
        measured_at=NOW,
        plan_code=None,
        subscription_status="not_subscribed",
        entitled=False,
    )
    assert unknown.state == unknown.usage_status == "unavailable"
    assert unknown.used_bytes is None and unknown.remaining_bytes is None
    assert inactive.state == "unavailable"
    assert inactive.can_accept_new_media is False


def test_measurement_and_gate_use_downloaded_tenant_bytes_only(factory) -> None:
    with factory() as db:
        db.add_all(
            [
                MediaFile(
                    sdkfileid="a-downloaded",
                    archive_message_id=1,
                    tenant_id="tenant-a",
                    download_status="downloaded",
                    file_size=80,
                ),
                MediaFile(
                    sdkfileid="a-pending",
                    archive_message_id=2,
                    tenant_id="tenant-a",
                    download_status="pending",
                    file_size=None,
                ),
                MediaFile(
                    sdkfileid="b-downloaded",
                    archive_message_id=3,
                    tenant_id="tenant-b",
                    download_status="downloaded",
                    file_size=10_000,
                ),
            ]
        )
        db.commit()
        snapshot = measure_storage_capacity(db, "tenant-a", at=NOW)
        exact_fill = check_storage_write(db, "tenant-a", 20, at=NOW)
        too_large = check_storage_write(db, "tenant-a", 21, at=NOW)
        db.rollback()

    assert snapshot.used_bytes == 80 and snapshot.state == "warning_80"
    assert exact_fill.allowed is True
    assert too_large.allowed is False and too_large.reason == "quota_exceeded"


def test_unified_worker_blocks_before_upload_then_recovers_without_losing_message(
    factory, tmp_path, monkeypatch
) -> None:
    payload = b"#!AMR" + b"quota-body"
    with factory() as db:
        db.get(BillingPlan, "rnd385-plan").storage_quota_bytes = len(payload) - 1
        message = ArchiveMessage(
            id=101,
            msgid="rnd385-message",
            seq=1,
            publickey_ver=1,
            encrypt_random_key="k",
            encrypt_chat_msg="c",
            decrypt_status="success",
            msgtype="voice",
            sdkfileid="rnd385-sdk",
            tenant_id="tenant-a",
        )
        db.add(message)
        db.commit()
        message_id = message.id

    fake = FakeWecomSdk()
    fake.set_media_chunks("rnd385-sdk", [payload])
    install_fake_sdk(monkeypatch, fake, wecom_sdk)
    provider = LocalStorageProvider(tmp_path)
    with factory() as db:
        message = db.get(ArchiveMessage, message_id)
        blocked = download_media_candidates(
            db,
            "tenant-a",
            "fake-lib",
            "fake-handle",
            provider,
            "local",
            30,
            [message],
            [],
            enforce_quota=True,
        )
        media = db.scalar(select(MediaFile).where(MediaFile.sdkfileid == "rnd385-sdk"))
        block = db.get(MediaQuotaBlock, media.id)
        assert blocked.quota_blocked == 1 and blocked.failed == 0
        assert media.download_status == "quota_blocked"
        assert block.observed_bytes == len(payload)
        assert block.reason == "quota_exceeded"
        assert list(Path(tmp_path).rglob("*.amr")) == []
        assert db.get(ArchiveMessage, message_id) is not None
        db.get(BillingPlan, "rnd385-plan").storage_quota_bytes = len(payload)
        db.commit()

        recovered = download_media_candidates(
            db,
            "tenant-a",
            "fake-lib",
            "fake-handle",
            provider,
            "local",
            30,
            [message],
            [],
            enforce_quota=True,
        )
        db.refresh(media)
        assert recovered.downloaded == 1 and recovered.quota_blocked == 0
        assert media.download_status == "downloaded"
        assert db.get(MediaQuotaBlock, media.id) is None
        assert Path(media.local_path).read_bytes() == payload
        assert db.scalar(select(TenantStorageDaily.used_bytes)) == len(payload)


def _postgres_test_url() -> str | None:
    url = os.getenv("RND385_TEST_DATABASE_URL", "").strip()
    if not url:
        return None
    if not urlparse(url).path.lstrip("/").endswith("test"):
        raise RuntimeError("RND385_TEST_DATABASE_URL must name a database ending in test")
    return url


@pytest.mark.skipif(
    _postgres_test_url() is None,
    reason="RND385_TEST_DATABASE_URL is not configured for isolated PostgreSQL proof",
)
def test_postgresql_concurrent_writes_cannot_spend_same_remaining_bytes() -> None:
    engine = create_engine(_postgres_test_url(), pool_size=4)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    message_ids = []
    with factory() as db:
        db.add(Tenant(id=tenant_id, name="RND-385", slug=f"rnd385-{suffix}"))
        db.add(
            BillingPlan(
                id=plan_id,
                code=f"rnd385-{suffix}",
                display_name="RND-385",
                is_active=True,
                amount_cents=1,
                currency="CNY",
                billing_period_months=12,
                storage_quota_bytes=10,
            )
        )
        db.flush()
        db.add(
            Subscription(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                plan_id=plan_id,
                status="active",
                starts_at=NOW - timedelta(days=1),
                ends_at=NOW + timedelta(days=1),
                source="test",
                renewal_count=0,
                revision=1,
            )
        )
        for index in range(2):
            message = ArchiveMessage(
                msgid=f"rnd385-{suffix}-{index}",
                seq=index + 1,
                publickey_ver=1,
                encrypt_random_key="k",
                encrypt_chat_msg="c",
                decrypt_status="success",
                tenant_id=tenant_id,
            )
            db.add(message)
            db.flush()
            message_ids.append(message.id)
        db.commit()

    def attempt(index: int) -> bool:
        with factory() as db:
            decision = check_storage_write(db, tenant_id, 6, at=NOW)
            if decision.allowed:
                db.add(
                    MediaFile(
                        sdkfileid=f"rnd385-{suffix}-{index}",
                        archive_message_id=message_ids[index],
                        tenant_id=tenant_id,
                        download_status="downloaded",
                        file_size=6,
                    )
                )
                refresh_tenant_storage_daily(db, tenant_id=tenant_id, usage_date=NOW.date())
            db.commit()
            return decision.allowed

    with ThreadPoolExecutor(max_workers=2) as executor:
        allowed = list(executor.map(attempt, range(2)))
    assert sorted(allowed) == [False, True]
    with factory() as db:
        assert measure_storage_capacity(db, tenant_id, at=NOW).used_bytes == 6


def test_selfhost_without_subscription_allows_core_media_write(
    factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("APP_EDITION", "selfhost")
    monkeypatch.delenv("SELFHOST_STORAGE_LIMIT_BYTES", raising=False)
    payload = b"#!AMR" + b"community-media"
    message = ArchiveMessage(
        id=301,
        msgid="gh168-selfhost-media",
        seq=1,
        publickey_ver=1,
        encrypt_random_key="k",
        encrypt_chat_msg="c",
        decrypt_status="success",
        msgtype="voice",
        sdkfileid="gh168-selfhost-sdk",
        tenant_id="tenant-b",
    )
    with factory() as db:
        db.add(message)
        db.commit()
        assert db.query(Subscription).filter_by(tenant_id="tenant-b").first() is None

    fake = FakeWecomSdk()
    fake.set_media_chunks("gh168-selfhost-sdk", [payload])
    install_fake_sdk(monkeypatch, fake, wecom_sdk)
    provider = LocalStorageProvider(tmp_path)
    with factory() as db:
        message = db.get(ArchiveMessage, 301)
        summary = download_media_candidates(
            db,
            "tenant-b",
            "fake-lib",
            "fake-handle",
            provider,
            "local",
            30,
            [message],
            [],
            enforce_quota=True,
        )
        media = db.query(MediaFile).filter_by(sdkfileid="gh168-selfhost-sdk").one()
        capacity = measure_storage_capacity(db, "tenant-b", at=NOW)
        assert summary.downloaded == 1 and summary.quota_blocked == 0
        assert Path(media.local_path).read_bytes() == payload
        assert capacity.state == "unlimited"
        assert capacity.subscription_status == "not_applicable"
        assert capacity.can_accept_new_media is True


def test_selfhost_technical_storage_limit_blocks_before_provider_write(
    factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("APP_EDITION", "selfhost")
    payload = b"#!AMR" + b"limited-community-media"
    monkeypatch.setenv("SELFHOST_STORAGE_LIMIT_BYTES", str(len(payload) - 1))
    message = ArchiveMessage(
        id=302,
        msgid="gh168-selfhost-limited-media",
        seq=1,
        publickey_ver=1,
        encrypt_random_key="k",
        encrypt_chat_msg="c",
        decrypt_status="success",
        msgtype="voice",
        sdkfileid="gh168-selfhost-limited-sdk",
        tenant_id="tenant-b",
    )
    with factory() as db:
        db.add(message)
        db.commit()

    fake = FakeWecomSdk()
    fake.set_media_chunks("gh168-selfhost-limited-sdk", [payload])
    install_fake_sdk(monkeypatch, fake, wecom_sdk)
    provider = LocalStorageProvider(tmp_path)
    with factory() as db:
        message = db.get(ArchiveMessage, 302)
        summary = download_media_candidates(
            db,
            "tenant-b",
            "fake-lib",
            "fake-handle",
            provider,
            "local",
            30,
            [message],
            [],
            enforce_quota=True,
        )
        assert summary.quota_blocked == 1 and summary.failed == 0
        assert summary.reason_counts["quota_exceeded"] == 1
        assert list(Path(tmp_path).rglob("*.amr")) == []
        assert check_storage_write(db, "tenant-b", len(payload), at=NOW).reason == "quota_exceeded"


def test_invalid_selfhost_storage_limit_fails_closed(factory, monkeypatch) -> None:
    monkeypatch.setenv("APP_EDITION", "selfhost")
    monkeypatch.setenv("SELFHOST_STORAGE_LIMIT_BYTES", "-1")
    with factory() as db:
        with pytest.raises(StorageCapacityConfigurationError):
            check_storage_write(db, "tenant-b", 1, at=NOW)
