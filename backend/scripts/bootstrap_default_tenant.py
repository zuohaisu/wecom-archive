#!/usr/bin/env python3
"""
Idempotent default-tenant bootstrap for RND-111.

Run once after alembic upgrade head (migration 0002) has been applied.
Safe to re-run: all inserts use ON CONFLICT DO NOTHING; NOT NULL enforcement
is guarded by a pg_attribute check.

Steps:
  1. Create default tenant row (id=DEFAULT_TENANT_ID, slug='default').
  2. Create tenant_wecom_configs row from WECOM_CORP_ID / WECOM_AGENT_ID /
     WECOM_OAUTH_SECRET / ADMIN_DOMAIN env vars.
  3. Backfill archive_messages.tenant_id WHERE tenant_id IS NULL.
  4. Backfill archive_message_recipients.tenant_id WHERE tenant_id IS NULL.
  5. Backfill sync_states.tenant_id WHERE tenant_id IS NULL.
  6. Backfill contacts.tenant_id WHERE tenant_id IS NULL.
  7. Backfill media_files.tenant_id WHERE tenant_id IS NULL.
  8. Enforce NOT NULL on all five backfilled columns (idempotent guard).
  9. Print row-count diagnostics (no message content, no secrets).

Usage (from backend/):
    python scripts/bootstrap_default_tenant.py

Required env vars:
    DATABASE_URL
    WECOM_CORP_ID
    WECOM_AGENT_ID
    WECOM_OAUTH_SECRET

Optional env vars:
    ADMIN_DOMAIN    Admin console domain registered in WeCom Admin
                    (e.g. admin.yourcompany.com).  Required for Phase 2
                    (RND-110) OAuth; can be left empty for Phase 1.

Security constraints:
    - WECOM_OAUTH_SECRET is written to tenant_wecom_configs.app_secret.
      Phase 1 stores it plaintext (internal single-tenant deployment).
      Phase 3 must encrypt at rest before storing.
    - app_secret is NOT printed or logged.
    - No message content, decrypted payloads, or archive data is read.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Well-known UUIDs for the single default tenant — stable across deployments.
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_TENANT_CONFIG_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _col_is_not_null(session: Session, table: str, column: str) -> bool:
    """Return True if the column already has NOT NULL in pg_attribute."""
    row = session.execute(
        text(
            """
            SELECT a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            WHERE c.relname = :tbl AND a.attname = :col
            """
        ),
        {"tbl": table, "col": column},
    ).fetchone()
    return bool(row and row[0])


def _null_count(session: Session, table: str, column: str) -> int:
    row = session.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")  # noqa: S608
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Bootstrap steps
# ---------------------------------------------------------------------------


def _step_upsert_tenant(session: Session) -> None:
    session.execute(
        text(
            """
            INSERT INTO tenants (id, name, slug, is_active, created_at, updated_at)
            VALUES (:id, :name, :slug, true, NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": DEFAULT_TENANT_ID,
            "name": DEFAULT_TENANT_NAME,
            "slug": DEFAULT_TENANT_SLUG,
        },
    )
    session.commit()
    print(f"[INFO] tenant upsert done (id={DEFAULT_TENANT_ID})", flush=True)


def _step_upsert_config(
    session: Session,
    corp_id: str,
    agent_id: str,
    app_secret: str,
    callback_domain: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO tenant_wecom_configs
                (id, tenant_id, corp_id, agent_id, app_secret,
                 callback_domain, is_active, created_at, updated_at)
            VALUES
                (:id, :tenant_id, :corp_id, :agent_id, :app_secret,
                 :callback_domain, true, NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": DEFAULT_TENANT_CONFIG_ID,
            "tenant_id": DEFAULT_TENANT_ID,
            "corp_id": corp_id,
            "agent_id": agent_id,
            "app_secret": app_secret,
            "callback_domain": callback_domain,
        },
    )
    session.commit()
    # Log structural success only — do NOT print corp_id or secret.
    print("[INFO] tenant_wecom_config upsert done", flush=True)


def _step_backfill(session: Session, table: str) -> None:
    before = _null_count(session, table, "tenant_id")
    if before == 0:
        print(f"[INFO] {table}: no NULL tenant_id rows — backfill skipped", flush=True)
        return

    session.execute(
        text(
            f"UPDATE {table} SET tenant_id = :tid WHERE tenant_id IS NULL"  # noqa: S608
        ),
        {"tid": DEFAULT_TENANT_ID},
    )
    session.commit()
    after = _null_count(session, table, "tenant_id")
    print(
        f"[INFO] {table}: backfilled {before} row(s); remaining NULL: {after}",
        flush=True,
    )
    if after != 0:
        print(
            f"[WARN] {table}: {after} row(s) still NULL after backfill — "
            "check for concurrent inserts",
            flush=True,
        )


def _step_enforce_not_null(session: Session, table: str) -> None:
    if _col_is_not_null(session, table, "tenant_id"):
        print(
            f"[INFO] {table}.tenant_id already NOT NULL — skipped", flush=True
        )
        return

    remaining = _null_count(session, table, "tenant_id")
    if remaining != 0:
        print(
            f"[FAIL] {table}: {remaining} NULL tenant_id row(s) remain — "
            "cannot enforce NOT NULL. Re-run backfill step.",
            flush=True,
        )
        sys.exit(1)

    session.execute(
        text(
            f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL"  # noqa: S608
        )
    )
    session.commit()
    print(f"[INFO] {table}.tenant_id set NOT NULL", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_ARCHIVE_TABLES = [
    "archive_messages",
    "archive_message_recipients",
    "sync_states",
    "contacts",
    "media_files",
]


def main() -> None:
    database_url = _require_env("DATABASE_URL")
    corp_id = _require_env("WECOM_CORP_ID")
    agent_id = _require_env("WECOM_AGENT_ID")
    app_secret = _require_env("WECOM_OAUTH_SECRET")
    callback_domain = _optional_env("ADMIN_DOMAIN", "")

    if not callback_domain:
        print(
            "[WARN] ADMIN_DOMAIN is not set. callback_domain will be empty. "
            "Set it before implementing RND-110 (WeCom OAuth).",
            flush=True,
        )

    engine = create_engine(database_url)

    with Session(engine) as session:
        print("[INFO] bootstrap_default_tenant starting …", flush=True)

        _step_upsert_tenant(session)
        _step_upsert_config(session, corp_id, agent_id, app_secret, callback_domain)

        for table in _ARCHIVE_TABLES:
            _step_backfill(session, table)

        for table in _ARCHIVE_TABLES:
            _step_enforce_not_null(session, table)

    print("[PASS] bootstrap_default_tenant completed successfully", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
