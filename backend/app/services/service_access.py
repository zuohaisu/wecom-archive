"""Authoritative tenant service access policy (RND-402).

This module is the single place that maps the authoritative
``Tenant.lifecycle_status`` projection (ADR-0005 §2.6) to capability
classes with stable denial codes.  Consumers must never recreate
grace/frozen/suspended decisions with local string or timestamp
comparisons; they call :func:`tenant_service_denial` (or the DB helper
:func:`tenant_service_allows`) instead.

Policy facts:

* Subscription ``grace`` is already folded into the ``active`` tenant
  projection by ``app.services.billing_lifecycle``, so no time logic
  exists here.
* Only explicit ``frozen`` / ``suspended`` trigger commercial gating.
  Tenants that were never commercialized keep the default ``active``
  projection (server default) and are therefore unaffected — a
  deployment not configured for billing is never accidentally frozen by
  this policy.
* ``suspended`` is the highest-priority state: every capability class is
  denied, including owner billing (payment can never clear a manual
  suspension; only a platform-admin resume reprojects the tenant).
* Denial codes are stable strings intended for HTTP ``detail`` values
  and sanitized worker log/audit lines; they never carry message
  content, credentials, or identifiers.

Capability classes (see ADR-0005 §2.6 access matrix):

* ``interactive`` — admin console business access (history, search,
  media, settings, exports …).
* ``owner_billing`` — Owner/manager billing, renewal orders, payment
  queries (allowed for ``frozen`` so a frozen tenant can always renew).
* ``worker_sync`` — new session sync, decrypt, archive writes.
* ``worker_media`` — media download / thumbnail / transcode workers.
* ``worker_export`` — export job generation and notification workers.
"""

from __future__ import annotations

# Capability classes -----------------------------------------------------

INTERACTIVE = "interactive"
OWNER_BILLING = "owner_billing"
WORKER_SYNC = "worker_sync"
WORKER_MEDIA = "worker_media"
WORKER_EXPORT = "worker_export"

CAPABILITIES: frozenset[str] = frozenset(
    {
        INTERACTIVE,
        OWNER_BILLING,
        WORKER_SYNC,
        WORKER_MEDIA,
        WORKER_EXPORT,
    }
)

# Stable denial codes ----------------------------------------------------

DENY_SUSPENDED = "service_suspended"
DENY_FROZEN = "service_frozen"
DENY_PROVISIONING = "service_provisioning"
DENY_UNKNOWN_STATUS = "service_unavailable"

# Capability → lifecycle_status → allowed.  Fail closed: anything not
# explicitly listed is denied.
_ALLOWED: dict[str, frozenset[str]] = {
    "provisioning": frozenset({OWNER_BILLING}),
    "active": frozenset(
        {INTERACTIVE, OWNER_BILLING, WORKER_SYNC, WORKER_MEDIA, WORKER_EXPORT}
    ),
    "frozen": frozenset({OWNER_BILLING}),
    "suspended": frozenset(),
}

_DENIAL_CODE_BY_STATUS: dict[str, str] = {
    "provisioning": DENY_PROVISIONING,
    "frozen": DENY_FROZEN,
    "suspended": DENY_SUSPENDED,
}


def tenant_service_denial(
    tenant_lifecycle_status: str | None, capability: str
) -> str | None:
    """Return the stable denial code for *capability*, or ``None`` when allowed.

    ``None`` lifecycle status (tenant row missing) fails closed with the
    most restrictive code.
    """
    status = tenant_lifecycle_status or "suspended"
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown service capability: {capability!r}")
    if capability in _ALLOWED.get(status, frozenset()):
        return None
    return _DENIAL_CODE_BY_STATUS.get(status, DENY_UNKNOWN_STATUS)


def tenant_service_allows(
    tenant_lifecycle_status: str | None, capability: str
) -> bool:
    """Boolean form of :func:`tenant_service_denial` for worker gates."""
    return tenant_service_denial(tenant_lifecycle_status, capability) is None
