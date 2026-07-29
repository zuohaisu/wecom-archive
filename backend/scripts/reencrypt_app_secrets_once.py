#!/usr/bin/env python3
"""One-shot, idempotent encryption backfill for tenant WeCom app secrets.

Usage (from ``backend/``)::

    python scripts/reencrypt_app_secrets_once.py --dry-run
    python scripts/reencrypt_app_secrets_once.py

Both ``DATABASE_URL`` and ``FIELD_ENCRYPTION_KEY`` must be configured. The
script never prints app secrets or encrypted tokens. ``--dry-run`` reports the
number of plaintext rows that would be changed and does not write to the DB.
Rows already recognizable as Fernet tokens are skipped, so repeat live runs
are no-ops for them.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Allow running from backend/ without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crypto import encrypt_value, is_encrypted  # noqa: E402
from app.db.models import TenantWecomConfig  # noqa: E402


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Missing required environment variable: {name}", flush=True)
        sys.exit(1)
    return value


def reencrypt_app_secrets(session: Session, *, dry_run: bool) -> tuple[int, int]:
    """Return ``(would_encrypt, skipped_encrypted)`` without exposing values.

    The caller owns the transaction so dry-run callers can prove no write was
    committed and live callers can commit the complete small backfill.
    """
    would_encrypt = 0
    skipped_encrypted = 0
    for config in session.query(TenantWecomConfig).order_by(TenantWecomConfig.id):
        if is_encrypted(config.app_secret):
            skipped_encrypted += 1
            continue
        would_encrypt += 1
        if not dry_run:
            config.set_app_secret(config.app_secret)
    return would_encrypt, skipped_encrypted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encrypt plaintext tenant_wecom_configs.app_secret rows once"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report plaintext rows that would be encrypted without writing",
    )
    args = parser.parse_args()

    database_url = _require_env("DATABASE_URL")
    # Validate before opening a transaction so bad configuration cannot result
    # in partial processing. The throwaway token is never stored or logged.
    _require_env("FIELD_ENCRYPTION_KEY")
    encrypt_value("")
    engine = create_engine(database_url)

    with Session(engine) as session:
        would_encrypt, skipped_encrypted = reencrypt_app_secrets(
            session, dry_run=args.dry_run
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"[INFO] re-encryption mode: {mode}", flush=True)
    print(f"[INFO] app_secret rows to encrypt: {would_encrypt}", flush=True)
    print(f"[INFO] app_secret rows already encrypted: {skipped_encrypted}", flush=True)
    print("[PASS] reencrypt_app_secrets_once completed", flush=True)


if __name__ == "__main__":
    main()
