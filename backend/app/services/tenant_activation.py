"""RND-388 self-service activation state machine (自动连通性检查与无人工激活).

The gate evaluation is intentionally separate from ``Tenant.lifecycle_status``
(whose CHECK stays frozen at provisioning|active|frozen|suspended): this
module owns the persisted ``tenant_activation_checks`` state machine
(not_started|blocked|ready) and the single promotion routine.  Nothing here
accepts a client-supplied tenant id or lifecycle value; every entry point
reads the tenant from the session or the platform route argument.

Flow:  evaluate gates in order (stop at first failure) → grant the RND-394
15-day trial when the tenant has no subscription row → re-check entitlement →
persist state ready|blocked → ``activate_tenant`` promotes ready→active and
audits.  Replaying an already-active tenant is a no-op with no writes and no
network (the row lock inside the evaluation makes the first caller the only
promoter).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from app.audit import AuditAction, AuditObjectType, write_audit
from app.crypto import FieldDecryptionError, decrypt_value
from app.db.models import (
    AdminSession,
    Tenant,
    TenantActivationCheck,
    TenantWecomConfig,
    ThirdPartyOrganizationBinding,
)
from app.services.entitlements import (
    ARCHIVE_ACCESS,
    get_subscription_summary,
    has_entitlement,
)
from app.services.tenant_config_service import connectivity_failure_reason
from app.services.trial_subscriptions import (
    TrialAlreadyUsedError,
    TrialGrantCommand,
    TrialNotEligibleError,
    grant_self_service_trial,
)

logger = logging.getLogger(__name__)

GATE_IDS = ("binding", "config", "connectivity", "subscription", "runtime")

SAFE_ACTIVATION_ERROR_CODES = frozenset(
    {
        "missing_binding",
        "config_incomplete",
        "config_not_decryptable",
        "credentials_invalid",
        "connectivity_failed",
        "no_entitlement",
        "runtime_unavailable",
        "activation_conflict",
    }
)

GATE_ACTORS = frozenset({"platform", "self_service", "payment_notify"})


class TenantActivationError(RuntimeError):
    """A tenant the caller named does not exist (server-side contract)."""


@dataclass(frozen=True)
class ActivationSnapshot:
    """Safe, serializable view of one evaluation or the persisted row."""

    state: str
    gate_results: dict[str, dict]
    safe_error_code: str | None
    revision: int
    updated_at: datetime | None


@dataclass(frozen=True)
class ActivationResult:
    """Outcome of an activation attempt; safe for any caller to serialize."""

    activated: bool
    replayed: bool
    snapshot: ActivationSnapshot


def _locked_tenant(db: Session, tenant_id: str) -> Tenant | None:
    return (
        db.query(Tenant)
        .filter(Tenant.id == tenant_id)
        .with_for_update()
        .first()
    )


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def _gate_binding(db: Session, tenant_id: str) -> tuple[bool, str | None]:
    binding = (
        db.query(ThirdPartyOrganizationBinding)
        .filter(ThirdPartyOrganizationBinding.tenant_id == tenant_id)
        .first()
    )
    if binding is None:
        return False, "missing_binding"
    try:
        decrypt_value(binding.permanent_code_encrypted)
    except FieldDecryptionError:
        return False, "credentials_invalid"
    return True, None


def _config_decrypts(config: TenantWecomConfig) -> bool:
    """Prove every stored secret round-trips before any network call."""
    try:
        config.decrypted_app_secret
        if config.private_key_encrypted:
            config.decrypted_private_key
        if config.has_callback_credentials:
            config.decrypted_callback_token
            config.decrypted_callback_encoding_aes_key
    except FieldDecryptionError:
        return False
    return True


def _gate_config(db: Session, tenant_id: str) -> tuple[bool, str | None]:
    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant_id)
        .first()
    )
    if config is None:
        return False, "config_incomplete"
    if not _config_decrypts(config):
        return False, "config_not_decryptable"
    if (
        not config.private_key_encrypted
        or config.publickey_version is None
        or not config.has_callback_credentials
    ):
        return False, "config_incomplete"
    try:
        from cryptography.hazmat.primitives import serialization

        serialization.load_pem_private_key(
            config.decrypted_private_key.encode("utf-8"), password=None
        )
    except (ValueError, TypeError):
        return False, "credentials_invalid"
    return True, None


def _gate_connectivity(
    db: Session, tenant_id: str, config: TenantWecomConfig
) -> tuple[bool, str | None]:
    try:
        from app.auth import get_wecom_token

        get_wecom_token(
            config.corp_id,
            config.decrypted_app_secret,
            cache_key=f"activation-connectivity:{tenant_id}",
        )
    except RuntimeError as error:
        reason = connectivity_failure_reason(error)
        if reason == "invalid_credentials":
            return False, "credentials_invalid"
        return False, "connectivity_failed"
    return True, None


def _gate_subscription(
    db: Session, tenant_id: str, *, at: datetime
) -> tuple[bool, str | None]:
    if get_subscription_summary(db, tenant_id) is None:
        # No subscription row at all: the post-gate trial grant decides, so
        # the gate defers instead of failing (RND-394 wiring below).
        return True, None
    if has_entitlement(db, tenant_id, ARCHIVE_ACCESS, at=at):
        return True, None
    # A subscription that exists but is not entitled (expired/canceled) has
    # already used its trial chance — block without granting another.
    return False, "no_entitlement"


def _callback_resolvable(config: TenantWecomConfig | None) -> bool:
    """Only a complete per-tenant stored callback credential pair is valid."""
    return config is not None and config.has_callback_credentials


def _gate_runtime(
    db: Session, tenant_id: str, config: TenantWecomConfig | None
) -> tuple[bool, str | None]:
    if not os.environ.get("WECOM_SDK_LIB_PATH", "").strip():
        return False, "runtime_unavailable"
    if not _callback_resolvable(config):
        return False, "runtime_unavailable"
    return True, None


def _evaluate_gates(
    db: Session, tenant: Tenant, *, at: datetime
) -> ActivationSnapshot:
    """Run gates in order; gates after the first failure stay absent (the UI
    renders them as skipped).  A ready evaluation may grant the RND-394 trial
    inside the caller's transaction."""
    gate_results: dict[str, dict] = {}
    config: TenantWecomConfig | None = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant.id)
        .first()
    )

    for gate_id in GATE_IDS:
        if gate_id == "binding":
            ok, code = _gate_binding(db, tenant.id)
        elif gate_id == "config":
            ok, code = _gate_config(db, tenant.id)
        elif gate_id == "connectivity":
            ok, code = _gate_connectivity(db, tenant.id, config)
        elif gate_id == "subscription":
            ok, code = _gate_subscription(db, tenant.id, at=at)
        else:
            ok, code = _gate_runtime(db, tenant.id, config)
        gate_results[gate_id] = {"ok": ok, "safe_error_code": code}
        if not ok:
            return ActivationSnapshot(
                state="blocked",
                gate_results=gate_results,
                safe_error_code=code,
                revision=0,
                updated_at=None,
            )

    # All gates pass.  Grant the RND-394 trial when the tenant has no
    # subscription row at all; a used/expired subscription is not our cue to
    # grant anything — the entitlement gate already failed for it.
    if get_subscription_summary(db, tenant.id) is None:
        try:
            grant_self_service_trial(
                db, TrialGrantCommand(tenant_id=tenant.id, trusted_at=at)
            )
        except (TrialNotEligibleError, TrialAlreadyUsedError) as error:
            logger.info(
                "activation tenant=%s trial_grant=declined reason=%s",
                _digest(tenant.id),
                type(error).__name__,
            )
    if not has_entitlement(db, tenant.id, ARCHIVE_ACCESS, at=at):
        gate_results["subscription"] = {
            "ok": False,
            "safe_error_code": "no_entitlement",
        }
        return ActivationSnapshot(
            state="blocked",
            gate_results=gate_results,
            safe_error_code="no_entitlement",
            revision=0,
            updated_at=None,
        )

    return ActivationSnapshot(
        state="ready",
        gate_results=gate_results,
        safe_error_code=None,
        revision=0,
        updated_at=None,
    )


def _digest(tenant_id: str) -> str:
    """A log-safe tenant tag: never log the raw id."""
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _upsert_check_row(
    db: Session, tenant_id: str, snapshot: ActivationSnapshot
) -> ActivationSnapshot:
    row = (
        db.query(TenantActivationCheck)
        .filter(TenantActivationCheck.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        row = TenantActivationCheck(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            state=snapshot.state,
            gate_results=snapshot.gate_results,
            safe_error_code=snapshot.safe_error_code,
            revision=1,
        )
        db.add(row)
        revision = 1
    else:
        row.state = snapshot.state
        row.gate_results = snapshot.gate_results
        row.safe_error_code = snapshot.safe_error_code
        row.revision += 1
        revision = row.revision
    db.flush()
    return ActivationSnapshot(
        state=snapshot.state,
        gate_results=snapshot.gate_results,
        safe_error_code=snapshot.safe_error_code,
        revision=revision,
        updated_at=row.updated_at,
    )


def _persisted_snapshot(db: Session, tenant_id: str) -> ActivationSnapshot | None:
    row = (
        db.query(TenantActivationCheck)
        .filter(TenantActivationCheck.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        return None
    return ActivationSnapshot(
        state=row.state,
        gate_results=row.gate_results,
        safe_error_code=row.safe_error_code,
        revision=row.revision,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_activation(
    db: Session, tenant_id: str, *, at: datetime | None = None
) -> ActivationSnapshot:
    """Row-locked gate evaluation with a replay short-circuit.

    Never promotes.  An already-active tenant returns its snapshot with no
    writes and no network.  Otherwise the checks row is upserted (revision+1)
    and flushed; the caller owns the commit.
    """
    checked_at = at or datetime.now(timezone.utc)
    tenant = _locked_tenant(db, tenant_id)
    if tenant is None:
        raise TenantActivationError(f"tenant not found: {tenant_id}")
    if tenant.lifecycle_status == "active":
        snapshot = _persisted_snapshot(db, tenant_id)
        if snapshot is not None:
            return ActivationSnapshot(
                state="active",
                gate_results=snapshot.gate_results,
                safe_error_code=None,
                revision=snapshot.revision,
                updated_at=snapshot.updated_at,
            )
        return ActivationSnapshot(
            state="active",
            gate_results={},
            safe_error_code=None,
            revision=0,
            updated_at=None,
        )
    snapshot = _evaluate_gates(db, tenant, at=checked_at)
    return _upsert_check_row(db, tenant_id, snapshot)


def activation_status(db: Session, tenant_id: str) -> ActivationSnapshot:
    """Read-only persisted snapshot for status pages.

    Never locks the tenant, re-evaluates gates, or touches the network — the
    polling page must not run a connectivity probe on every tick.  An
    already-active tenant reports state 'active'; a never-evaluated tenant
    reports 'not_started'.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    snapshot = _persisted_snapshot(db, tenant_id)
    if tenant is not None and tenant.lifecycle_status == "active":
        if snapshot is None:
            return ActivationSnapshot(
                state="active",
                gate_results={},
                safe_error_code=None,
                revision=0,
                updated_at=None,
            )
        return ActivationSnapshot(
            state="active",
            gate_results=snapshot.gate_results,
            safe_error_code=None,
            revision=snapshot.revision,
            updated_at=snapshot.updated_at,
        )
    if snapshot is None:
        return ActivationSnapshot(
            state="not_started",
            gate_results={},
            safe_error_code=None,
            revision=0,
            updated_at=None,
        )
    return snapshot


def promote_tenant(
    db: Session,
    tenant: Tenant,
    *,
    actor: str,
    admin_user_id: str | None = None,
    gate_results: dict[str, dict] | None = None,
) -> int:
    """The single promotion routine: lifecycle provisioning→active plus the
    identical session promotion and audit the platform console used to own.

    Returns the number of promoted provisioning sessions.  The caller owns
    the transaction and commit.
    """
    if actor not in GATE_ACTORS:
        actor = "platform"
    promoted_sessions = (
        db.query(AdminSession)
        .filter(
            AdminSession.tenant_id == tenant.id,
            AdminSession.session_scope == "provisioning",
            AdminSession.is_revoked.is_(False),
        )
        .update(
            {AdminSession.session_scope: "admin"},
            synchronize_session=False,
        )
    )
    tenant.lifecycle_status = "active"
    tenant.lifecycle_revision += 1
    tenant.onboarding_completed_at = datetime.now(timezone.utc)
    tenant.frozen_at = None
    detail: dict = {
        "actor": actor,
        "gate_results": gate_results or {},
    }
    if promoted_sessions:
        detail["promoted_provisioning_sessions"] = promoted_sessions
    write_audit(
        db,
        tenant_id=tenant.id,
        action=AuditAction.PLATFORM_TENANT_ACTIVATED,
        object_type=AuditObjectType.TENANT,
        # PlatformAdmin is tenant-less and cannot satisfy this FK.
        admin_user_id=admin_user_id,
        object_id=tenant.id,
        detail=detail,
    )
    db.flush()
    return promoted_sessions


def activate_tenant(
    db: Session,
    tenant_id: str,
    *,
    actor: str,
    admin_user_id: str | None = None,
    require_gates: bool = True,
) -> ActivationResult:
    """The only promoter.  Gated self-service/notify entries evaluate first;
    the platform override (``require_gates=False``) promotes unconditionally,
    preserving the console's authoritative path.

    Holds the tenant row lock for the whole attempt, so a concurrent second
    call replays with no double trial, audit or promotion.  Commits before
    returning.
    """
    checked_at = datetime.now(timezone.utc)
    tenant = _locked_tenant(db, tenant_id)
    if tenant is None:
        db.rollback()
        raise TenantActivationError(f"tenant not found: {tenant_id}")
    if tenant.lifecycle_status == "active":
        db.commit()
        snapshot = _persisted_snapshot(db, tenant_id)
        if snapshot is not None:
            snapshot = ActivationSnapshot(
                state="active",
                gate_results=snapshot.gate_results,
                safe_error_code=None,
                revision=snapshot.revision,
                updated_at=snapshot.updated_at,
            )
        else:
            snapshot = ActivationSnapshot(
                state="active",
                gate_results={},
                safe_error_code=None,
                revision=0,
                updated_at=None,
            )
        return ActivationResult(activated=False, replayed=True, snapshot=snapshot)

    snapshot: ActivationSnapshot | None = None
    if require_gates:
        snapshot = _evaluate_gates(db, tenant, at=checked_at)
        if snapshot.state != "ready":
            snapshot = _upsert_check_row(db, tenant.id, snapshot)
            db.commit()
            return ActivationResult(
                activated=False, replayed=False, snapshot=snapshot
            )

    promote_tenant(
        db,
        tenant,
        actor=actor,
        admin_user_id=admin_user_id,
        gate_results=snapshot.gate_results if snapshot else {},
    )
    if snapshot is not None:
        snapshot = _upsert_check_row(db, tenant.id, snapshot)
    else:
        snapshot = ActivationSnapshot(
            state="ready",
            gate_results={},
            safe_error_code=None,
            revision=0,
            updated_at=None,
        )
    db.commit()
    return ActivationResult(activated=True, replayed=False, snapshot=snapshot)


def spawn_activation_worker(
    db_bind, tenant_id: str, *, actor: str
) -> None:
    """Detached best-effort activation after a config save, config test or
    payment success.  Never blocks or fails the caller's request: a fresh
    session owns its own transaction and the tenant row lock serializes
    concurrent attempts."""
    factory = sessionmaker(bind=db_bind, expire_on_commit=False)

    def _run() -> None:
        try:
            with factory() as db:
                activate_tenant(db, tenant_id, actor=actor)
        except Exception:  # noqa: BLE001 -- best-effort by contract
            logger.error(
                "activation tenant=%s actor=%s trigger=auto-failed",
                _digest(tenant_id),
                actor,
            )

    thread = threading.Thread(
        target=_run,
        name="rnd388-activation-trigger",
        daemon=True,
    )
    thread.start()
