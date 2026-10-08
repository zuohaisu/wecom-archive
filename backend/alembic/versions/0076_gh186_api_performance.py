"""GH-186 super-admin API performance telemetry tables.

Revision ID: 0076
Revises: 0075
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0076"
down_revision: Union[str, None] = "0075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AGG_COLUMNS = [
    sa.Column("request_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("class_counts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("completed_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("duration_sum_us", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("duration_min_us", sa.BigInteger(), nullable=True),
    sa.Column("duration_max_us", sa.BigInteger(), nullable=True),
    sa.Column("success_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("success_duration_sum_us", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("success_min_us", sa.BigInteger(), nullable=True),
    sa.Column("success_max_us", sa.BigInteger(), nullable=True),
    sa.Column("hist", postgresql.JSONB(), nullable=True),
    sa.Column("hist_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    sa.Column("stream_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("stream_error_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("stream_duration_sum_us", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("stream_min_us", sa.BigInteger(), nullable=True),
    sa.Column("stream_max_us", sa.BigInteger(), nullable=True),
    sa.Column("stream_ttfb_sum_us", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    sa.Column("stream_ttfb_min_us", sa.BigInteger(), nullable=True),
    sa.Column("stream_ttfb_max_us", sa.BigInteger(), nullable=True),
    sa.Column("stats_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
]


def upgrade() -> None:
    op.create_table(
        "api_performance_endpoint_hourly",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("route", sa.String(length=500), nullable=False),
        sa.Column("traffic_class", sa.String(length=16), nullable=False, server_default="business"),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        *_AGG_COLUMNS,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "method", "route", "bucket_start", name="uq_api_perf_hourly_endpoint_bucket"
        ),
    )
    op.create_index(
        "ix_api_perf_hourly_bucket_start", "api_performance_endpoint_hourly", ["bucket_start"]
    )

    op.create_table(
        "api_performance_endpoint_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("route", sa.String(length=500), nullable=False),
        sa.Column("traffic_class", sa.String(length=16), nullable=False, server_default="business"),
        sa.Column("bucket_date", sa.Date(), nullable=False),
        *_AGG_COLUMNS,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "method", "route", "bucket_date", name="uq_api_perf_daily_endpoint_bucket"
        ),
    )
    op.create_index(
        "ix_api_perf_daily_bucket_date", "api_performance_endpoint_daily", ["bucket_date"]
    )

    op.create_table(
        "api_performance_flush_batches",
        sa.Column("batch_id", sa.String(length=120), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("flush_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_index(
        "ix_api_perf_batches_created_at", "api_performance_flush_batches", ["created_at"]
    )

    op.create_table(
        "api_performance_alert_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_date", sa.Date(), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("anomaly_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alert_date", name="uq_api_perf_alert_date"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'failed_or_unknown')",
            name="ck_api_perf_alert_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("api_performance_alert_state")
    op.drop_table("api_performance_flush_batches")
    op.drop_index("ix_api_perf_daily_bucket_date", table_name="api_performance_endpoint_daily")
    op.drop_table("api_performance_endpoint_daily")
    op.drop_index("ix_api_perf_hourly_bucket_start", table_name="api_performance_endpoint_hourly")
    op.drop_table("api_performance_endpoint_hourly")
