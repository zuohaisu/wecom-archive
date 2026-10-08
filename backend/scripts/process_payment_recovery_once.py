#!/usr/bin/env python3
"""Run one bounded WeChat payment recovery or T+1 reconciliation batch."""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.settings import APP_EDITION_CLOUD, get_app_edition  # noqa: E402
from app.services.payment_recovery import (  # noqa: E402
    RECONCILIATION_BATCH_LIMIT,
    RECOVERY_BATCH_LIMIT,
    run_payment_reconciliation_once,
    run_payment_recovery_once,
)
from app.services.wechat_pay import get_wechat_pay_provider  # noqa: E402


def _mode(argv: list[str]) -> str | None:
    if len(argv) != 2 or argv[1] not in {"recovery", "reconciliation"}:
        return None
    return argv[1]


def main(argv: list[str] | None = None) -> int:
    try:
        edition = get_app_edition()
    except ValueError:
        print("[FAIL] payment_recovery invalid_edition", flush=True)
        return 1
    if edition != APP_EDITION_CLOUD:
        print("[INFO] payment_recovery skipped selfhost edition", flush=True)
        return 0
    selected = _mode(argv or sys.argv)
    if selected is None:
        print("[FAIL] payment_recovery invalid_mode", flush=True)
        return 1
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] payment_recovery configuration_missing", flush=True)
        return 1
    try:
        provider = get_wechat_pay_provider()
        factory = sessionmaker(bind=create_engine(database_url), expire_on_commit=False)
        summary = (
            run_payment_recovery_once(factory, provider, limit=RECOVERY_BATCH_LIMIT)
            if selected == "recovery"
            else run_payment_reconciliation_once(
                factory, provider, limit=RECONCILIATION_BATCH_LIMIT
            )
        )
    except Exception:  # noqa: BLE001 - never print provider/DB details
        print("[FAIL] payment_recovery processing_failed", flush=True)
        return 1
    print(
        "[INFO] payment_recovery processing_completed "
        f"mode={selected} claimed={summary.claimed} recovered={summary.recovered} "
        f"pending={summary.pending} manual_recovery={summary.manual_recovery} "
        f"failed={summary.failed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
