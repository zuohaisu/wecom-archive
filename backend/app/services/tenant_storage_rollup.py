"""Tenant media-storage daily rollup (RND-331).

This module is the single writer for ``tenant_storage_daily``.  It deliberately
uses the same explicit ``download_status == 'downloaded'`` filter as billing
consumers, so pending and failed download attempts never affect storage usage.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import MediaFile, Tenant, TenantStorageDaily


def refresh_tenant_storage_daily(
    db: Session,
    usage_date: Optional[date] = None,
    tenant_id: Optional[str] = None,
) -> int:
    """Upsert daily downloaded-media byte totals and return rows written.

    ``usage_date`` defaults to the current UTC calendar date.  Supplying a
    tenant ID makes a targeted retry possible; without it every tenant gets a
    row (including tenants whose current usage is zero).
    """
    rollup_date = usage_date or datetime.now(timezone.utc).date()
    tenants = select(Tenant.id)
    if tenant_id is not None:
        tenants = tenants.where(Tenant.id == tenant_id)

    written = 0
    for current_tenant_id in db.execute(tenants).scalars():
        used_bytes = int(
            db.execute(
                select(func.coalesce(func.sum(MediaFile.file_size), 0)).where(
                    MediaFile.tenant_id == current_tenant_id,
                    MediaFile.download_status == "downloaded",
                )
            ).scalar_one()
        )
        row = db.execute(
            select(TenantStorageDaily).where(
                TenantStorageDaily.tenant_id == current_tenant_id,
                TenantStorageDaily.usage_date == rollup_date,
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(
                TenantStorageDaily(
                    tenant_id=current_tenant_id,
                    usage_date=rollup_date,
                    used_bytes=used_bytes,
                )
            )
        else:
            row.used_bytes = used_bytes
        written += 1

    db.flush()
    return written
