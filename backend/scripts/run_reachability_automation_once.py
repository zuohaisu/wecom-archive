#!/usr/bin/env python3
"""Run one tenant-scoped reachability automation pass under the shared lock.

Usage (from backend/):
    python scripts/run_reachability_automation_once.py incremental|reconcile

Tenant scope is resolved from the active configured corporation in the
process environment; it is intentionally never accepted on argv or emitted.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys

# Direct script execution sets sys.path[0] to backend/scripts rather than
# backend. Keep the deployed ExecStart contract while making app importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import TenantWecomConfig
from app.services.reachability_automation_service import AUTOMATION_MODES, run_automation

_DEFAULT_LOCK_PATH = "/srv/apps/wecom-archive-365/shared/run/wecom-archive-reachability-check.lock"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run reachability automation once")
    parser.add_argument("mode", choices=sorted(AUTOMATION_MODES))
    return parser.parse_args()


def _acquire_lock() -> int | None:
    path = os.environ.get("REACHABILITY_AUTOMATION_LOCK_PATH", _DEFAULT_LOCK_PATH).strip()
    if not path:
        return None
    try:
        os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        return None
    except OSError:
        return -1


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _resolve_tenant_id(db: Session) -> str | None:
    # Per-tenant mode first: WECOM_TENANT_ID is authoritative when set (the
    # per-tenant worker chain merges it into the child environment).
    tenant_id_env = os.environ.get("WECOM_TENANT_ID", "").strip()
    if tenant_id_env:
        row = (
            db.query(TenantWecomConfig.tenant_id)
            .filter(
                TenantWecomConfig.tenant_id == tenant_id_env,
                TenantWecomConfig.is_active.is_(True),
            )
            .first()
        )
        return row[0] if row else None
    corp_id = os.environ.get("WECOM_CORP_ID", "").strip()
    if not corp_id:
        return None
    row = (
        db.query(TenantWecomConfig.tenant_id)
        .filter(TenantWecomConfig.corp_id == corp_id, TenantWecomConfig.is_active.is_(True))
        .first()
    )
    return row[0] if row else None


def main() -> int:
    args = _parse_args()
    lock_fd = _acquire_lock()
    if lock_fd is None:
        print(f"[INFO] reachability automation mode={args.mode} status=locked count=0", flush=True)
        return 0
    if lock_fd < 0:
        print(f"[FAIL] reachability automation mode={args.mode} status=lock_error count=0", flush=True)
        return 1
    try:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            print(f"[FAIL] reachability automation mode={args.mode} status=database_unavailable count=0", flush=True)
            return 1
        try:
            with Session(create_engine(database_url)) as db:
                tenant_id = _resolve_tenant_id(db)
                if tenant_id is None:
                    print(f"[FAIL] reachability automation mode={args.mode} status=tenant_unavailable count=0", flush=True)
                    return 1
                outcome = run_automation(db, tenant_id, args.mode)
        except Exception:
            print(f"[FAIL] reachability automation mode={args.mode} status=error count=0", flush=True)
            return 1
        code = 0 if outcome in {"completed", "no_data", "locked"} else 1
        level = "PASS" if code == 0 else "FAIL"
        print(f"[{level}] reachability automation mode={args.mode} status={outcome} count=0", flush=True)
        return code
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
