from datetime import timedelta

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


def _default_subscription_grace_ends_at(context):
    """Keep ORM-created rows compatible with the database's seven-day policy."""
    ends_at = context.get_current_parameters().get("ends_at")
    return ends_at + timedelta(days=7) if ends_at is not None else None


class DuplicateCorpIdError(ValueError):
    """Raised when a TenantWecomConfig write would assign an active corp_id
    to more than one tenant. See RND-184."""


class Tenant(Base):
    """Top-level tenant entity. One row per company in future SaaS; one default row for MVP."""

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_tenants_slug"),
        CheckConstraint(
            "lifecycle_status IN ('provisioning', 'active', 'frozen', 'suspended')",
            name="ck_tenants_lifecycle_status",
        ),
        CheckConstraint(
            "lifecycle_revision >= 1",
            name="ck_tenants_lifecycle_revision",
        ),
        CheckConstraint(
            "suspension_previous_status IS NULL OR "
            "suspension_previous_status IN ('provisioning', 'active', 'frozen')",
            name="ck_tenants_suspension_previous_status",
        ),
    )

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    lifecycle_status = Column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    lifecycle_revision = Column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    frozen_at = Column(DateTime(timezone=True), nullable=True)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    suspension_reason = Column(String(255), nullable=True)
    suspended_by_platform_admin_id = Column(String(36), nullable=True)
    suspension_previous_status = Column(String(16), nullable=True)
    onboarding_completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantBranding(Base):
    """One tenant-scoped paid white-label configuration (RND-259).

    Image bytes stay on this tenant-owned row instead of in a shared public
    object namespace.  ``custom_domain`` is globally unique and is only
    served after the application-level entitlement, verification, certificate
    and Host gates all pass.
    """

    __tablename__ = "tenant_branding"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_branding_tenant"),
        UniqueConstraint("custom_domain", name="uq_tenant_branding_custom_domain"),
        CheckConstraint(
            "domain_state IS NULL OR domain_state IN "
            "('pending_verification', 'verified', 'active')",
            name="ck_tenant_branding_domain_state",
        ),
        CheckConstraint(
            "certificate_status IN ('not_requested', 'pending', 'issued', 'failed', 'expired')",
            name="ck_tenant_branding_certificate_status",
        ),
        CheckConstraint(
            "(logo_content IS NULL) = (logo_mime_type IS NULL)",
            name="ck_tenant_branding_logo_pair",
        ),
        CheckConstraint(
            "(favicon_content IS NULL) = (favicon_mime_type IS NULL)",
            name="ck_tenant_branding_favicon_pair",
        ),
        Index("ix_tenant_branding_custom_domain", "custom_domain"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    logo_content = Column(LargeBinary, nullable=True)
    logo_mime_type = Column(String(32), nullable=True)
    logo_updated_at = Column(DateTime(timezone=True), nullable=True)
    favicon_content = Column(LargeBinary, nullable=True)
    favicon_mime_type = Column(String(32), nullable=True)
    favicon_updated_at = Column(DateTime(timezone=True), nullable=True)
    custom_domain = Column(String(253), nullable=True)
    domain_state = Column(String(32), nullable=True)
    domain_enabled = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Hash only.  Raw TXT values are returned once to the tenant administrator
    # and never retained, logged, placed in audit detail, or returned later.
    verification_token_hash = Column(String(64), nullable=True)
    verification_requested_at = Column(DateTime(timezone=True), nullable=True)
    verification_verified_at = Column(DateTime(timezone=True), nullable=True)
    domain_last_checked_at = Column(DateTime(timezone=True), nullable=True)
    certificate_status = Column(
        String(32), nullable=False, default="not_requested", server_default=text("'not_requested'")
    )
    certificate_expires_at = Column(DateTime(timezone=True), nullable=True)
    certificate_last_checked_at = Column(DateTime(timezone=True), nullable=True)
    # Coarse controller/DNS code only; never store raw provider errors, keys,
    # certificate material, or ownership-token values.
    domain_failure_code = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BillingPlan(Base):
    """Server-authoritative commercial plan definition (RND-376)."""

    __tablename__ = "billing_plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_billing_plans_code"),
        CheckConstraint("amount_cents >= 0", name="ck_billing_plans_amount"),
        CheckConstraint("length(currency) = 3", name="ck_billing_plans_currency"),
        CheckConstraint(
            "billing_period_months > 0",
            name="ck_billing_plans_period_months",
        ),
        CheckConstraint(
            "storage_quota_bytes >= 0",
            name="ck_billing_plans_storage_quota",
        ),
    )

    id = Column(String(36), primary_key=True)
    code = Column(String(64), nullable=False)
    display_name = Column(String(128), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    billing_period_months = Column(Integer, nullable=False)
    storage_quota_bytes = Column(BigInteger, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PlanEntitlement(Base):
    """Normalized boolean capability attached to a billing plan."""

    __tablename__ = "plan_entitlements"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "capability",
            name="uq_plan_entitlements_plan_capability",
        ),
    )

    id = Column(String(36), primary_key=True)
    plan_id = Column(
        String(36),
        ForeignKey("billing_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    capability = Column(String(64), nullable=False)
    is_enabled = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Subscription(Base):
    """The single authoritative current subscription for one tenant."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_subscriptions_tenant"),
        CheckConstraint(
            "status IN ('trial', 'active', 'grace', 'expired', 'canceled')",
            name="ck_subscriptions_status",
        ),
        CheckConstraint("starts_at < ends_at", name="ck_subscriptions_date_range"),
        CheckConstraint(
            "ends_at < grace_ends_at",
            name="ck_subscriptions_grace_date_range",
        ),
        CheckConstraint("renewal_count >= 0", name="ck_subscriptions_renewal_count"),
        CheckConstraint("revision >= 1", name="ck_subscriptions_revision"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=False
    )
    plan_id = Column(String(36), ForeignKey("billing_plans.id"), nullable=False)
    status = Column(String(16), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    grace_ends_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_default_subscription_grace_ends_at,
    )
    cancel_at_period_end = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source = Column(String(32), nullable=False)
    renewal_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SubscriptionHistory(Base):
    """Append-only snapshot for every authoritative subscription assignment."""

    __tablename__ = "subscription_history"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "revision",
            name="uq_subscription_history_revision",
        ),
        CheckConstraint(
            "status IN ('trial', 'active', 'grace', 'expired', 'canceled')",
            name="ck_subscription_history_status",
        ),
        CheckConstraint(
            "starts_at < ends_at", name="ck_subscription_history_date_range"
        ),
        CheckConstraint(
            "ends_at < grace_ends_at",
            name="ck_subscription_history_grace_date_range",
        ),
        CheckConstraint(
            "renewal_count >= 0", name="ck_subscription_history_renewal_count"
        ),
        CheckConstraint("revision >= 1", name="ck_subscription_history_revision"),
    )

    id = Column(String(36), primary_key=True)
    subscription_id = Column(
        String(36), ForeignKey("subscriptions.id"), nullable=False
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    plan_id = Column(String(36), ForeignKey("billing_plans.id"), nullable=False)
    status = Column(String(16), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    grace_ends_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_default_subscription_grace_ends_at,
    )
    cancel_at_period_end = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source = Column(String(32), nullable=False)
    renewal_count = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    change_kind = Column(String(32), nullable=False)
    recorded_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionActivation(Base):
    """Durable, provider-neutral idempotency record for paid activations."""

    __tablename__ = "subscription_activations"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "idempotency_key_hash",
            name="uq_subscription_activations_source_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_subscription_activations_status",
        ),
        CheckConstraint(
            "activation_kind IS NULL OR activation_kind IN ('activation', 'renewal')",
            name="ck_subscription_activations_kind",
        ),
        CheckConstraint(
            "subscription_revision IS NULL OR subscription_revision >= 1",
            name="ck_subscription_activations_revision",
        ),
        CheckConstraint(
            "applied_renewal_count IS NULL OR applied_renewal_count >= 0",
            name="ck_subscription_activations_renewal_count",
        ),
        CheckConstraint(
            "status != 'failed' OR failure_code IS NOT NULL",
            name="ck_subscription_activations_failed_code",
        ),
        CheckConstraint(
            "status != 'applied' OR ("
            "subscription_id IS NOT NULL AND subscription_revision IS NOT NULL AND "
            "applied_renewal_count IS NOT NULL AND "
            "activation_kind IS NOT NULL AND applied_starts_at IS NOT NULL AND "
            "applied_ends_at IS NOT NULL AND applied_at IS NOT NULL)",
            name="ck_subscription_activations_applied_result",
        ),
        Index("ix_subscription_activations_tenant_created", "tenant_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    plan_code = Column(String(64), nullable=False)
    source = Column(String(32), nullable=False)
    idempotency_key_hash = Column(String(64), nullable=False)
    command_hash = Column(String(64), nullable=False)
    trusted_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'pending'"))
    failure_code = Column(String(32), nullable=True)
    subscription_id = Column(
        String(36), ForeignKey("subscriptions.id"), nullable=True
    )
    subscription_revision = Column(Integer, nullable=True)
    applied_renewal_count = Column(Integer, nullable=True)
    activation_kind = Column(String(16), nullable=True)
    applied_starts_at = Column(DateTime(timezone=True), nullable=True)
    applied_ends_at = Column(DateTime(timezone=True), nullable=True)
    applied_grace_ends_at = Column(DateTime(timezone=True), nullable=True)
    # RND-399: exact pre-payment snapshot. NULL on legacy applied rows means
    # the historical state is unknowable and any refund must fail to manual
    # recovery rather than guessing.
    prior_subscription_existed = Column(Boolean, nullable=True)
    prior_plan_id = Column(String(36), ForeignKey("billing_plans.id"), nullable=True)
    prior_status = Column(String(16), nullable=True)
    prior_starts_at = Column(DateTime(timezone=True), nullable=True)
    prior_ends_at = Column(DateTime(timezone=True), nullable=True)
    prior_grace_ends_at = Column(DateTime(timezone=True), nullable=True)
    prior_cancel_at_period_end = Column(Boolean, nullable=True)
    prior_source = Column(String(32), nullable=True)
    prior_renewal_count = Column(Integer, nullable=True)
    prior_revision = Column(Integer, nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PaymentOrder(Base):
    """Provider-neutral, server-authoritative annual-plan purchase order."""

    __tablename__ = "payment_orders"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_order_ref",
            name="uq_payment_orders_provider_ref",
        ),
        UniqueConstraint(
            "provider",
            "provider_transaction_id",
            name="uq_payment_orders_provider_transaction",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_payment_orders_tenant_idempotency",
        ),
        CheckConstraint("amount_cents > 0", name="ck_payment_orders_amount"),
        CheckConstraint("length(currency) = 3", name="ck_payment_orders_currency"),
        CheckConstraint(
            "status IN ('creating', 'pending', 'paid_activation_pending', "
            "'succeeded', 'closed', 'failed')",
            name="ck_payment_orders_status",
        ),
        CheckConstraint("created_at < expires_at", name="ck_payment_orders_expiry"),
        CheckConstraint(
            "status NOT IN ('paid_activation_pending', 'succeeded') OR ("
            "paid_at IS NOT NULL AND provider_transaction_id IS NOT NULL)",
            name="ck_payment_orders_paid_result",
        ),
        CheckConstraint(
            "status != 'succeeded' OR ("
            "activation_id IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_payment_orders_activation_result",
        ),
        CheckConstraint(
            "status != 'failed' OR failure_code IS NOT NULL",
            name="ck_payment_orders_failed_code",
        ),
        Index("ix_payment_orders_tenant_created", "tenant_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    plan_id = Column(String(36), ForeignKey("billing_plans.id"), nullable=False)
    plan_code = Column(String(64), nullable=False)
    plan_name = Column(String(128), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    provider = Column(String(32), nullable=False)
    provider_order_ref = Column(String(64), nullable=False)
    provider_transaction_id = Column(String(64), nullable=True)
    provider_state = Column(String(32), nullable=True)
    status = Column(String(32), nullable=False, server_default=text("'creating'"))
    checkout_url = Column(Text, nullable=True)
    idempotency_key_hash = Column(String(64), nullable=False)
    activation_id = Column(
        String(36), ForeignKey("subscription_activations.id"), nullable=True
    )
    failure_code = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ManualFinancialTransaction(Base):
    """Platform-recorded receipt or refund, separate from provider payment facts.

    Provider-confirmed receipts stay in ``payment_orders``. This table records
    only an operator's manual collection/refund entry, so it can never be
    mistaken for a verified provider callback or influence subscription access.
    """

    __tablename__ = "manual_financial_transactions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('receipt', 'refund')",
            name="ck_manual_financial_transactions_kind",
        ),
        CheckConstraint(
            "amount_cents > 0",
            name="ck_manual_financial_transactions_amount",
        ),
        CheckConstraint(
            "length(currency) = 3",
            name="ck_manual_financial_transactions_currency",
        ),
        Index(
            "ix_manual_financial_transactions_tenant_occurred",
            "tenant_id",
            "occurred_at",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    kind = Column(String(16), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, server_default=text("'CNY'"))
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    reference = Column(String(128), nullable=True)
    note = Column(Text, nullable=True)
    recorded_by_platform_admin_id = Column(
        String(36), ForeignKey("platform_admins.id"), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaymentEvent(Base):
    """Minimal durable evidence for one verified provider payment fact."""

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_event_id", name="uq_payment_events_provider_event"
        ),
        CheckConstraint(
            "source IN ('callback', 'query')", name="ck_payment_events_source"
        ),
        CheckConstraint(
            "length(payload_hash) = 64", name="ck_payment_events_payload_hash"
        ),
        Index("ix_payment_events_order_created", "order_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    order_id = Column(String(36), ForeignKey("payment_orders.id"), nullable=False)
    provider = Column(String(32), nullable=False)
    provider_event_id = Column(String(128), nullable=False)
    provider_transaction_id = Column(String(64), nullable=False)
    event_type = Column(String(32), nullable=False)
    source = Column(String(16), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionTermGrant(Base):
    """One exact subscription projection added by one trusted payment."""

    __tablename__ = "subscription_term_grants"
    __table_args__ = (
        UniqueConstraint("payment_order_id", name="uq_term_grants_payment_order"),
        UniqueConstraint("activation_id", name="uq_term_grants_activation"),
        CheckConstraint(
            "status IN ('active', 'reversed', 'manual_recovery_required')",
            name="ck_term_grants_status",
        ),
        CheckConstraint(
            "activation_kind IN ('activation', 'renewal')",
            name="ck_term_grants_activation_kind",
        ),
        CheckConstraint(
            "applied_starts_at < applied_ends_at AND "
            "applied_ends_at < applied_grace_ends_at",
            name="ck_term_grants_applied_range",
        ),
        CheckConstraint(
            "applied_renewal_count >= 0 AND applied_revision >= 1",
            name="ck_term_grants_applied_counters",
        ),
        CheckConstraint(
            "status != 'manual_recovery_required' OR failure_code IS NOT NULL",
            name="ck_term_grants_manual_failure",
        ),
        Index("ix_term_grants_tenant_created", "tenant_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    payment_order_id = Column(
        String(36), ForeignKey("payment_orders.id"), nullable=False
    )
    activation_id = Column(
        String(36), ForeignKey("subscription_activations.id"), nullable=False
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    subscription_id = Column(
        String(36), ForeignKey("subscriptions.id"), nullable=False
    )
    status = Column(String(32), nullable=False)
    failure_code = Column(String(64), nullable=True)
    activation_kind = Column(String(16), nullable=False)
    prior_subscription_existed = Column(Boolean, nullable=True)
    prior_plan_id = Column(String(36), ForeignKey("billing_plans.id"), nullable=True)
    prior_status = Column(String(16), nullable=True)
    prior_starts_at = Column(DateTime(timezone=True), nullable=True)
    prior_ends_at = Column(DateTime(timezone=True), nullable=True)
    prior_grace_ends_at = Column(DateTime(timezone=True), nullable=True)
    prior_cancel_at_period_end = Column(Boolean, nullable=True)
    prior_source = Column(String(32), nullable=True)
    prior_renewal_count = Column(Integer, nullable=True)
    prior_revision = Column(Integer, nullable=True)
    applied_plan_id = Column(
        String(36), ForeignKey("billing_plans.id"), nullable=False
    )
    applied_status = Column(String(16), nullable=False)
    applied_starts_at = Column(DateTime(timezone=True), nullable=False)
    applied_ends_at = Column(DateTime(timezone=True), nullable=False)
    applied_grace_ends_at = Column(DateTime(timezone=True), nullable=False)
    applied_cancel_at_period_end = Column(Boolean, nullable=False)
    applied_source = Column(String(32), nullable=False)
    applied_renewal_count = Column(Integer, nullable=False)
    applied_revision = Column(Integer, nullable=False)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RefundOrder(Base):
    """Authoritative full-refund workflow, separate from manual finance rows."""

    __tablename__ = "refund_orders"
    __table_args__ = (
        UniqueConstraint("payment_order_id", name="uq_refund_orders_payment_order"),
        UniqueConstraint("term_grant_id", name="uq_refund_orders_term_grant"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_refund_orders_tenant_idempotency",
        ),
        UniqueConstraint(
            "provider", "provider_ref", name="uq_refund_orders_provider_ref"
        ),
        UniqueConstraint(
            "provider",
            "provider_refund_id",
            name="uq_refund_orders_provider_refund_id",
        ),
        CheckConstraint("amount_cents > 0", name="ck_refund_orders_amount"),
        CheckConstraint("length(currency) = 3", name="ck_refund_orders_currency"),
        CheckConstraint(
            "status IN ('created', 'processing', 'succeeded', 'closed', "
            "'abnormal', 'manual_recovery_required')",
            name="ck_refund_orders_status",
        ),
        CheckConstraint(
            "status != 'succeeded' OR "
            "(succeeded_at IS NOT NULL AND entitlement_reversed_at IS NOT NULL)",
            name="ck_refund_orders_succeeded_result",
        ),
        CheckConstraint(
            "status NOT IN ('abnormal', 'manual_recovery_required') OR "
            "failure_code IS NOT NULL",
            name="ck_refund_orders_failure_code",
        ),
        Index("ix_refund_orders_tenant_created", "tenant_id", "requested_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    payment_order_id = Column(
        String(36), ForeignKey("payment_orders.id"), nullable=False
    )
    term_grant_id = Column(
        String(36), ForeignKey("subscription_term_grants.id"), nullable=False
    )
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    provider = Column(String(32), nullable=False)
    provider_ref = Column(String(64), nullable=True)
    provider_refund_id = Column(String(64), nullable=True)
    provider_state = Column(String(32), nullable=True)
    status = Column(String(32), nullable=False)
    reason_code = Column(String(64), nullable=False)
    approved_by_platform_admin_id = Column(
        String(36), ForeignKey("platform_admins.id"), nullable=False
    )
    idempotency_key_hash = Column(String(64), nullable=False)
    failure_code = Column(String(64), nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=False)
    provider_accepted_at = Column(DateTime(timezone=True), nullable=True)
    succeeded_at = Column(DateTime(timezone=True), nullable=True)
    entitlement_reversed_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RefundEvent(Base):
    """Append-only trusted provider refund fact."""

    __tablename__ = "refund_events"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_event_id", name="uq_refund_events_provider_event"
        ),
        CheckConstraint(
            "source IN ('callback', 'query')", name="ck_refund_events_source"
        ),
        CheckConstraint(
            "state IN ('PROCESSING', 'SUCCESS', 'CLOSED', 'ABNORMAL')",
            name="ck_refund_events_state",
        ),
        CheckConstraint("amount_cents > 0", name="ck_refund_events_amount"),
        CheckConstraint("length(currency) = 3", name="ck_refund_events_currency"),
        CheckConstraint(
            "length(payload_hash) = 64", name="ck_refund_events_payload_hash"
        ),
        Index("ix_refund_events_refund_created", "refund_order_id", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    refund_order_id = Column(
        String(36), ForeignKey("refund_orders.id"), nullable=False
    )
    provider = Column(String(32), nullable=False)
    provider_event_id = Column(String(128), nullable=False)
    provider_ref = Column(String(64), nullable=False)
    provider_refund_id = Column(String(64), nullable=True)
    provider_order_ref = Column(String(64), nullable=True)
    provider_transaction_id = Column(String(64), nullable=True)
    state = Column(String(16), nullable=False)
    source = Column(String(16), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingNotificationIntent(Base):
    """Durable, deduplicated instruction to send one billing-state notice."""

    __tablename__ = "billing_notification_intents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "dedupe_key",
            name="uq_billing_notification_intents_tenant_dedupe",
        ),
        CheckConstraint(
            "subject_type IN ('subscription', 'payment_order', 'refund_order')",
            name="ck_billing_notification_intents_subject_type",
        ),
        CheckConstraint(
            "audience IN ('owner', 'operations')",
            name="ck_billing_notification_intents_audience",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'canceled', 'failed')",
            name="ck_billing_notification_intents_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_billing_notification_intents_attempt_count",
        ),
        CheckConstraint(
            "length(context_key) = 64 AND length(dedupe_key) = 64",
            name="ck_billing_notification_intents_hashes",
        ),
        Index(
            "ix_billing_notification_intents_due",
            "status",
            "next_attempt_at",
            "scheduled_at",
        ),
        Index(
            "ix_billing_notification_intents_tenant_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    subject_type = Column(String(32), nullable=False)
    subject_id = Column(String(36), nullable=False)
    kind = Column(String(64), nullable=False)
    audience = Column(String(16), nullable=False)
    context_key = Column(String(64), nullable=False)
    dedupe_key = Column(String(64), nullable=False)
    source_revision = Column(Integer, nullable=True)
    effective_at = Column(DateTime(timezone=True), nullable=False)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=False)
    cancellation_code = Column(String(64), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BillingNotificationAttempt(Base):
    """Append-only, sanitized result of one notification delivery attempt."""

    __tablename__ = "billing_notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "attempt_no",
            name="uq_billing_notification_attempts_intent_number",
        ),
        CheckConstraint(
            "outcome IN ('sent', 'failed')",
            name="ck_billing_notification_attempts_outcome",
        ),
        CheckConstraint(
            "attempt_no >= 1",
            name="ck_billing_notification_attempts_number",
        ),
        CheckConstraint(
            "outcome != 'failed' OR failure_code IS NOT NULL",
            name="ck_billing_notification_attempts_failure_code",
        ),
        Index(
            "ix_billing_notification_attempts_tenant_attempted",
            "tenant_id",
            "attempted_at",
        ),
    )

    id = Column(String(36), primary_key=True)
    intent_id = Column(
        String(36), ForeignKey("billing_notification_intents.id"), nullable=False
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    outcome = Column(String(16), nullable=False)
    failure_code = Column(String(64), nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False)


class TenantWecomConfig(Base):
    """Per-tenant WeCom app credentials. One row per tenant for MVP.

    app_secret and private_key_encrypted are Fernet ciphertext at rest.
    Do NOT log either credential or their decrypted accessors.
    """

    __tablename__ = "tenant_wecom_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_wecom_configs_tenant"),
        # A given WeCom corp_id must resolve to exactly one active tenant.
        # Partial index — inactive/disabled configs are exempt so a corp_id
        # can be freely reassigned after the old config is deactivated.
        Index(
            "uq_tenant_wecom_configs_active_corp_id",
            "corp_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36),
        ForeignKey("tenants.id"),
        nullable=False,
    )
    corp_id = Column(String(64), nullable=False)
    agent_id = Column(String(64), nullable=False)
    app_secret = Column(Text, nullable=False)
    # RND-311 (B2-1): encrypted PEM private key; nullable for existing rows.
    private_key_encrypted = Column(Text, nullable=True)
    # RND-386 (T1): per-tenant session-archive callback credentials, Fernet
    # ciphertext at rest. Nullable — the legacy single-corp deployment keeps
    # using env-scoped WECOM_CALLBACK_TOKEN/AESKey.
    callback_token_encrypted = Column(Text, nullable=True)
    callback_encoding_aes_key_encrypted = Column(Text, nullable=True)
    publickey_version = Column(Integer, nullable=True)

    def set_app_secret(self, plain: str) -> None:
        """Encrypt and assign the permanent WeCom app credential for storage."""
        # Lazy import keeps this ORM model independent of crypto module import
        # order and avoids circular imports during application startup.
        from app.crypto import encrypt_value

        self.app_secret = encrypt_value(plain)

    @property
    def decrypted_app_secret(self) -> str:
        """Return the stored WeCom app credential; never log this value."""
        # See set_app_secret() for why this import intentionally stays local.
        from app.crypto import decrypt_value

        return decrypt_value(self.app_secret)

    def set_credentials(self, secret: str, private_key_pem: str) -> None:
        """Encrypt and assign the WeCom secret and RSA PEM private key."""
        from app.crypto import encrypt_value

        self.app_secret = encrypt_value(secret)
        self.private_key_encrypted = encrypt_value(private_key_pem)

    @property
    def decrypted_private_key(self) -> str:
        """Return the stored RSA PEM private key; never log this value."""
        from app.crypto import decrypt_value

        return decrypt_value(self.private_key_encrypted)

    def set_private_key(self, private_key_pem: str) -> None:
        """Encrypt and assign only the RSA PEM private key."""
        from app.crypto import encrypt_value

        self.private_key_encrypted = encrypt_value(private_key_pem)

    def set_callback_credentials(self, token: str, aes_key: str) -> None:
        """Encrypt and assign the per-tenant callback Token/EncodingAESKey."""
        from app.crypto import encrypt_value

        self.callback_token_encrypted = encrypt_value(token)
        self.callback_encoding_aes_key_encrypted = encrypt_value(aes_key)

    @property
    def decrypted_callback_token(self) -> str:
        """Return the stored callback Token; never log this value."""
        from app.crypto import decrypt_value

        return decrypt_value(self.callback_token_encrypted)

    @property
    def decrypted_callback_encoding_aes_key(self) -> str:
        """Return the stored callback EncodingAESKey; never log this value."""
        from app.crypto import decrypt_value

        return decrypt_value(self.callback_encoding_aes_key_encrypted)

    @property
    def has_callback_credentials(self) -> bool:
        """True when per-tenant callback credentials are fully stored."""
        return bool(
            self.callback_token_encrypted
            and self.callback_encoding_aes_key_encrypted
        )

    callback_domain = Column(String(255), nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WecomAuthorizationAttempt(Base):
    """Server-side, one-use CSRF state for a third-party app installation."""

    __tablename__ = "wecom_authorization_attempts"

    id = Column(String(36), primary_key=True)
    state_hash = Column(String(64), nullable=False, unique=True)
    status = Column(String(16), nullable=False, server_default=text("'pending'"))
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WecomSuiteTicketState(Base):
    """Encrypted, authoritative latest suite_ticket for one provider suite."""

    __tablename__ = "wecom_suite_ticket_states"
    __table_args__ = (
        CheckConstraint(
            "source_timestamp > 0",
            name="ck_wecom_suite_ticket_source_timestamp_positive",
        ),
    )

    suite_id = Column(String(128), primary_key=True)
    ticket_encrypted = Column(Text, nullable=False)
    ticket_digest = Column(String(64), nullable=False)
    source_timestamp = Column(BigInteger, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WecomAuthorizationProof(Base):
    """Short-lived trusted organization evidence; never exposed to a client."""

    __tablename__ = "wecom_authorization_proofs"
    __table_args__ = (
        CheckConstraint("authorization_mode = 'admin'", name="ck_wecom_auth_proof_admin_mode"),
    )

    id = Column(String(36), primary_key=True)
    browser_token_hash = Column(String(64), nullable=False, unique=True)
    corp_id = Column(String(64), nullable=False)
    corp_name = Column(String(255), nullable=False)
    authorized_subject = Column(String(128), nullable=False)
    agent_id = Column(String(64), nullable=True)
    authorization_mode = Column(String(16), nullable=False)
    permanent_code_encrypted = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, server_default=text("'pending'"))
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WecomOrganizationClaim(Base):
    """Browser-bound, minimal handoff from trusted proof to provisioning."""

    __tablename__ = "wecom_organization_claims"

    id = Column(String(36), primary_key=True)
    public_ref_hash = Column(String(64), nullable=False, unique=True)
    corp_id = Column(String(64), nullable=False)
    corp_name = Column(String(255), nullable=False)
    authorized_subject = Column(String(128), nullable=False)
    agent_id = Column(String(64), nullable=True)
    permanent_code_encrypted = Column(Text, nullable=False)
    state = Column(String(16), nullable=False, server_default=text("'pending'"))
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    provisioned_tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=True)
    provisioning_session_id = Column(String(36), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ThirdPartyOrganizationBinding(Base):
    """Authorized third-party app installation, separate from archive config."""

    __tablename__ = "third_party_organization_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_third_party_binding_tenant"),
        UniqueConstraint("corp_id", name="uq_third_party_binding_corp"),
        CheckConstraint(
            "authorization_mode = 'admin'",
            name="ck_third_party_binding_admin_mode",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    corp_id = Column(String(64), nullable=False)
    agent_id = Column(String(64), nullable=True)
    permanent_code_encrypted = Column(Text, nullable=False)
    authorization_mode = Column(String(16), nullable=False, server_default=text("'admin'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RetentionConfig(Base):
    """Per-tenant message retention policy (RND-318)."""

    __tablename__ = "retention_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_retention_configs_tenant"),
        CheckConstraint(
            "retention_days >= 1 AND retention_days <= 3650",
            name="ck_retention_configs_retention_days_range",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    retention_days = Column(Integer, nullable=False)
    is_locked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AppConfigStore(Base):
    """Application-level configuration values for this deployment (RND-246)."""

    __tablename__ = "app_config_store"

    key = Column(String, primary_key=True)
    group = Column(String, nullable=False)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    value_type = Column(String, nullable=False)
    requires_restart = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by = Column(String, nullable=True)


def _reject_duplicate_active_corp_id(connection, target: "TenantWecomConfig") -> None:
    """Application-level guard mirroring uq_tenant_wecom_configs_active_corp_id.

    Runs on every ORM insert/update of TenantWecomConfig so callers get a
    clear error before the DB constraint would reject the write. Inactive
    configs are exempt — a corp_id may be reassigned once its old config is
    deactivated.
    """
    if not target.is_active:
        return

    conflict = connection.execute(
        select(TenantWecomConfig.__table__.c.id).where(
            TenantWecomConfig.__table__.c.corp_id == target.corp_id,
            TenantWecomConfig.__table__.c.is_active.is_(True),
            TenantWecomConfig.__table__.c.id != target.id,
        )
    ).first()
    if conflict is not None:
        raise DuplicateCorpIdError(
            "This WeCom CorpID is already assigned to another tenant."
        )


@event.listens_for(TenantWecomConfig, "before_insert")
def _validate_corp_id_on_insert(mapper, connection, target: TenantWecomConfig) -> None:
    _reject_duplicate_active_corp_id(connection, target)


@event.listens_for(TenantWecomConfig, "before_update")
def _validate_corp_id_on_update(mapper, connection, target: TenantWecomConfig) -> None:
    _reject_duplicate_active_corp_id(connection, target)


class AdminUser(Base):
    """WeCom employees who have authenticated via OAuth. Created on first login."""

    __tablename__ = "admin_users"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "wecom_user_id", name="uq_admin_users_tenant_wecom"
        ),
        # RND-191: pg_trgm GIN indexes so search_contacts' ILIKE '%term%'
        # predicates (app/routers/search.py) are index-backed instead of a
        # sequential scan. See migration 0013.
        Index(
            "ix_admin_users_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_admin_users_wecom_user_id_trgm",
            "wecom_user_id",
            postgresql_using="gin",
            postgresql_ops={"wecom_user_id": "gin_trgm_ops"},
        ),
        Index("ix_admin_users_invite_token", "invite_token"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    wecom_user_id = Column(String(64), nullable=False)
    name = Column(Text, nullable=True)
    avatar_url = Column(Text, nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # RND-277 (F0-1) account system fields.
    password_hash = Column(Text, nullable=True)
    role = Column(
        Enum(
            "owner",
            "admin",
            "compliance",
            "legal",
            "readonlyaudit",
            name="admin_user_role",
        ),
        nullable=False,
        server_default=text("'admin'"),
    )
    status = Column(
        Enum("active", "disabled", name="admin_user_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    email = Column(Text, nullable=True)
    phone = Column(Text, nullable=True)
    department = Column(Text, nullable=True)
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    invite_token = Column(Text, nullable=True)
    invited_by = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    invite_status = Column(Text, nullable=True)

    # RND-297 (A8-2): persisted UI appearance and language preferences.
    ui_theme = Column(String(16), nullable=False, server_default=text("'light'"))
    ui_locale = Column(String(16), nullable=False, server_default=text("'zh-CN'"))


class AdminSession(Base):
    """Active admin login sessions. session_id (cookie value) is the PK.

    is_revoked: set on logout or forced expiry.
    expires_at: hard TTL enforced server-side on every request.
    Phase 2 (RND-110) implements get_current_user() dependency that reads this table.
    """

    __tablename__ = "admin_sessions"
    __table_args__ = (
        Index("ix_admin_sessions_expires_at", "expires_at"),
        CheckConstraint(
            "session_scope IN ('admin', 'provisioning')",
            name="ck_admin_sessions_scope",
        ),
    )

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    wecom_user_id = Column(String(64), nullable=False)
    session_scope = Column(
        String(16), nullable=False, default="admin", server_default=text("'admin'")
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_revoked = Column(Boolean, nullable=False, default=False)


class AdminLoginIdentity(Base):
    """RND-321: which AdminUser a verified external login identity (a
    WeCom UserId, scoped to one tenant) is authorized to sign a session
    for.

    This is the source of truth for "is this scanned/OAuth'd identity
    allowed in" — a callback looks up (tenant_id, provider, subject) here,
    never by scanning AdminUser.wecom_user_id directly. That legacy column
    is kept in sync as a read-only compatibility field only *after* a bind
    succeeds here (see `_bind_login_identity` in app/routers/auth.py),
    never the other way around, so it can never become a second, drifting
    source of truth for who is allowed to log in.
    """

    __tablename__ = "admin_login_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "subject",
            name="uq_login_identity_tenant_provider_subject",
        ),
        # At most one identity of a given provider per account. Nothing in
        # this ticket's scope needs an account to hold two WeCom identities;
        # relaxing this later needs its own tested reason, not a silent
        # side effect of some other change.
        UniqueConstraint(
            "admin_user_id", "provider",
            name="uq_login_identity_admin_user_provider",
        ),
        Index("ix_admin_login_identities_admin_user_id", "admin_user_id"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdminAccessRequest(Base):
    """RND-321: a verified-but-unbound external identity asking for
    console access. No AdminUser exists for this yet — an owner/admin must
    explicitly link it to an existing account or create a new one before
    any session can ever be issued for it.

    The partial unique index enforces "one open request per identity" —
    repeat scans while pending find and return the same row — without
    blocking a *new* request once the previous one is resolved (linked or
    used to create an account), since by then the identity is bound and
    this code path is never reached for it again.
    """

    __tablename__ = "admin_access_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_admin_access_requests_status_valid",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('linked', 'created')",
            name="ck_admin_access_requests_resolution_valid",
        ),
        Index("ix_admin_access_requests_tenant_status", "tenant_id", "status"),
        Index(
            "uq_admin_access_requests_pending_tenant_provider_subject",
            "tenant_id", "provider", "subject",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    # Human-review clues only — never used for automatic matching/binding.
    display_name = Column(Text, nullable=True)
    email_hint = Column(Text, nullable=True)
    status = Column(
        Enum("pending", "resolved", name="admin_access_request_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    resolution = Column(Text, nullable=True)
    resolved_admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    resolved_by_admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AuditLog(Base):
    """Immutable, append-only audit trail (RND-293 / A7-1).

    Records admin actions for compliance evidence. There is NO update/delete
    path at the application layer — rows are written once (A7-2) and read
    (A7-3) but never mutated. `detail` holds structured context only; it
    MUST NOT contain message bodies or decrypted payloads (SF-1).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_id", "tenant_id"),
        Index("ix_audit_logs_admin_user_id", "admin_user_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=False
    )
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=True, index=False
    )
    action = Column(Text, nullable=False)
    object_type = Column(Text, nullable=False)
    object_id = Column(Text, nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# —— RND-306 (B1-1) 平台超管实体（独立于 admin_users，tenant-less）——
class PlatformAdmin(Base):
    """Platform super-admin, isolated from per-tenant admin_users.

    Tenant-less by design: a platform admin operates across all tenants
    (cross-tenant scope is enforced at the auth layer, see B1-2 / RND-305).
    """

    __tablename__ = "platform_admins"
    __table_args__ = (
        UniqueConstraint("email", name="uq_platform_admins_email"),
        Index("ix_platform_admins_status", "status"),
    )

    id = Column(String(36), primary_key=True)
    email = Column(Text, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(
        Enum("superadmin", name="platform_admin_role"),
        nullable=False,
        server_default=text("'superadmin'"),
    )
    status = Column(
        Enum("active", "disabled", name="platform_admin_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at = Column(DateTime(timezone=True), nullable=True)


# —— RND-278 (F0-3) 密码重置令牌 ——
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    # Stores only the SHA-256 hex digest of the raw token. The raw token is
    # limited to the reset email link and the browser URL.
    token = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# —— RND-316 (C2-2) 导出安全审批令牌 ——
class ExportApprovalToken(Base):
    __tablename__ = "export_approval_tokens"

    id = Column(String(36), primary_key=True)
    admin_user_id = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # Only the SHA-256 hex digest is persisted; the raw token is returned once.
    token = Column(String(64), nullable=False, unique=True, index=True)
    # SHA-256 of canonical export parameters binds approval to one request.
    params_hash = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# —— RND-360 会话存档导出与全量媒体离线包 ——
class ExportJob(Base):
    """Durable asynchronous export job for one tenant-wide media ZIP.

    Text PDF/Excel exports are generated synchronously and therefore do not
    need a durable job row.  Large media exports always use this queue so the
    web process never buffers a tenant's archive or waits for object storage.
    """

    __tablename__ = "export_jobs"
    __table_args__ = (
        CheckConstraint("kind = 'media_zip'", name="ck_export_jobs_kind"),
        CheckConstraint("format = 'zip'", name="ck_export_jobs_format"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'failed', 'expired')",
            name="ck_export_jobs_status",
        ),
        CheckConstraint(
            "notification_status IN ('pending', 'sent', 'failed')",
            name="ck_export_jobs_notification_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_export_jobs_attempt_count"),
        CheckConstraint(
            "notification_attempts >= 0",
            name="ck_export_jobs_notification_attempts",
        ),
        CheckConstraint(
            "file_size IS NULL OR file_size >= 0",
            name="ck_export_jobs_file_size",
        ),
        Index("ix_export_jobs_tenant_requested", "tenant_id", "requested_at"),
        Index("ix_export_jobs_status_requested", "status", "requested_at"),
        Index("ix_export_jobs_status_expires", "status", "expires_at"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    requested_by = Column(
        String(36), ForeignKey("admin_users.id"), nullable=False, index=True
    )
    kind = Column(String(24), nullable=False, default="media_zip")
    format = Column(String(16), nullable=False, default="zip")
    status = Column(String(16), nullable=False, default="queued")
    storage_backend = Column(String(32), nullable=True)
    storage_ref = Column(Text, nullable=True)
    provider_operation_id = Column(String(128), nullable=True)
    provider_index_ref = Column(Text, nullable=True)
    provider_manifest_ref = Column(Text, nullable=True)
    file_size = Column(BigInteger, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    requested_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error = Column(String(64), nullable=True)
    notification_status = Column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    notification_attempts = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    notification_last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    notification_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# —— RND-393 年度套餐月度导出配额 ——
class ExportMonthlyUsage(Base):
    """Authoritative per-tenant usage count for one calendar month."""

    __tablename__ = "export_monthly_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "period_start",
            "export_type",
            name="uq_export_monthly_usage_period_type",
        ),
        CheckConstraint(
            "export_type IN ('text', 'media_zip')",
            name="ck_export_monthly_usage_type",
        ),
        CheckConstraint("used_count >= 0", name="ck_export_monthly_usage_count"),
        Index(
            "ix_export_monthly_usage_tenant_period",
            "tenant_id",
            "period_start",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    period_start = Column(Date, nullable=False)
    export_type = Column(String(24), nullable=False)
    used_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KeyVersion(Base):
    """Maps WeCom publickey_ver to the private key used for decryption."""

    __tablename__ = "key_versions"
    # Keep the standalone version lookup index from migration 0001 while
    # enforcing uniqueness inside, rather than across, tenant key spaces.
    __table_args__ = (
        Index("ix_key_versions_publickey_ver", "publickey_ver"),
        UniqueConstraint(
            "tenant_id", "publickey_ver", name="uq_key_versions_tenant_publickey_ver"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    publickey_ver = Column(Integer, nullable=False)
    key_alias = Column(String(128), nullable=False)
    private_key_path = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SyncState(Base):
    """Persists the last synced seq per corp so restarts resume safely."""

    __tablename__ = "sync_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "corp_id", name="uq_sync_states_tenant_corp_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    corp_id = Column(String(64), nullable=False)
    last_seq = Column(BigInteger, nullable=False, default=0)
    # RND-211: the cursor remains the source of truth for incremental SDK
    # reads, while this separate version lets the console cheaply decide
    # whether a completed sync requires it to reload its view.
    status = Column(
        Enum("idle", "syncing", "error", name="sync_state_status"),
        nullable=False,
        default="idle",
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    seq_version = Column(Integer, nullable=False, default=0)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ReachabilityAuditRun(Base):
    """Tenant-scoped, aggregate-only reachability-check snapshot (RND-337)."""

    __tablename__ = "reachability_audit_runs"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_reachability_audit_runs_public_id"),
        CheckConstraint(
            "status IN ('checking', 'completed', 'incomplete', 'error')",
            name="ck_reachability_audit_runs_status_valid",
        ),
        CheckConstraint(
            "source IN ('manual', 'incremental', 'reconcile')",
            name="ck_reachability_audit_runs_source_valid",
        ),
        CheckConstraint(
            "matching_count >= 0 AND checked_count >= 0 AND reachable_count >= 0 "
            "AND unreachable_count >= 0",
            name="ck_reachability_audit_runs_counts_nonnegative",
        ),
        Index("ix_reachability_audit_runs_tenant_created", "tenant_id", "created_at"),
        # The database, rather than a process-local lock, makes a tenant's
        # active run idempotent across concurrent web processes.
        Index(
            "uq_reachability_audit_runs_tenant_checking",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'checking'"),
            sqlite_where=text("status = 'checking'"),
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    public_id = Column(String(36), nullable=False)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    status = Column(String(16), nullable=False, default="checking")
    source = Column(String(16), nullable=False, default="manual")
    algorithm_version = Column(String(32), nullable=False)
    scope_from = Column(DateTime(timezone=True), nullable=False)
    scope_to = Column(DateTime(timezone=True), nullable=False)
    # Internal-only candidate watermark. It is never exposed by API/CLI/logs.
    scope_max_message_id = Column(BigInteger, nullable=False, default=0)
    matching_count = Column(Integer, nullable=False, default=0)
    checked_count = Column(Integer, nullable=False, default=0)
    reachable_count = Column(Integer, nullable=False, default=0)
    unreachable_count = Column(Integer, nullable=False, default=0)
    reason_counts = Column(JSONB, nullable=False, default=dict)
    safe_error_code = Column(String(48), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReachabilityFinding(Base):
    """Internal-only lifecycle record for one classified visibility issue."""

    __tablename__ = "reachability_findings"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_reachability_findings_public_id"),
        UniqueConstraint(
            "tenant_id", "archive_message_id", "reason_code", "algorithm_version",
            name="uq_reachability_findings_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "archive_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_reachability_findings_tenant_message",
        ),
        CheckConstraint(
            "status IN ('active', 'resolved')",
            name="ck_reachability_findings_status_valid",
        ),
        CheckConstraint(
            "occurrence_count >= 0",
            name="ck_reachability_findings_occurrences_nonnegative",
        ),
        Index(
            "ix_reachability_findings_tenant_status_last_seen",
            "tenant_id", "status", "last_seen", "id",
        ),
        Index(
            "ix_reachability_findings_tenant_message_active",
            "tenant_id", "archive_message_id", "status",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    public_id = Column(String(36), nullable=False)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    # This is the deliberately internal-only message reference. It must never
    # be serialized, logged, used in a cursor, or accepted from a request.
    archive_message_id = Column(BigInteger, nullable=False)
    reason_code = Column(String(96), nullable=False)
    status = Column(String(16), nullable=False, default="active")
    first_seen = Column(DateTime(timezone=True), nullable=False)
    last_seen = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    occurrence_count = Column(Integer, nullable=False, default=0)
    first_run_id = Column(BigInteger, ForeignKey("reachability_audit_runs.id"), nullable=False)
    last_run_id = Column(BigInteger, ForeignKey("reachability_audit_runs.id"), nullable=False)
    algorithm_version = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ArchiveMessage(Base):
    """
    Stores one WeCom archive message per row.

    Encrypted envelope fields (raw_encrypted_payload, encrypt_random_key,
    encrypt_chat_msg) are always retained so decryption can be re-run after a
    key rotation.  decrypt_status tracks whether decryption has been attempted:
      pending  — row inserted but decryption not yet run
      success  — decryption succeeded
      failed   — decryption failed

    decrypted_payload is ALWAYS NULL, in every decrypt_status. Nothing in
    the application ever assigns to it: the decrypt worker extracts the
    fields the console needs (content_text, msgtype, sender, ...) and
    persists those, and the RND-197 reparse path writes structured_content
    only. This is the SF-1 data minimization contract — the full decrypted
    envelope is deliberately never persisted, so a database dump cannot
    yield it. If you need another field, extract it into its own column;
    do not "fix" this one by populating it.

    content_text holds the plain-text body extracted from the decrypted
    envelope in memory, and is indexed for full-text search via a GIN
    tsvector index.
    """

    __tablename__ = "archive_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "msgid", name="uq_archive_messages_tenant_msgid"
        ),
        # RND-201 round 3 (B4, migration 0011): supports the composite
        # foreign keys on message_revocations (tenant_id,
        # revoke_event_message_id/original_message_id) -> here. id alone
        # is already globally unique (primary key); this adds the
        # tenant-scoped pairing Postgres requires as a composite FK
        # target, proving a referencing row's declared tenant actually
        # matches the tenant of the archive_messages row it points to.
        UniqueConstraint(
            "tenant_id", "id", name="uq_archive_messages_tenant_id_id"
        ),
        Index("ix_archive_messages_msgtime_msgtype", "msgtime", "msgtype"),
        Index(
            "ix_archive_messages_decrypted_payload_gin",
            "decrypted_payload",
            postgresql_using="gin",
        ),
        Index(
            "ix_archive_messages_content_text_fts",
            text("to_tsvector('simple', coalesce(content_text, ''))"),
            postgresql_using="gin",
        ),
        Index(
            "ix_archive_messages_structured_content_gin",
            "structured_content",
            postgresql_using="gin",
        ),
        # RND-191: pg_trgm GIN index so search_messages' ILIKE '%term%'
        # predicate (app/routers/search.py) is index-backed -- the FTS GIN
        # index above only supports to_tsvector(...) @@ to_tsquery(...)
        # matching, never a plain ILIKE substring predicate, so ILIKE was
        # falling back to a sequential scan. Same substring/case-
        # insensitive matching semantics as before; only the query plan
        # changes. See migration 0013.
        Index(
            "ix_archive_messages_content_text_trgm",
            "content_text",
            postgresql_using="gin",
            postgresql_ops={"content_text": "gin_trgm_ops"},
        ),
        # RND-191: tenant_id-leading composite indexes for the hot,
        # always-tenant-scoped query shapes -- see migration 0013's
        # docstring for why a composite beats intersecting single-column
        # indexes here.
        Index("ix_archive_messages_tenant_msgtime_id", "tenant_id", "msgtime", "id"),
        Index("ix_archive_messages_tenant_roomid", "tenant_id", "roomid"),
        Index(
            "ix_archive_messages_tenant_decrypt_revoked",
            "tenant_id",
            "decrypt_status",
            "is_revoked",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    msgid = Column(String(64), nullable=False)
    seq = Column(BigInteger, nullable=False, index=True)

    # --- Encrypted envelope ---
    publickey_ver = Column(Integer, nullable=False)
    raw_encrypted_payload = Column(JSONB, nullable=True)
    encrypt_random_key = Column(Text, nullable=False)
    encrypt_chat_msg = Column(Text, nullable=False)

    # --- Decryption state ---
    decrypt_status = Column(String(16), nullable=False, default="pending")
    decrypted_payload = Column(JSONB, nullable=True)

    # --- Structured content (RND-197) ---
    # Type-specific normalized fields + scoped raw sub-payload for the
    # basic structured message types (link/location/markdown/news/
    # miniprogram/card/docmsg/audio_doc). Deliberately separate from
    # decrypted_payload above -- see migration 0008 docstring for why.
    structured_content = Column(JSONB, nullable=True)

    # --- Fields extracted from decrypted_payload ---
    content_text = Column(Text, nullable=True)
    msgtype = Column(String(32), nullable=True, index=True)
    sender = Column(String(64), nullable=True, index=True)
    roomid = Column(String(64), nullable=True, index=True)
    msgtime = Column(BigInteger, nullable=True, index=True)
    tolist = Column(JSONB, nullable=True)
    sdkfileid = Column(Text, nullable=True)

    # --- Revoke association (RND-201) ---
    # Set only by app.revoke_reconciliation, never by the parser/decrypt
    # self-update above -- a message marks itself revoked only as a side
    # effect of a *different* row (its matching "revoke" event) being
    # processed. content_text/structured_content/decrypted_payload and all
    # media_files rows are never touched when these are set -- see
    # app.revoke_reconciliation module docstring.
    is_revoked = Column(Boolean, nullable=False, server_default=text("false"))
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GroupChatMetadata(Base):
    """Tenant-scoped current metadata for an archived WeCom group chat.

    ``roomid`` remains the immutable archive correlation key.  ``display_name``
    is only a validated current name from the customer-group API; no message
    payload, roster, or name history is retained here.
    """

    __tablename__ = "group_chat_metadata"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "roomid", name="uq_group_chat_metadata_tenant_roomid"
        ),
        Index("ix_group_chat_metadata_tenant_roomid", "tenant_id", "roomid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    roomid = Column(String(64), nullable=False)
    display_name = Column(Text, nullable=True)
    source = Column(String(64), nullable=False, default="wecom_external_groupchat")
    sync_status = Column(String(32), nullable=False, default="unresolved")
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ArchiveMessageRecipient(Base):
    """
    Per-receiver lookup rows derived from archive_messages.tolist.

    One row per (message, receiver) pair.  Populated alongside the parent
    archive_messages row so that queries like "find all messages received by
    user X" can use a plain indexed B-tree lookup instead of JSONB containment.
    """

    __tablename__ = "archive_message_recipients"
    __table_args__ = (
        # RND-191: composite index for the recipient-side membership/
        # participation lookups (tenant_id + receiver_userid always filter
        # together) -- see migration 0013.
        Index(
            "ix_archive_message_recipients_tenant_receiver",
            "tenant_id",
            "receiver_userid",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(
        BigInteger, ForeignKey("archive_messages.id"), nullable=False, index=True
    )
    receiver_userid = Column(String(64), nullable=False, index=True)
    receiver_type = Column(String(32), nullable=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MediaFile(Base):
    """Tracks download state for media attachments referenced by archive messages.

    tenant_id is nullable during migration (backfilled by
    bootstrap_default_tenant.py, then enforced NOT NULL), matching the same
    pattern used for archive_messages/archive_message_recipients/sync_states/
    contacts. sdkfileid uniqueness is scoped to (tenant_id, sdkfileid), not
    global — two different tenants' WeCom corps could in principle hand back
    the same sdkfileid, and a global unique constraint would make the second
    tenant's insert fail outright.

    storage_backend / storage_ref (RND-174 QA remediation, migration 0005):
    the authoritative, per-row record of which MediaStorageProvider holds
    this row's bytes ("local" or "qiniu_kodo") and that provider's own
    reference (a local path for "local", a Qiniu object key for
    "qiniu_kodo"). Media access must resolve the provider from THESE
    columns, never from the deployment-wide MEDIA_STORAGE_PROVIDER setting
    — that setting only controls where NEW media is written, so switching
    it must never reinterpret an existing row (see
    app.media_storage.resolve_media_file_state). Migration 0005 backfills
    every pre-existing row as storage_backend="local",
    storage_ref=local_path.

    local_path is kept for backward compatibility only — legacy/local-only,
    never authoritative for a Qiniu-backed row (a Qiniu row's local_path is
    always left None; only its storage_ref holds the object key). New code
    should read storage_backend/storage_ref, falling back to a populated
    legacy local_path only when storage_backend was never backfilled (see
    app.media_storage.resolve_effective_storage_reference).

    migration_status / migration_attempted_at / migration_error (RND-186,
    migration 0006): bookkeeping for scripts/migrate_local_media_to_qiniu.py
    only — no serving/timeline code path reads these. "Already migrated" is
    fully determined by storage_backend=="qiniu_kodo" alone (a migrated row
    is naturally excluded from any future migration candidate scan); these
    columns exist only to distinguish "never attempted" (migration_status
    IS NULL) from "attempted and failed" (migration_status="failed"), since
    a failed attempt must leave storage_backend/storage_ref completely
    untouched (still "local", still fully servable) rather than encoding
    failure there. migration_error is a short, sanitized diagnostic tag —
    never a raw path, sdkfileid, or exception string.

    bucket / mime_type / checksum_sha256 (RND-186 QA fix, migration 0007):
    full storage metadata for a migrated row, populated by
    scripts/migrate_local_media_to_qiniu.py at the moment of a successful
    upload — computed from the exact bytes uploaded, never re-derived from
    a remote round-trip. bucket is NULL for storage_backend="local" (no
    bucket concept) and is read from the provider that performed the
    upload (never re-read from config independently, so it can never drift
    from what was actually used). mime_type is content-sniffed (see
    app.media_storage.detect_media_signature_from_bytes) — never inferred
    from a file extension alone. checksum_sha256 is a fixed algorithm
    (SHA-256), hex-encoded. storage_ref remains the sole authoritative
    object key / local path reference — deliberately not duplicated into a
    second "object_key" column.
    """

    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "sdkfileid", name="uq_media_files_tenant_sdkfileid"
        ),
        CheckConstraint(
            "download_status != 'downloaded' OR file_size IS NOT NULL",
            name="ck_media_files_downloaded_requires_file_size",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sdkfileid = Column(Text, nullable=False)
    archive_message_id = Column(
        BigInteger, ForeignKey("archive_messages.id"), nullable=False, index=True
    )
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    file_type = Column(String(32), nullable=True)
    local_path = Column(Text, nullable=True)
    oss_key = Column(Text, nullable=True)
    storage_backend = Column(String(32), nullable=True, index=True)
    storage_ref = Column(Text, nullable=True)
    file_size = Column(BigInteger, nullable=True)
    download_status = Column(String(16), nullable=False, default="pending")
    # RND-172: durable event-sweep retry budget. Incremented before every
    # SDK attempt so restarts cannot reset a failing item's retry count.
    download_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    migration_status = Column(String(16), nullable=True, index=True)
    migration_attempted_at = Column(DateTime(timezone=True), nullable=True)
    migration_error = Column(Text, nullable=True)
    bucket = Column(String(128), nullable=True)
    mime_type = Column(String(128), nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    # RND-207 (migration 0012): list/timeline thumbnail metadata for image
    # media. thumbnail_ref is a derived object key / local path held in the
    # SAME storage backend as the original (storage_backend); NULL means
    # "serve the original". image_width/image_height are the original's
    # post-EXIF pixel dimensions, used only to reserve an aspect-ratio box in
    # the list. thumbnail_status/thumbnail_attempted_at/thumbnail_error are
    # backfill bookkeeping mirroring the migration_* columns above: no
    # serving path depends on them, and thumbnail_error is a short sanitized
    # tag only — never a raw path/key/exception.
    thumbnail_ref = Column(Text, nullable=True)
    image_width = Column(Integer, nullable=True)
    image_height = Column(Integer, nullable=True)
    thumbnail_status = Column(String(16), nullable=True, index=True)
    thumbnail_attempted_at = Column(DateTime(timezone=True), nullable=True)
    thumbnail_error = Column(Text, nullable=True)
    # RND-258: browser-playable derivative for voice / audio_archive media.
    # The original archival object remains untouched and downloadable.
    playback_ref = Column(Text, nullable=True)
    playback_status = Column(
        String(32),
        nullable=True,
        default="not_applicable",
        server_default=text("'not_applicable'"),
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MediaQuotaBlock(Base):
    """Durable capacity denial separate from stored-media truth (RND-385)."""

    __tablename__ = "media_quota_blocks"
    __table_args__ = (
        CheckConstraint("observed_bytes > 0", name="ck_media_quota_blocks_bytes"),
        CheckConstraint(
            "reason IN ('quota_exceeded', 'subscription_inactive', "
            "'usage_unavailable')",
            name="ck_media_quota_blocks_reason",
        ),
        Index("ix_media_quota_blocks_tenant_blocked", "tenant_id", "blocked_at"),
    )

    media_file_id = Column(
        Integer,
        ForeignKey("media_files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    observed_bytes = Column(BigInteger, nullable=False)
    reason = Column(String(32), nullable=False)
    blocked_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantStorageDaily(Base):
    """Daily materialized media-byte total for one tenant (RND-331).

    The rollup service is the sole writer. ``used_bytes`` is calculated only
    from MediaFile rows whose download_status is ``downloaded``.
    """

    __tablename__ = "tenant_storage_daily"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "usage_date", name="uq_tenant_storage_daily_tenant_date"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    usage_date = Column(Date, nullable=False)
    used_bytes = Column(BigInteger, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MessageRevocation(Base):
    """One row per WeCom "revoke" (撤回) event, associating it with the
    original ArchiveMessage it targets (RND-201).

    A revoke event arrives as its own archive_messages row
    (msgtype="revoke"); its decrypted payload's revoke.pre_msgid field
    names the msgid of the message being revoked (WeCom session-archive
    convention: https://developer.work.weixin.qq.com/document/path/91774,
    e.g. {"msgid":"...","action":"recall","msgtype":"revoke",
    "revoke":{"pre_msgid":"..."}}). This table is the durable record of
    that association, independent of which order the two rows arrive in
    or which worker run processes them -- see app.revoke_reconciliation,
    the single canonical place this table is written from.

    status:
      "pending"   -- revoke event decrypted and target_msgid extracted,
                     but no archive_messages row with msgid=target_msgid
                     exists yet in this tenant. Retried on every future
                     decrypt sweep (reconcile_pending_revocations).
      "linked"    -- original_message_id is set; the original row's
                     is_revoked/revoked_at have been updated. Terminal.
      "malformed" -- the revoke event's decrypted payload had no usable
                     target_msgid. Terminal -- there is nothing to retry,
                     but the event and its raw payload are retained
                     rather than dropped.

    original_message_id is set exactly once, on first successful link,
    and never cleared or reassigned afterwards. If two revoke events ever
    target the same original message, the second one still links (so it
    is never left dangling in "pending") but never overwrites the first
    revoke's revoked_at on the original row -- see
    app.revoke_reconciliation for the earliest-wins tie-break rule.

    tenant_id is REQUIRED (NOT NULL, RND-201 round 3 / B4, migration
    0011) -- unlike the nullable-during-migration convention every other
    archive-adjacent table follows, message_revocations was introduced
    by this feature (migration 0009) with no legacy pre-tenant-
    foundation rows possible, so there was never a valid reason for it
    to be nullable here.

    Two CHECK constraints (migration 0010, RND-201 round 2 QA fix)
    enforce association consistency at the database level, not just in
    app.revoke_reconciliation's application logic:

      ck_message_revocations_status_valid -- status can only ever be one
      of the three values this model's docstring documents. Catches a
      future typo/regression at write time instead of silently storing
      an unrecognized status that app.revoke_reconciliation.
      display_status() and every caller would then mis-handle.

      ck_message_revocations_linked_consistency -- original_message_id
      is set if and only if status="linked". This is the exact invariant
      _try_link() maintains by construction (both columns are written
      together in one statement — see app.revoke_reconciliation), made
      impossible to violate even by a future bug or an out-of-band
      manual UPDATE.

    Tenant + uniqueness integrity (migration 0011, RND-201 round 3 / B4
    QA fix) -- database-enforced, not just application-filtered:

      uq_message_revocations_revoke_event_message_id -- a PLAIN unique
      constraint on revoke_event_message_id alone (not
      (tenant_id, revoke_event_message_id)): one revoke event can only
      ever have exactly one association row, period -- a composite
      unique constraint would still theoretically allow the same
      revoke_event_message_id to appear twice under two different
      tenant_id values, which is exactly the ambiguity this closes.

      fk_message_revocations_tenant_revoke_event /
      fk_message_revocations_tenant_original -- composite foreign keys
      (tenant_id, revoke_event_message_id) and
      (tenant_id, original_message_id), both referencing
      archive_messages(tenant_id, id) (see ArchiveMessage's
      uq_archive_messages_tenant_id_id). These replace the old
      single-column FKs to archive_messages.id: Postgres's default
      MATCH SIMPLE means a composite FK is skipped entirely when any of
      its columns is NULL, so original_message_id staying NULL for
      pending/malformed rows is unaffected -- but whenever
      original_message_id IS set (or always, for the NOT-NULL
      revoke_event_message_id side), the constraint now additionally
      proves the referenced archive_messages row belongs to the SAME
      tenant this association row declares, not merely that a row with
      that id exists somewhere.
    """

    __tablename__ = "message_revocations"
    __table_args__ = (
        UniqueConstraint(
            "revoke_event_message_id",
            name="uq_message_revocations_revoke_event_message_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "revoke_event_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_message_revocations_tenant_revoke_event",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "original_message_id"],
            ["archive_messages.tenant_id", "archive_messages.id"],
            name="fk_message_revocations_tenant_original",
        ),
        Index(
            "ix_message_revocations_tenant_target_msgid",
            "tenant_id",
            "target_msgid",
        ),
        CheckConstraint(
            "status IN ('pending', 'linked', 'malformed')",
            name="ck_message_revocations_status_valid",
        ),
        CheckConstraint(
            "(status = 'linked') = (original_message_id IS NOT NULL)",
            name="ck_message_revocations_linked_consistency",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )

    # No per-column ForeignKey(...) here -- the referential integrity for
    # both of these (existence AND tenant match) is provided by the
    # composite ForeignKeyConstraints in __table_args__ above.
    revoke_event_message_id = Column(BigInteger, nullable=False, index=True)
    revoke_event_msgid = Column(String(64), nullable=False)
    revoke_event_msgtime = Column(BigInteger, nullable=True)

    target_msgid = Column(String(64), nullable=True)
    original_message_id = Column(BigInteger, nullable=True, index=True)

    status = Column(String(16), nullable=False, default="pending")

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RetentionLock(Base):
    """One immutable retention-lock record per expired archive message."""

    __tablename__ = "retention_locks"
    __table_args__ = (
        UniqueConstraint(
            "archive_message_id", name="uq_retention_locks_archive_message_id"
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    archive_message_id = Column(
        Integer, ForeignKey("archive_messages.id"), nullable=False
    )
    locked_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Contact(Base):
    """Lightweight registry of WeCom user identities seen in the archive."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "wecom_userid", name="uq_contacts_tenant_wecom_userid"
        ),
        # RND-191: pg_trgm GIN indexes so search_contacts' ILIKE '%term%'
        # predicates (app/routers/search.py) are index-backed instead of a
        # sequential scan. See migration 0013.
        Index(
            "ix_contacts_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_contacts_wecom_userid_trgm",
            "wecom_userid",
            postgresql_using="gin",
            postgresql_ops={"wecom_userid": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    wecom_userid = Column(String(64), nullable=False)
    name = Column(Text, nullable=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExternalContact(Base):
    """WeCom external contact, populated by the external-contact API."""

    __tablename__ = "external_contacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_userid",
            name="uq_external_contacts_tenant_ext_userid",
        ),
        Index(
            "ix_external_contacts_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_external_contacts_current_nickname_trgm",
            "current_nickname_normalized",
            postgresql_using="gin",
            postgresql_ops={"current_nickname_normalized": "gin_trgm_ops"},
        ),
        Index(
            "ix_external_contacts_company_trgm",
            "company",
            postgresql_using="gin",
            postgresql_ops={"company": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_userid = Column(String(64), nullable=False)
    # Compatibility display label from RND-287. It can contain an employee
    # remark and is deliberately not repurposed as the customer's real name.
    name = Column(Text, nullable=True)
    # The current WeCom self-chosen nickname is a separate customer-level
    # identity field. The raw value remains auditable; normalized/display
    # values keep comparison and rendering safe.
    current_nickname_raw = Column(Text, nullable=True)
    current_nickname_normalized = Column(Text, nullable=True)
    current_nickname_display = Column(Text, nullable=True)
    current_nickname_observed_at = Column(DateTime(timezone=True), nullable=True)
    company = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    source = Column(Text, nullable=True)
    owner_wecom_userid = Column(String(64), nullable=True)
    last_interaction_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, nullable=True)
    tenant_id = Column(
        String(36), ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExternalContactRefreshTask(Base):
    """Durable, coalesced external-contact metadata refresh request.

    The task contains only the bounded external identifier needed to call the
    WeCom API. It is intentionally separate from ``external_contacts`` so an
    incoming direct message can request a lookup even when WeCom has not yet
    made that user a readable external-contact relationship.
    """

    __tablename__ = "external_contact_refresh_tasks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_userid",
            name="uq_external_contact_refresh_tasks_tenant_external_userid",
        ),
        Index(
            "ix_external_contact_refresh_tasks_tenant_ready",
            "tenant_id",
            "state",
            "next_attempt_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False, index=True)
    external_userid = Column(String(64), nullable=False)
    source = Column(String(32), nullable=False)
    state = Column(String(16), nullable=False, server_default=text("'pending'"))
    attempt_count = Column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_error_class = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExternalContactFollow(Base):
    """One tenant-scoped employee-to-external-contact follow relationship."""

    __tablename__ = "external_contact_follows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "external_userid"],
            ["external_contacts.tenant_id", "external_contacts.external_userid"],
            name="fk_external_contact_follows_tenant_contact",
        ),
        UniqueConstraint(
            "tenant_id",
            "external_userid",
            "follow_userid",
            name="uq_external_contact_follows_tenant_contact_user",
        ),
        Index(
            "ix_external_contact_follows_tenant_contact_active",
            "tenant_id",
            "external_userid",
            "is_active",
        ),
        Index(
            "ix_external_contact_follows_remark_trgm",
            "remark_normalized",
            postgresql_using="gin",
            postgresql_ops={"remark_normalized": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    external_userid = Column(String(64), nullable=False)
    follow_userid = Column(String(64), nullable=False)
    remark_raw = Column(Text, nullable=True)
    remark_normalized = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    observed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# RND-356 (T2) — AI support knowledge-base index, chat, and audit log.
#
# The allowlist these tables are ever populated from lives in
# backend/app/ai_kb/manifest.json (RND-355) — nothing here scans a
# directory directly. See app/services/ai/ingestion.py.
# ---------------------------------------------------------------------------


class KbIndexVersion(Base):
    """One row per index build (full rebuild or incremental run). Chunks
    reference the version that produced them, which is what makes "roll
    back to the previous index" a metadata flip (mark this version
    rolled_back, chunks with status!=active stop being retrieved) rather
    than a destructive delete-and-hope-for-the-best."""

    __tablename__ = "kb_index_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    status = Column(String(16), nullable=False, default="active")  # active | superseded | rolled_back
    triggered_by = Column(String(64), nullable=False)  # e.g. "manual", "scheduled_reindex"
    plan_summary = Column(JSONB, nullable=True)  # counts of add/update/remove/noop, no content
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)


class KbDocumentChunk(Base):
    """One retrievable unit of an approved knowledge source (RND-355
    manifest entry), split by heading. access_level/locale are denormalized
    from the manifest entry at index time so retrieval filtering never has
    to join back to the manifest file on the hot path."""

    __tablename__ = "kb_document_chunks"
    __table_args__ = (
        Index(
            "ix_kb_document_chunks_source_chunk",
            "source_id", "chunk_index",
        ),
        Index(
            "ix_kb_document_chunks_access_locale_version",
            "access_level", "locale", "index_version_id",
        ),
        # pg_trgm GIN index, NOT to_tsvector — Postgres's 'simple' FTS
        # config tokenizes an entire run of CJK characters as one lexeme
        # (no Chinese word segmenter shipped), so it cannot match a query
        # phrase against a sub-phrase of a longer chunk. See
        # app/services/ai/retriever.py's module docstring for the
        # empirical verification. Mirrors the existing
        # ix_archive_messages_content_text_trgm precedent.
        Index(
            "ix_kb_document_chunks_content_text_trgm",
            "content_text",
            postgresql_using="gin",
            postgresql_ops={"content_text": "gin_trgm_ops"},
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    index_version_id = Column(BigInteger, ForeignKey("kb_index_versions.id"), nullable=False)
    source_id = Column(String(128), nullable=False)  # matches ai_kb manifest source_id
    topic_id = Column(String(128), nullable=False)
    title = Column(String(255), nullable=False)
    heading_path = Column(Text, nullable=False)  # e.g. "配置说明 > 存储配额"
    chunk_index = Column(Integer, nullable=False)
    content_text = Column(Text, nullable=False)
    access_level = Column(String(16), nullable=False)  # customer | internal (never "forbidden")
    locale = Column(String(16), nullable=False)
    doc_version = Column(String(32), nullable=False)
    doc_path = Column(String(512), nullable=False)  # repo-relative, for the citation link
    content_hash = Column(String(64), nullable=False)  # sha256 of the whole source file; drives compute_index_plan diffing
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AiChatSession(Base):
    """One AI support conversation. Retention/deletion policy owned by
    RND-359 (T5); this table only records session identity and lifecycle."""

    __tablename__ = "ai_chat_sessions"
    __table_args__ = (
        Index("ix_ai_chat_sessions_tenant_admin_user", "tenant_id", "admin_user_id"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    status = Column(String(16), nullable=False, default="active")  # active | closed
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_activity_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)


class AiChatMessage(Base):
    """One turn of an AI support conversation. `citations` holds only
    {source_id, title, heading_path, doc_path} objects for chunks the
    answering request was itself permitted to retrieve — never a raw
    document excerpt, and never anything the answer_service didn't already
    have server-side evidence for (see app/services/ai/answer_service.py)."""

    __tablename__ = "ai_chat_messages"
    __table_args__ = (
        Index("ix_ai_chat_messages_session_created", "session_id", "created_at", "id"),
        Index("ix_ai_chat_messages_tenant", "tenant_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("ai_chat_sessions.id"), nullable=False)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    role = Column(String(16), nullable=False)  # user | assistant | system
    content = Column(Text, nullable=False)
    citations = Column(JSONB, nullable=True)
    response_status = Column(String(32), nullable=True)  # answered | insufficient_evidence | escalated | error
    index_version_id = Column(BigInteger, ForeignKey("kb_index_versions.id"), nullable=True)
    model_provider = Column(String(64), nullable=True)
    model_name = Column(String(128), nullable=True)
    # RND-357 (T3): "有帮助/无帮助" feedback on one assistant message.
    helpful = Column(Boolean, nullable=True)
    feedback_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AiQueryAuditLog(Base):
    """Redacted audit trail of every AI support query: what was retrieved
    and returned, never the sensitive values a tool call or config read
    might have touched. query_text is the admin's own typed question — not
    archived customer chat content — and is retained under the same policy
    as the owning chat session (RND-359)."""

    __tablename__ = "ai_query_audit_logs"
    __table_args__ = (
        Index("ix_ai_query_audit_logs_tenant_created", "tenant_id", "created_at", "id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    # ondelete=SET NULL: this audit row must survive deletion of the chat
    # session it was generated for (RND-357's user-initiated session
    # deletion must never be blocked by, or cascade into deleting, the
    # separately-retained audit trail — see RND-359's retention policy).
    session_id = Column(String(36), ForeignKey("ai_chat_sessions.id", ondelete="SET NULL"), nullable=True)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    query_text = Column(Text, nullable=False)
    retrieved_chunk_ids = Column(JSONB, nullable=False, default=list)
    index_version_id = Column(BigInteger, ForeignKey("kb_index_versions.id"), nullable=True)
    model_provider = Column(String(64), nullable=True)
    model_name = Column(String(128), nullable=True)
    response_status = Column(String(32), nullable=False)
    latency_ms = Column(Integer, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# RND-357 (T3) — escalate-to-human handoff record.
#
# This is a first, minimal shape covering T3's own AC ("转人工时生成脱敏摘要；
# 用户可预览并确认提交内容"). RND-359 (T5) owns the real triage/classification
# pipeline and will ALTER this table to add reason/resolution_category/
# resolved_at/resolved_by columns rather than replace it.
# ---------------------------------------------------------------------------


class AiHandoff(Base):
    """One user-confirmed escalation-to-human request. redacted_summary is
    always the text the user actually saw and approved in the preview —
    never regenerated silently after submission."""

    __tablename__ = "ai_handoff"
    __table_args__ = (
        Index("ix_ai_handoff_tenant_created", "tenant_id", "created_at", "id"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    # ondelete=SET NULL: a submitted handoff must survive deletion of the
    # originating chat session — see AiQueryAuditLog.session_id above.
    session_id = Column(String(36), ForeignKey("ai_chat_sessions.id", ondelete="SET NULL"), nullable=True)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    redacted_summary = Column(Text, nullable=False)
    contact = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="pending")  # pending | in_review | resolved
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# RND-161 (T6) — proactive user feedback, submitted independently of any AI
# conversation. Feeds the same human-processing/knowledge-gap closed loop
# as AiHandoff (RND-359 unifies triage for both), but is a distinct
# record: a user reporting a bug/suggestion never implies an AI turn
# happened, and vice versa.
# ---------------------------------------------------------------------------


class AiFeedback(Base):
    """One user-submitted feedback record. product_version/page_id, when
    present, come only from the RND-358 read-only tool registry
    (product_version / current_page) — this table never grows its own
    parallel diagnostic-collection logic."""

    __tablename__ = "ai_feedback"
    __table_args__ = (
        Index("ix_ai_feedback_tenant_created", "tenant_id", "created_at", "id"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    feedback_type = Column(String(16), nullable=False)  # bug | question | suggestion | other
    body = Column(Text, nullable=False)
    contact = Column(Text, nullable=True)
    product_version = Column(String(64), nullable=True)
    page_id = Column(String(64), nullable=True)
    browser_info = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="new")  # new | in_review | resolved
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# RND-358 (T4) — read-only diagnostic tool invocation audit.
# ---------------------------------------------------------------------------


class AiToolInvocation(Base):
    """One audited call through app.services.ai_tools.registry.invoke_tool.
    fields_returned holds only the returned FIELD NAMES (a JSON array of
    strings) — never values — so this table can reconstruct "who read what
    kind of data when" without being able to reconstruct any actual
    configuration value or diagnostic content."""

    __tablename__ = "ai_tool_invocations"
    __table_args__ = (
        Index("ix_ai_tool_invocations_tenant_created", "tenant_id", "created_at", "id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), ForeignKey("tenants.id"), nullable=False)
    admin_user_id = Column(String(36), ForeignKey("admin_users.id"), nullable=False)
    tool_name = Column(String(64), nullable=False)
    consent_given = Column(Boolean, nullable=False)
    fields_returned = Column(JSONB, nullable=False, default=list)
    result_status = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExternalContactNicknameHistory(Base):
    """Observed transitions of a customer's own WeCom nickname."""

    __tablename__ = "external_contact_nickname_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "external_userid"],
            ["external_contacts.tenant_id", "external_contacts.external_userid"],
            name="fk_external_contact_nickname_history_tenant_contact",
        ),
        Index(
            "ix_external_contact_nickname_history_tenant_contact_observed",
            "tenant_id",
            "external_userid",
            "observed_at",
            "id",
        ),
        Index(
            "ix_external_contact_nickname_history_old_trgm",
            "old_nickname_normalized",
            postgresql_using="gin",
            postgresql_ops={"old_nickname_normalized": "gin_trgm_ops"},
        ),
        Index(
            "ix_external_contact_nickname_history_new_trgm",
            "new_nickname_normalized",
            postgresql_using="gin",
            postgresql_ops={"new_nickname_normalized": "gin_trgm_ops"},
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(36), nullable=False)
    external_userid = Column(String(64), nullable=False)
    old_nickname_raw = Column(Text, nullable=True)
    old_nickname_normalized = Column(Text, nullable=True)
    old_nickname_display = Column(Text, nullable=True)
    new_nickname_raw = Column(Text, nullable=True)
    new_nickname_normalized = Column(Text, nullable=True)
    new_nickname_display = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TenantActivationCheck(Base):
    """Persisted self-service activation-gate state (RND-388).

    Deliberately separate from ``Tenant.lifecycle_status``: that column keeps
    its frozen CHECK (provisioning|active|frozen|suspended) while this table
    records the automated activation-check state machine
    (not_started|blocked|ready).  A tenant with no row has never been
    evaluated; rows are created by the first evaluation and updated in place
    (revision+1 per evaluation).
    """

    __tablename__ = "tenant_activation_checks"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_activation_checks_tenant"),
        CheckConstraint(
            "state IN ('not_started', 'blocked', 'ready')",
            name="ck_tenant_activation_checks_state",
        ),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(
        String(36),
        ForeignKey("tenants.id"),
        nullable=False,
    )
    state = Column(
        String(16),
        nullable=False,
        default="not_started",
        server_default=text("'not_started'"),
    )
    gate_results = Column(JSONB, nullable=False, default=dict)
    safe_error_code = Column(String(64), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
