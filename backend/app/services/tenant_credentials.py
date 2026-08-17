"""Per-tenant archive worker runtime credentials (RND-387).

Multi-tenant deployments run one worker chain per active tenant instead of
the legacy single-corp ``WECOM_CORP_ID``/``WECOM_ARCHIVE_SECRET`` environment
chain.  This module owns the DB-side tenant resolution and the digest log tag
so no caller embeds a raw tenant_id in a log line.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.db.models import Tenant, TenantWecomConfig


def active_tenant_configs(db: Session) -> list[TenantWecomConfig]:
    """Active, non-provisioning tenant config rows in stable creation order.

    Only fully live tenants archive: ``Tenant.is_active`` plus
    ``lifecycle_status == 'active'`` (RND-398) gate the per-tenant loop, and
    the config row itself must be active.  Provisioning tenants are excluded
    until activation promotes them.
    """
    return (
        db.query(TenantWecomConfig)
        .join(Tenant, Tenant.id == TenantWecomConfig.tenant_id)
        .filter(
            Tenant.is_active.is_(True),
            Tenant.lifecycle_status == "active",
            TenantWecomConfig.is_active.is_(True),
        )
        .order_by(TenantWecomConfig.created_at)
        .all()
    )


def config_for_tenant(db: Session, tenant_id: str) -> TenantWecomConfig | None:
    """The active config row for one tenant, if any."""
    return (
        db.query(TenantWecomConfig)
        .filter(
            TenantWecomConfig.tenant_id == tenant_id,
            TenantWecomConfig.is_active.is_(True),
        )
        .first()
    )


def tenant_log_tag(tenant_id: str) -> str:
    """A stable digest of a tenant id safe for logs; never the raw id."""
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]
