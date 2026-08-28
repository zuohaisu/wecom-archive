#!/usr/bin/env python3
"""Run one tenant-scoped reachability automation pass under the shared lock.

Usage (from backend/):
    python scripts/run_reachability_automation_once.py incremental|reconcile

Tenant scope comes from an explicit worker tenant selector or all active
TenantWecomConfig rows. It is intentionally never accepted on argv or emitted.
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
from app.services.tenant_credentials import (
    active_tenant_configs,
    active_tenant_ids,
    tenant_log_tag,
)

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


def _resolve_tenant_ids(db: Session) -> tuple[list[str], list[str]]:
    """Return selected and config-missing active tenants without CorpID lookup."""
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
        return ([tenant_id_env], []) if row else ([], [tenant_id_env])

    active_ids = active_tenant_ids(db)
    configured_ids = [config.tenant_id for config in active_tenant_configs(db)]
    configured_set = set(configured_ids)
    return configured_ids, [tenant_id for tenant_id in active_ids if tenant_id not in configured_set]


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
                tenant_ids, missing_config_ids = _resolve_tenant_ids(db)
                completed = failed = 0
                for missing_tenant_id in missing_config_ids:
                    failed += 1
                    print(
                        f"[WARN] reachability automation tenant={tenant_log_tag(missing_tenant_id)} "
                        "status=tenant_config_unavailable count=0",
                        flush=True,
                    )
                for tenant_id in tenant_ids:
                    try:
                        outcome = run_automation(db, tenant_id, args.mode)
                    except Exception:  # noqa: BLE001 -- one tenant must not stop another
                        db.rollback()
                        failed += 1
                        print(
                            f"[WARN] reachability automation tenant={tenant_log_tag(tenant_id)} "
                            "status=error count=0",
                            flush=True,
                        )
                        continue
                    if outcome in {"completed", "no_data", "locked"}:
                        completed += 1
                    else:
                        failed += 1
                    level = "PASS" if outcome in {"completed", "no_data", "locked"} else "FAIL"
                    print(
                        f"[{level}] reachability automation tenant={tenant_log_tag(tenant_id)} "
                        f"mode={args.mode} status={outcome} count=0",
                        flush=True,
                    )
        except Exception:
            print(f"[FAIL] reachability automation mode={args.mode} status=error count=0", flush=True)
            return 1
        if not tenant_ids and not missing_config_ids:
            print(
                f"[INFO] reachability automation mode={args.mode} status=no-active-tenants count=0",
                flush=True,
            )
            return 0
        print(
            f"[INFO] reachability automation mode={args.mode} tenant_completed={completed} "
            f"tenant_failed={failed} count=0",
            flush=True,
        )
        return 1 if completed == 0 and failed else 0
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
