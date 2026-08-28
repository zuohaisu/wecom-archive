"""GH-108 tenant-scoped production-workload regression coverage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_external_contact_reconciliation_uses_each_tenants_resolved_corp_id(
    monkeypatch,
) -> None:
    from app.services import external_contact_sync as sync

    session = MagicMock()
    configs = [SimpleNamespace(tenant_id="tenant-a"), SimpleNamespace(tenant_id="tenant-b")]
    credentials = {
        "tenant-a": SimpleNamespace(
            tenant_id="tenant-a", corp_id="corp-a", archive_secret="secret-a"
        ),
        "tenant-b": SimpleNamespace(
            tenant_id="tenant-b", corp_id="corp-b", archive_secret="secret-b"
        ),
    }
    calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(sync, "active_tenant_ids", lambda _session: ["tenant-a", "tenant-b"])
    monkeypatch.setattr(sync, "active_tenant_configs", lambda _session: configs)
    monkeypatch.setattr(
        sync, "credentials_for_active_config", lambda config: credentials[config.tenant_id]
    )
    monkeypatch.setattr(
        sync,
        "sync_external_contacts",
        lambda _session, tenant_id, corp_id, secret, **_kwargs: calls.append(
            (tenant_id, corp_id, secret)
        )
        or sync.RunSummary(),
    )
    monkeypatch.setattr(
        sync,
        "reconcile_internal_contact_avatars",
        lambda *_args, **_kwargs: SimpleNamespace(selected=0, ready=0, unavailable=0),
    )

    summary = sync.reconcile_active_tenants(session, "external-provider-secret", "oauth-provider-secret")

    assert calls == [
        ("tenant-a", "corp-a", "external-provider-secret"),
        ("tenant-b", "corp-b", "external-provider-secret"),
    ]
    assert (summary.tenant_count, summary.tenant_completed, summary.tenant_failed) == (2, 2, 0)


def test_external_contact_reconciliation_isolates_bad_and_missing_tenant_configs(
    monkeypatch,
) -> None:
    from app.services import external_contact_sync as sync
    from app.services.tenant_credentials import TenantCredentialError

    session = MagicMock()
    configs = [SimpleNamespace(tenant_id="tenant-a"), SimpleNamespace(tenant_id="tenant-b")]
    completed: list[str] = []

    monkeypatch.setattr(
        sync,
        "active_tenant_ids",
        lambda _session: ["tenant-a", "tenant-b", "tenant-missing"],
    )
    monkeypatch.setattr(sync, "active_tenant_configs", lambda _session: configs)

    def _credentials(config):
        if config.tenant_id == "tenant-a":
            raise TenantCredentialError("tenant_credentials_key_mismatch")
        return SimpleNamespace(
            tenant_id="tenant-b", corp_id="corp-b", archive_secret="secret-b"
        )

    monkeypatch.setattr(sync, "credentials_for_active_config", _credentials)
    monkeypatch.setattr(
        sync,
        "sync_external_contacts",
        lambda _session, tenant_id, *_args, **_kwargs: completed.append(tenant_id)
        or sync.RunSummary(),
    )
    monkeypatch.setattr(
        sync,
        "reconcile_internal_contact_avatars",
        lambda *_args, **_kwargs: SimpleNamespace(selected=0, ready=0, unavailable=0),
    )

    summary = sync.reconcile_active_tenants(session, "external-provider-secret", "")

    assert completed == ["tenant-b"]
    assert (summary.tenant_count, summary.tenant_completed, summary.tenant_failed) == (3, 1, 2)
    assert session.rollback.call_count == 1


def test_external_contact_tenant_slice_is_bounded_and_rotates_daily() -> None:
    from datetime import datetime, timedelta, timezone

    from app.services.external_contact_sync import _bounded_tenant_ids

    active = ["tenant-a", "tenant-b", "tenant-c"]
    first = _bounded_tenant_ids(active, 2, datetime(2026, 8, 5, tzinfo=timezone.utc))
    second = _bounded_tenant_ids(
        active,
        2,
        datetime(2026, 8, 5, tzinfo=timezone.utc) + timedelta(days=1),
    )

    assert len(first) == len(second) == 2
    assert first != second
    assert set(first) <= set(active)
    assert set(second) <= set(active)


def test_refresh_queue_never_selects_another_tenants_tasks(monkeypatch) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.models import ExternalContactRefreshTask
    from app.services import external_contact_refresh_worker as worker
    from app.services.external_contact_sync import RefreshResult

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ExternalContactRefreshTask.__table__.create(engine)
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all(
            [
                ExternalContactRefreshTask(
                    tenant_id="tenant-a",
                    external_userid="wm-a",
                    source="callback",
                    state="pending",
                    next_attempt_at=now,
                ),
                ExternalContactRefreshTask(
                    tenant_id="tenant-b",
                    external_userid="wm-b",
                    source="callback",
                    state="pending",
                    next_attempt_at=now,
                ),
            ]
        )
        session.commit()
        selected: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            worker,
            "refresh_external_contact",
            lambda _session, tenant_id, corp_id, _secret, external_userid, **_kwargs: selected.append(
                (tenant_id, corp_id, external_userid)
            )
            or RefreshResult(found=True),
        )

        summary = worker.run_external_contact_refresh_queue(
            session, "tenant-a", "corp-a", "external-provider-secret", now=now
        )

        assert (summary.selected, summary.refreshed, summary.unavailable) == (1, 1, 0)
        assert selected == [("tenant-a", "corp-a", "wm-a")]
        assert [task.tenant_id for task in session.query(ExternalContactRefreshTask).all()] == ["tenant-b"]


def test_versioned_archive_unit_unsets_only_legacy_archive_selectors() -> None:
    from pathlib import Path

    unit = (
        Path(__file__).resolve().parents[2]
        / "deploy/systemd/wecom-archive-worker.service"
    ).read_text(encoding="utf-8")

    assert "-u WECOM_CORP_ID -u WECOM_ARCHIVE_SECRET" in unit
    assert "WECOM_THIRD_PARTY" not in unit
