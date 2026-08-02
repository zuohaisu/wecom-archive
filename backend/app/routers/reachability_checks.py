"""Authenticated APIs for persistent tenant reachability snapshots."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser
from app.db.session import get_db
from app.schemas.reachability_checks import (
    ReachabilityCheckSnapshotOut,
    ReachabilityCheckStartIn,
)
from app.services.reachability_check_service import (
    create_or_reuse_run,
    latest_snapshot,
    mark_run_error,
    snapshot_for_run,
)

router = APIRouter()
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_RUNNER_SCRIPT = _BACKEND_DIR / "scripts" / "run_reachability_check_once.py"


def _launch_runner(public_id: str) -> None:
    """Detach a one-shot scanner; argv contains only the approved public id."""
    subprocess.Popen(
        [sys.executable, str(_RUNNER_SCRIPT), public_id],
        cwd=str(_BACKEND_DIR),
        close_fds=True,
    )


@router.get(
    "/api/admin/reachability-checks/latest",
    response_model=ReachabilityCheckSnapshotOut,
)
def get_latest_reachability_check(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
) -> ReachabilityCheckSnapshotOut:
    _, tenant_id = auth
    return ReachabilityCheckSnapshotOut(**latest_snapshot(db, tenant_id))


@router.post(
    "/api/admin/reachability-checks",
    response_model=ReachabilityCheckSnapshotOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_reachability_check(
    request: Request,
    _payload: ReachabilityCheckStartIn = Body(default_factory=ReachabilityCheckStartIn),
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
) -> ReachabilityCheckSnapshotOut:
    """Create/reuse a DB-guarded run and return before scanner work begins."""
    if "tenant_id" in request.query_params:
        raise HTTPException(status_code=422, detail="tenant scope is session-derived")
    _, tenant_id = auth
    run, created = create_or_reuse_run(db, tenant_id)
    if created:
        try:
            _launch_runner(run.public_id)
        except Exception:
            mark_run_error(db, tenant_id, run.public_id, "process_start_failed")
            # Re-read only in the authenticated tenant scope after failure.
            return ReachabilityCheckSnapshotOut(**latest_snapshot(db, tenant_id))
    return ReachabilityCheckSnapshotOut(**snapshot_for_run(run))
