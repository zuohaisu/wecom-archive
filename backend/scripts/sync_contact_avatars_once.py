#!/usr/bin/env python3
"""Controlled, non-production-only backfill for RND-371 contact avatars.

The script intentionally refreshes only archive staff identities. External
contacts are covered by their existing daily reconciliation. It refuses
APP_ENV=production and requires an explicit arming flag; production backfill
requires a separately approved operational runbook.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.avatar_sync import reconcile_internal_contact_avatars
from app.services.external_contact_sync import _require_tenant_id


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(name)
    return value


def guard_environment(environ: dict[str, str] | None = None) -> None:
    env = environ or os.environ
    if env.get("RND371_AVATAR_BACKFILL", "").strip() != "1":
        raise RuntimeError("RND371_AVATAR_BACKFILL")
    if env.get("APP_ENV", "").strip().lower() == "production":
        raise RuntimeError("production_refused")


def _positive(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return default


def main() -> int:
    try:
        guard_environment()
        database_url = _require_env("DATABASE_URL")
        corp_id = _require_env("WECOM_CORP_ID")
        oauth_secret = _require_env("WECOM_OAUTH_SECRET")
    except RuntimeError as exc:
        print(f"[FAIL] avatar_backfill {exc}", flush=True)
        return 1

    batch_size = _positive(os.environ.get("RND371_AVATAR_BACKFILL_BATCH_SIZE", "50"), 50)
    max_batches = _positive(os.environ.get("RND371_AVATAR_BACKFILL_MAX_BATCHES", "20"), 20)
    selected = ready = unavailable = 0
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            tenant_id = _require_tenant_id(session, corp_id)
            for _ in range(max_batches):
                batch = reconcile_internal_contact_avatars(
                    session,
                    tenant_id,
                    corp_id,
                    oauth_secret,
                    limit=batch_size,
                )
                session.commit()
                selected += batch.selected
                ready += batch.ready
                unavailable += batch.unavailable
                if batch.selected < batch_size:
                    break
    except Exception as exc:  # noqa: BLE001 -- never expose DB/provider details
        print(f"[FAIL] avatar_backfill {type(exc).__name__}", flush=True)
        return 1

    print(
        "[INFO] avatar_backfill status=completed "
        f"selected={selected} ready={ready} unavailable={unavailable}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
