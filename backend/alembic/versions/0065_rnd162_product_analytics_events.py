"""Add privacy-minimal product analytics events (RND-162).

Revision ID: 0065
Revises: 0064
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065"
down_revision: Union[str, None] = "0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_analytics_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_name", sa.String(length=96), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("event_class", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("admin_user_id", sa.String(length=36), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "source IN ('frontend', 'backend')",
            name="ck_product_analytics_events_source",
        ),
        sa.CheckConstraint(
            "event_class IN ('authentication', 'core_workflow', 'secondary_workflow')",
            name="ck_product_analytics_events_class",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_product_analytics_events_tenant", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["admin_users.id"], name="fk_product_analytics_events_admin_user", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_product_analytics_events"),
    )
    op.create_index(
        "ix_product_analytics_events_tenant_occurred",
        "product_analytics_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_product_analytics_events_name_occurred",
        "product_analytics_events",
        ["event_name", "occurred_at"],
    )
    op.create_index(
        "ix_product_analytics_events_occurred",
        "product_analytics_events",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_analytics_events_occurred", table_name="product_analytics_events")
    op.drop_index("ix_product_analytics_events_name_occurred", table_name="product_analytics_events")
    op.drop_index("ix_product_analytics_events_tenant_occurred", table_name="product_analytics_events")
    op.drop_table("product_analytics_events")
