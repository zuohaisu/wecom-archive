"""Database-level tenant and association integrity for message_revocations (RND-201 round 3 QA fix, B4).

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-18

Round 2 QA closed B1/B2/B3/B7 but rejected the release on B4: the
database still accepted message_revocations.tenant_id = NULL, duplicate
rows for the same revoke event, and rows whose declared tenant disagreed
with the tenant of the archive_messages row(s) they reference.
app.revoke_reconciliation's explicit tenant_id filters (already covered
by tenant-isolation tests) are real but insufficient on their own --
this migration makes every one of those invariants impossible to violate
at the database level, independent of application code correctness.

This migration is data-safety-first: it validates (and, only where
provably safe, repairs) existing message_revocations rows BEFORE adding
any new constraint, and raises a clear RuntimeError rather than silently
applying a constraint over data that would violate it. See
scripts/check_message_revocations_integrity.py for the equivalent
read-only, pre-deployment dry-run version of the same checks (run that
first against production; this migration performs the same checks again
itself as a safety net, since the two can't be assumed to always run
back-to-back against an unchanged database).

Deterministic tenant recovery (the ONLY recovery this migration ever
performs): a message_revocations row with tenant_id IS NULL is repaired
using its revoke_event_message_id's own archive_messages row --
`message_revocations.revoke_event_message_id -> archive_messages.id ->
archive_messages.tenant_id` -- because that archive_messages row is the
authoritative record of the event this association is FOR. No tenant is
ever invented, defaulted, or inferred from any other source. If the
referenced archive_messages row does't exist, or itself has
tenant_id IS NULL, the row cannot be safely repaired and the migration
aborts.

No other data is ever repaired automatically:
  - revoke-event tenant mismatches, original-message tenant mismatches,
    and duplicate revoke_event_message_id rows are all detected and
    reported, but never silently fixed (there is no deterministic,
    provably-correct rule for picking a winner) -- the migration aborts
    with an actionable diagnostic (which rows, which tenants) instead.

Schema changes, all additive/tightening and reversible (see downgrade()):
  1. archive_messages gets a new UNIQUE(tenant_id, id) constraint --
     needed as the target of the composite foreign keys below (id alone
     is already globally unique via the primary key; this does not
     change that, it only adds a tenant-scoped view of the same
     guarantee that Postgres can build a composite FK against).
  2. message_revocations.tenant_id becomes NOT NULL.
  3. uq_message_revocations_tenant_revoke_event (composite UNIQUE on
     (tenant_id, revoke_event_message_id)) is replaced with a plain
     UNIQUE(revoke_event_message_id) -- one revoke event can only ever
     have exactly one association row, full stop; keeping the composite
     version alongside would be redundant (a plain single-column unique
     constraint is strictly stronger) and would still theoretically
     allow the same revoke_event_message_id to appear under two
     different tenant_id values, which is exactly the ambiguity B4
     flagged.
  4. The two single-column foreign keys
     (revoke_event_message_id -> archive_messages.id,
     original_message_id -> archive_messages.id) are replaced with
     composite foreign keys
     (tenant_id, revoke_event_message_id) -> archive_messages(tenant_id, id)
     and (tenant_id, original_message_id) -> archive_messages(tenant_id, id).
     Postgres's default MATCH SIMPLE means a composite FK is not
     evaluated at all when any of its columns is NULL -- since
     original_message_id is nullable (pending/malformed rows) but
     tenant_id is now NOT NULL, a NULL original_message_id still leaves
     that row's link to archive_messages completely optional, exactly
     as before; a NON-NULL original_message_id now additionally proves
     the referenced row belongs to the SAME tenant, which is the actual
     B4 requirement. The revoke_event_message_id side is NOT NULL
     already, so its composite FK is always fully checked -- it now
     proves both existence and tenant match, replacing the old
     existence-only guarantee with a strictly stronger one.

The two CHECK constraints added in migration 0010
(ck_message_revocations_status_valid,
ck_message_revocations_linked_consistency) are untouched -- they remain
valid and continue to enforce the status/original_message_id state
model unchanged.

Lock/downtime note (explicitly required to be documented, not assumed):
this migration's DDL runs inside one Alembic transaction. On Postgres,
`ALTER TABLE ... ADD CONSTRAINT ... NOT NULL` and `ADD CONSTRAINT ...
FOREIGN KEY` each take an ACCESS EXCLUSIVE lock on message_revocations
(and, for the new archive_messages UNIQUE constraint, on
archive_messages) for the duration of the validation scan Postgres
performs while adding them. For the expected production data volume
this ticket is aware of (message_revocations is a brand-new table
introduced by 0009, with no confirmed production rows as of this
ticket -- see the RND-201 round 1 report), this is expected to be
near-instantaneous. This has NOT been demonstrated against a
production-scale table, and this migration does NOT claim zero-downtime
behavior -- if message_revocations ever grows large before this
migration ships, re-verify lock duration in a staging environment first
(e.g. via `\\timing` and a concurrent read query) before running against
production.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class MessageRevocationIntegrityError(RuntimeError):
    """Raised when message_revocations contains data that cannot be
    safely migrated to the new tenant/association integrity constraints
    -- see this module's docstring for exactly what is and is not
    auto-repaired."""


def _scalar(conn, sql: str):
    return conn.execute(sa.text(sql)).scalar()


def _rows(conn, sql: str):
    return conn.execute(sa.text(sql)).fetchall()


def upgrade() -> None:
    conn = op.get_bind()

    # --- Step 1: deterministic tenant recovery (the only auto-repair) ---
    # message_revocations.revoke_event_message_id -> archive_messages.id
    # -> archive_messages.tenant_id is the authoritative source: that
    # archive_messages row IS the revoke event this association record
    # is for. Only applied when the referenced row exists and itself has
    # a non-NULL tenant_id -- never a default, never inferred otherwise.
    conn.execute(
        sa.text(
            """
            UPDATE message_revocations mr
            SET tenant_id = am.tenant_id
            FROM archive_messages am
            WHERE mr.tenant_id IS NULL
              AND mr.revoke_event_message_id = am.id
              AND am.tenant_id IS NOT NULL
            """
        )
    )

    # --- Step 2: fail safe on anything still incompatible ---
    remaining_null_tenant = _scalar(
        conn, "SELECT COUNT(*) FROM message_revocations WHERE tenant_id IS NULL"
    )
    if remaining_null_tenant:
        bad_ids = _rows(
            conn,
            "SELECT id, revoke_event_message_id FROM message_revocations "
            "WHERE tenant_id IS NULL ORDER BY id LIMIT 20",
        )
        raise MessageRevocationIntegrityError(
            f"{remaining_null_tenant} message_revocations row(s) still have tenant_id "
            "IS NULL after deterministic recovery -- their revoke_event_message_id "
            "either has no matching archive_messages row, or that row also has "
            "tenant_id IS NULL, so no safe tenant could be recovered. Affected "
            f"(message_revocations.id, revoke_event_message_id) pairs (up to 20): "
            f"{[tuple(r) for r in bad_ids]}. Resolve manually (or run "
            "scripts/check_message_revocations_integrity.py for a full report) "
            "before re-running this migration."
        )

    event_tenant_mismatch = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM message_revocations mr
        JOIN archive_messages am ON am.id = mr.revoke_event_message_id
        WHERE mr.tenant_id != am.tenant_id
        """,
    )
    if event_tenant_mismatch:
        bad_ids = _rows(
            conn,
            """
            SELECT mr.id, mr.tenant_id, am.tenant_id FROM message_revocations mr
            JOIN archive_messages am ON am.id = mr.revoke_event_message_id
            WHERE mr.tenant_id != am.tenant_id ORDER BY mr.id LIMIT 20
            """,
        )
        raise MessageRevocationIntegrityError(
            f"{event_tenant_mismatch} message_revocations row(s) have a tenant_id "
            "that disagrees with the tenant of their own revoke_event_message_id's "
            "archive_messages row. This is not automatically repairable (which "
            "tenant is correct cannot be inferred). Affected "
            f"(message_revocations.id, mr.tenant_id, event tenant_id) (up to 20): "
            f"{[tuple(r) for r in bad_ids]}. Resolve manually before re-running "
            "this migration."
        )

    original_tenant_mismatch = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM message_revocations mr
        JOIN archive_messages am ON am.id = mr.original_message_id
        WHERE mr.original_message_id IS NOT NULL AND mr.tenant_id != am.tenant_id
        """,
    )
    if original_tenant_mismatch:
        bad_ids = _rows(
            conn,
            """
            SELECT mr.id, mr.tenant_id, am.tenant_id FROM message_revocations mr
            JOIN archive_messages am ON am.id = mr.original_message_id
            WHERE mr.original_message_id IS NOT NULL AND mr.tenant_id != am.tenant_id
            ORDER BY mr.id LIMIT 20
            """,
        )
        raise MessageRevocationIntegrityError(
            f"{original_tenant_mismatch} message_revocations row(s) are linked to "
            "an original_message_id belonging to a different tenant than the "
            "association row itself declares. This is not automatically "
            "repairable (neither the original message's tenant nor the "
            "association's tenant is silently changed, and no alternative "
            "original message is guessed). Affected (message_revocations.id, "
            f"mr.tenant_id, original's tenant_id) (up to 20): "
            f"{[tuple(r) for r in bad_ids]}. Resolve manually before re-running "
            "this migration."
        )

    duplicate_events = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM (
            SELECT revoke_event_message_id FROM message_revocations
            GROUP BY revoke_event_message_id HAVING COUNT(*) > 1
        ) dup
        """,
    )
    if duplicate_events:
        bad_ids = _rows(
            conn,
            """
            SELECT revoke_event_message_id, COUNT(*) FROM message_revocations
            GROUP BY revoke_event_message_id HAVING COUNT(*) > 1
            ORDER BY revoke_event_message_id LIMIT 20
            """,
        )
        raise MessageRevocationIntegrityError(
            f"{duplicate_events} revoke_event_message_id value(s) have more than "
            "one message_revocations row. This migration does not guess which "
            "row is authoritative and discard the others -- that would silently "
            "destroy association state. Affected (revoke_event_message_id, "
            f"row_count) (up to 20): {[tuple(r) for r in bad_ids]}. Resolve "
            "manually (inspect whether the duplicates are semantically "
            "identical and, if so, deterministically merge/delete the "
            "redundant ones yourself) before re-running this migration."
        )

    # Defensive only -- the pre-existing single-column foreign keys this
    # migration is about to replace already make this scenario
    # impossible, but the check costs nothing and documents the
    # invariant explicitly rather than assuming it silently.
    missing_event_refs = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM message_revocations mr
        LEFT JOIN archive_messages am ON am.id = mr.revoke_event_message_id
        WHERE am.id IS NULL
        """,
    )
    if missing_event_refs:
        raise MessageRevocationIntegrityError(
            f"{missing_event_refs} message_revocations row(s) reference a "
            "revoke_event_message_id with no matching archive_messages row -- "
            "this should be impossible under the existing foreign key and "
            "indicates database corruption. Resolve manually before "
            "re-running this migration."
        )

    # --- Step 3: schema changes (only reached once the data is clean) ---
    op.create_unique_constraint(
        "uq_archive_messages_tenant_id_id", "archive_messages", ["tenant_id", "id"]
    )

    op.alter_column("message_revocations", "tenant_id", nullable=False)

    op.drop_constraint(
        "uq_message_revocations_tenant_revoke_event",
        "message_revocations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_message_revocations_revoke_event_message_id",
        "message_revocations",
        ["revoke_event_message_id"],
    )

    op.drop_constraint(
        "message_revocations_revoke_event_message_id_fkey",
        "message_revocations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "message_revocations_original_message_id_fkey",
        "message_revocations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_message_revocations_tenant_revoke_event",
        "message_revocations",
        "archive_messages",
        ["tenant_id", "revoke_event_message_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_message_revocations_tenant_original",
        "message_revocations",
        "archive_messages",
        ["tenant_id", "original_message_id"],
        ["tenant_id", "id"],
    )


def downgrade() -> None:
    # Restores the exact 0010 schema. Does not, and cannot, restore any
    # tenant_id that was NULL before this migration's Step 1 recovery --
    # that data is genuinely gone once overwritten, but the recovered
    # value is the CORRECT one (derived from the authoritative event
    # record), not a fabricated one, so nothing legitimate is lost. No
    # rows are ever deleted by this downgrade.
    op.drop_constraint(
        "fk_message_revocations_tenant_original",
        "message_revocations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_message_revocations_tenant_revoke_event",
        "message_revocations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "message_revocations_original_message_id_fkey",
        "message_revocations",
        "archive_messages",
        ["original_message_id"],
        ["id"],
    )
    op.create_foreign_key(
        "message_revocations_revoke_event_message_id_fkey",
        "message_revocations",
        "archive_messages",
        ["revoke_event_message_id"],
        ["id"],
    )

    op.drop_constraint(
        "uq_message_revocations_revoke_event_message_id",
        "message_revocations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_message_revocations_tenant_revoke_event",
        "message_revocations",
        ["tenant_id", "revoke_event_message_id"],
    )

    op.alter_column("message_revocations", "tenant_id", nullable=True)

    op.drop_constraint(
        "uq_archive_messages_tenant_id_id", "archive_messages", type_="unique"
    )
