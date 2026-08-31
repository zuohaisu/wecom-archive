"""Add auditable resolution evidence for historical payment findings.

Revision ID: 0073
Revises: 0072
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: Union[str, None] = "0072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RESOLUTION_CLASSIFICATION_CHECK = (
    "resolution_classification IS NULL OR resolution_classification IN "
    "('HISTORICAL_VERIFICATION_READINESS_PROBING', 'UNKNOWN')"
)
_RESOLUTION_FAILURE_CLASS_CHECK = (
    "resolution_failure_class IS NULL OR resolution_failure_class IN "
    "('MISSING_SIGNATURE_HEADERS', 'UNKNOWN_PUBLIC_KEY_ID', "
    "'INVALID_TIMESTAMP', 'STALE_TIMESTAMP', "
    "'INVALID_SIGNATURE_ENCODING', 'SIGNATURE_MISMATCH', "
    "'SIGNTEST', 'UNKNOWN')"
)


def upgrade() -> None:
    with op.batch_alter_table("payment_recovery_findings") as batch:
        batch.add_column(sa.Column("resolution_classification", sa.String(length=64)))
        batch.add_column(sa.Column("resolution_failure_class", sa.String(length=64)))
        batch.add_column(sa.Column("resolution_reason_code", sa.String(length=64)))
        batch.add_column(
            sa.Column("resolved_by_platform_admin_id", sa.String(length=36))
        )
        batch.create_foreign_key(
            "fk_payment_recovery_findings_resolved_by_platform_admin",
            "platform_admins",
            ["resolved_by_platform_admin_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_payment_recovery_findings_resolution_classification",
            _RESOLUTION_CLASSIFICATION_CHECK,
        )
        batch.create_check_constraint(
            "ck_payment_recovery_findings_resolution_failure_class",
            _RESOLUTION_FAILURE_CLASS_CHECK,
        )


def downgrade() -> None:
    with op.batch_alter_table("payment_recovery_findings") as batch:
        batch.drop_constraint(
            "ck_payment_recovery_findings_resolution_failure_class", type_="check"
        )
        batch.drop_constraint(
            "ck_payment_recovery_findings_resolution_classification", type_="check"
        )
        batch.drop_constraint(
            "fk_payment_recovery_findings_resolved_by_platform_admin",
            type_="foreignkey",
        )
        batch.drop_column("resolved_by_platform_admin_id")
        batch.drop_column("resolution_reason_code")
        batch.drop_column("resolution_failure_class")
        batch.drop_column("resolution_classification")
