"""Run a small durable external-contact refresh queue batch.

This CLI is invoked by a systemd path unit for low-latency event work and by a
short retry timer for jobs that could not yet be read from the WeCom API. It
prints aggregate counts only.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.external_contact_refresh_worker import run_external_contact_refresh_queue


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(name)
    return value


def _positive_int(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return default


def main() -> int:
    try:
        database_url = _require_env("DATABASE_URL")
        corp_id = _require_env("WECOM_CORP_ID")
    except RuntimeError:
        print("[FAIL] external_contact_refresh configuration_missing", flush=True)
        return 1

    external_secret = os.environ.get("WECOM_EXTERNAL_CONTACT_SECRET", "").strip()
    if not external_secret:
        print("[INFO] external_contact_refresh status=skipped reason=not_configured", flush=True)
        return 0

    limit = _positive_int(os.environ.get("EXTERNAL_CONTACT_REFRESH_BATCH_SIZE", "25"), 25)
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            summary = run_external_contact_refresh_queue(
                session,
                corp_id,
                external_secret,
                limit=limit,
            )
    except Exception:  # noqa: BLE001 -- do not expose DB or API error details
        print("[FAIL] external_contact_refresh status=failed", flush=True)
        return 1

    print(
        "[INFO] external_contact_refresh status=completed "
        f"selected={summary.selected} refreshed={summary.refreshed} "
        f"unavailable={summary.unavailable}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
