#!/usr/bin/env python3
"""Bounded tenant-scoped backfill for current WeCom customer-group names.

This is intentionally an operator-triggered, limited reconciliation.  It does
not decrypt messages, alter archive rows, or print room IDs, names, tokens, or
provider responses.  Re-run it to observe renamed groups; unchanged metadata
causes no database write.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import get_wecom_token  # noqa: E402
from app.services.external_contact_sync import _require_tenant_id  # noqa: E402
from app.services.group_chat_metadata import backfill_group_chat_metadata  # noqa: E402

_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable not set or empty: {name}")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded WeCom group-chat metadata backfill")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    args = parser.parse_args()
    if not 1 <= args.limit <= _MAX_LIMIT:
        parser.error(f"--limit must be between 1 and {_MAX_LIMIT}")
    return args


def main() -> int:
    args = _parse_args()
    try:
        database_url = _require_env("DATABASE_URL")
        corp_id = _require_env("WECOM_CORP_ID")
        external_secret = _require_env("WECOM_EXTERNAL_CONTACT_SECRET")
        token = get_wecom_token(
            corp_id, external_secret, cache_key=f"{corp_id}:external_contact"
        )
    except Exception:
        print("[FAIL] group_chat_metadata initialization failed", flush=True)
        return 1

    try:
        with Session(create_engine(database_url)) as session:
            tenant_id = _require_tenant_id(session, corp_id)
            summary = backfill_group_chat_metadata(
                session,
                tenant_id=tenant_id,
                access_token=token,
                limit=args.limit,
            )
            session.commit()
    except Exception:
        print("[FAIL] group_chat_metadata backfill failed", flush=True)
        return 1

    print(f"[INFO] group_chat_metadata scanned={summary.scanned}", flush=True)
    print(f"[INFO] group_chat_metadata resolved={summary.resolved}", flush=True)
    print(f"[INFO] group_chat_metadata unresolved={summary.unresolved}", flush=True)
    print(f"[INFO] group_chat_metadata created={summary.created}", flush=True)
    print(f"[INFO] group_chat_metadata updated={summary.updated}", flush=True)
    print(f"[INFO] group_chat_metadata skipped={summary.skipped}", flush=True)
    print(f"[INFO] group_chat_metadata errors={summary.errors}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
