"""Safe tenant-scoped archive-worker credential resolution.

This module owns the DB-side active-configuration lookup, credential
classification, and digest log tag.  Callers must only emit the fixed error
classes below: values from tenant records and cryptography must never reach
worker output.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.crypto import (
    FieldDecryptionError,
    FieldEncryptionConfigurationError,
    is_encrypted,
)
from app.db.models import Tenant, TenantWecomConfig

TENANT_CONFIG_UNAVAILABLE = "tenant_config_unavailable"
TENANT_CREDENTIALS_MISSING_FIELDS = "tenant_credentials_missing_fields"
TENANT_CREDENTIALS_LEGACY_FORMAT = "tenant_credentials_legacy_format"
TENANT_CREDENTIALS_KEY_MISMATCH = "tenant_credentials_key_mismatch"
TENANT_CREDENTIALS_UNKNOWN = "tenant_credentials_unknown"


class TenantCredentialError(RuntimeError):
    """Expected credential failure carrying only a safe error class."""

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(error_class)


@dataclass(frozen=True)
class TenantArchiveCredentials:
    """Validated runtime values for one tenant archive worker chain."""

    config: TenantWecomConfig
    tenant_id: str
    corp_id: str
    archive_secret: str = field(repr=False)
    publickey_version: int | None


def active_tenant_ids(db: Session) -> list[str]:
    """Live tenant ids in a stable order, including rows missing a config.

    Worker loops use this alongside ``active_tenant_configs`` so an active
    tenant without configuration is observed as a fail-closed per-tenant
    failure instead of being silently omitted from reconciliation.
    """
    return [
        tenant_id
        for (tenant_id,) in (
            db.query(Tenant.id)
            .filter(Tenant.lifecycle_status == "active")
            .order_by(Tenant.created_at)
            .all()
        )
    ]


def active_tenant_configs(db: Session) -> list[TenantWecomConfig]:
    """Active, non-provisioning tenant config rows in stable creation order.

    Only fully live tenants archive: ``lifecycle_status == 'active'`` gates
    the per-tenant loop, and the config row itself must be active. Provisioning
    tenants are excluded until activation promotes them.
    """
    return (
        db.query(TenantWecomConfig)
        .join(Tenant, Tenant.id == TenantWecomConfig.tenant_id)
        .filter(
            Tenant.lifecycle_status == "active",
            TenantWecomConfig.is_active.is_(True),
        )
        .order_by(TenantWecomConfig.created_at)
        .all()
    )


def config_for_tenant(db: Session, tenant_id: str) -> TenantWecomConfig | None:
    """The active config row for one tenant, if any.

    This compatibility lookup intentionally retains its existing use by
    non-worker callers. Archive workers use ``resolve_tenant_archive_credentials``
    instead so they can distinguish missing/inactive config from bad secrets.
    """
    return (
        db.query(TenantWecomConfig)
        .filter(
            TenantWecomConfig.tenant_id == tenant_id,
            TenantWecomConfig.is_active.is_(True),
        )
        .first()
    )


def credentials_for_active_config(config: TenantWecomConfig) -> TenantArchiveCredentials:
    """Validate and decrypt one known-active archive config without leaks."""
    if (
        not config.corp_id
        or not config.corp_id.strip()
        or not config.app_secret
        or not config.app_secret.strip()
    ):
        raise TenantCredentialError(TENANT_CREDENTIALS_MISSING_FIELDS)
    if not is_encrypted(config.app_secret):
        raise TenantCredentialError(TENANT_CREDENTIALS_LEGACY_FORMAT)
    try:
        archive_secret = config.decrypted_app_secret
    except FieldDecryptionError as exc:
        raise TenantCredentialError(TENANT_CREDENTIALS_KEY_MISMATCH) from exc
    except FieldEncryptionConfigurationError as exc:
        raise TenantCredentialError(TENANT_CREDENTIALS_UNKNOWN) from exc
    except Exception as exc:  # noqa: BLE001 -- expose only the controlled category
        raise TenantCredentialError(TENANT_CREDENTIALS_UNKNOWN) from exc
    return TenantArchiveCredentials(
        config=config,
        tenant_id=config.tenant_id,
        corp_id=config.corp_id,
        archive_secret=archive_secret,
        publickey_version=config.publickey_version,
    )


def resolve_tenant_archive_credentials(
    db: Session, tenant_id: str
) -> TenantArchiveCredentials:
    """Resolve one explicitly targeted active tenant's archive credentials.

    A missing or non-active tenant, missing config, or inactive config is
    deliberately one safe operational category. The returned values must only
    be passed to the SDK/child environment and never to logs or API responses.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant_id)
        .first()
    )
    if (
        tenant is None
        or tenant.lifecycle_status != "active"
        or config is None
        or not config.is_active
    ):
        raise TenantCredentialError(TENANT_CONFIG_UNAVAILABLE)
    return credentials_for_active_config(config)


def tenant_log_tag(tenant_id: str) -> str:
    """A stable digest of a tenant id safe for logs; never the raw id."""
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]
