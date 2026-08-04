"""Real-PostgreSQL round-trip coverage for migration 0034 (RND-321).

QA found that an earlier version of this migration mutated existing
admin_users.wecom_user_id at upgrade time to work around a legacy-identity
collision, which made 0033 -> 0034 -> 0033 irreversible for that data on a
real Postgres round trip. The fix moved the collision handling to request
resolution time (app/routers/users.py) and reverted the migration to
schema-plus-read-only-backfill only. This test is the regression guard for
that: it seeds a pre-existing access_requested account, drives a real
upgrade/downgrade round trip via Alembic against DATABASE_URL, and asserts
the account's data is byte-for-byte unchanged afterward.

Skips (does not fail) when DATABASE_URL is not set or the target database
is not already at head 0034, matching the skip-gracefully convention used
by test_tenant_foundation.py's own migration-dependent tests.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_config():
    from alembic.config import Config

    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    return config


@pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")
def test_migration_0034_roundtrip_preserves_legacy_access_requested_account_data() -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    config = _alembic_config()
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    engine = create_engine(os.environ["DATABASE_URL"])

    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if current != head or head != "0034":
        pytest.skip(f"Database not at head 0034 (at {current!r}, head {head!r}) — run alembic upgrade head first")

    # tenants.id / admin_users.id are varchar(36) — plain UUIDs, not
    # prefixed, to fit exactly; wecom_user_id (varchar(64)) has room.
    tenant_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    seeded = {
        "wecom_user_id": f"wecom-roundtrip-{uuid.uuid4()}",
        "name": "Roundtrip Legacy Orphan",
        "role": "readonlyaudit",
        "status": "disabled",
        "invite_status": "access_requested",
    }

    try:
        command.downgrade(config, "0033")

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, is_active, created_at, updated_at) "
                    "VALUES (:id, :id, :id, true, now(), now())"
                ),
                {"id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO admin_users "
                    "(id, tenant_id, wecom_user_id, name, role, status, invite_status, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :wecom_user_id, :name, :role, :status, :invite_status, now(), now())"
                ),
                {"id": account_id, "tenant_id": tenant_id, **seeded},
            )

        command.upgrade(config, "0034")

        with engine.connect() as conn:
            identity_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM admin_login_identities "
                    "WHERE admin_user_id = :id"
                ),
                {"id": account_id},
            ).scalar()
        assert identity_count == 0, (
            "an access_requested-tagged legacy row must never get an automatic "
            "login identity from the backfill"
        )

        command.downgrade(config, "0033")

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT wecom_user_id, name, role, status, invite_status "
                    "FROM admin_users WHERE id = :id"
                ),
                {"id": account_id},
            ).mappings().one()

        assert dict(row) == seeded, (
            "0033 -> 0034 -> 0033 must restore this pre-existing admin_users "
            "row exactly — migration 0034 must never mutate it"
        )
    finally:
        command.upgrade(config, head)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM admin_users WHERE id = :id"), {"id": account_id})
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
