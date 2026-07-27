"""Search and composite indexes (RND-191).

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-27

RND-191 profiling identified two index gaps in the API/query hot paths:

1. search_messages() / search_contacts() (app/routers/search.py) filter with
   ILIKE '%term%', which cannot use the existing FTS GIN index
   (ix_archive_messages_content_text_fts, a to_tsvector('simple', ...)
   expression index -- tsvector indexes only support @@ to_tsquery(...)
   matching, never a plain ILIKE substring predicate) and falls back to a
   full sequential scan. pg_trgm's GIN trigram opclass DOES let the planner
   use an index for ILIKE '%term%' -- same substring/case-insensitive
   matching semantics as before (nothing about what matches changes), just
   index-backed instead of a sequential scan. Added on every column
   search.py's ILIKE predicates touch: archive_messages.content_text,
   contacts.name, contacts.wecom_userid, admin_users.name,
   admin_users.wecom_user_id.

2. archive_messages / archive_message_recipients only had single-column
   indexes (tenant_id alone, msgtime alone, roomid alone, decrypt_status
   not indexed at all, ...). Since every production query is tenant-scoped,
   a composite index with tenant_id as the leading column serves these hot
   paths far better than Postgres intersecting two single-column indexes:
     - archive_messages(tenant_id, msgtime, id): timeline/search cursor
       pagination's ORDER BY msgtime, id WHERE tenant_id = :t
     - archive_messages(tenant_id, roomid): group-room membership
       resolution (_group_room_message_ids and friends)
     - archive_messages(tenant_id, decrypt_status, is_revoked):
       search_messages' baseline filter (every search request carries both
       predicates alongside tenant_id)
     - archive_message_recipients(tenant_id, receiver_userid): recipient-
       side membership/participation lookups

All additive, all safely reversible (downgrade drops exactly what upgrade
created); no column or data changes. See models.py's ArchiveMessage /
ArchiveMessageRecipient / Contact / AdminUser __table_args__ for the
declarative mirror of these same indexes.

Operational note: plain CREATE INDEX takes a table-level lock that blocks
writes for the duration of the build -- existing migrations in this repo
(0001, 0008, 0012) all create their GIN/expression indexes the same way, so
this migration follows that established convention rather than introducing
a new CONCURRENTLY/autocommit pattern. On a large production
archive_messages table this can mean a multi-second-or-longer write pause
during deploy; if that's not acceptable for a live cutover, rerun the
statements below with CREATE INDEX CONCURRENTLY outside of a transaction
instead of via this migration.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        "CREATE INDEX ix_archive_messages_content_text_trgm "
        "ON archive_messages USING gin (content_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_contacts_name_trgm ON contacts USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_contacts_wecom_userid_trgm "
        "ON contacts USING gin (wecom_userid gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_admin_users_name_trgm ON admin_users USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_admin_users_wecom_user_id_trgm "
        "ON admin_users USING gin (wecom_user_id gin_trgm_ops)"
    )

    op.create_index(
        "ix_archive_messages_tenant_msgtime_id",
        "archive_messages",
        ["tenant_id", "msgtime", "id"],
    )
    op.create_index(
        "ix_archive_messages_tenant_roomid",
        "archive_messages",
        ["tenant_id", "roomid"],
    )
    op.create_index(
        "ix_archive_messages_tenant_decrypt_revoked",
        "archive_messages",
        ["tenant_id", "decrypt_status", "is_revoked"],
    )
    op.create_index(
        "ix_archive_message_recipients_tenant_receiver",
        "archive_message_recipients",
        ["tenant_id", "receiver_userid"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_archive_message_recipients_tenant_receiver",
        table_name="archive_message_recipients",
    )
    op.drop_index(
        "ix_archive_messages_tenant_decrypt_revoked", table_name="archive_messages"
    )
    op.drop_index("ix_archive_messages_tenant_roomid", table_name="archive_messages")
    op.drop_index(
        "ix_archive_messages_tenant_msgtime_id", table_name="archive_messages"
    )

    op.execute("DROP INDEX IF EXISTS ix_admin_users_wecom_user_id_trgm")
    op.execute("DROP INDEX IF EXISTS ix_admin_users_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_contacts_wecom_userid_trgm")
    op.execute("DROP INDEX IF EXISTS ix_contacts_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_archive_messages_content_text_trgm")
    # pg_trgm itself is left installed on downgrade -- dropping a shared
    # extension other objects might depend on is out of scope for a single
    # migration's rollback, and CREATE EXTENSION IF NOT EXISTS on a future
    # re-upgrade is a no-op if it's still present.
