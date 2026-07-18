"""Database-level integrity constraints for message_revocations (RND-201 round 2 QA fix).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-18

Round 2 independent QA asked for a review of whether association
consistency should be enforced at the schema level rather than relying
solely on app.revoke_reconciliation's application logic. Two CHECK
constraints are added, both purely additive and safe against existing
data (message_revocations is a brand-new table from migration 0009; no
production rows can violate either constraint since app.revoke_
reconciliation has always maintained both invariants by construction):

  ck_message_revocations_status_valid -- status can only be one of
  "pending" / "linked" / "malformed" (see MessageRevocation's model
  docstring). Catches a future typo/regression at write time.

  ck_message_revocations_linked_consistency -- original_message_id is
  set if and only if status="linked". This is the exact invariant
  app.revoke_reconciliation._try_link() maintains by writing both
  columns together in a single UPDATE statement; the constraint makes it
  impossible to violate even via a future bug or an out-of-band manual
  UPDATE.

Deliberately does NOT touch archive_messages, tenant_id nullability, or
any foreign key on this or any other table -- those were reviewed and
left unchanged (see the RND-201 round 2 development report's Database
Integrity section for the reasoning: archive_messages.tenant_id
nullability is an established, intentional, codebase-wide convention
predating this ticket, and a composite (tenant_id, id) foreign key would
require altering archive_messages itself, which round 1 QA already
approved unchanged -- the existing explicit tenant_id filter discipline
in app.revoke_reconciliation, already covered by tenant-isolation tests,
provides the equivalent protection without that risk).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_message_revocations_status_valid",
        "message_revocations",
        "status IN ('pending', 'linked', 'malformed')",
    )
    op.create_check_constraint(
        "ck_message_revocations_linked_consistency",
        "message_revocations",
        "(status = 'linked') = (original_message_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_message_revocations_linked_consistency",
        "message_revocations",
        type_="check",
    )
    op.drop_constraint(
        "ck_message_revocations_status_valid",
        "message_revocations",
        type_="check",
    )
