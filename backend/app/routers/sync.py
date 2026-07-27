"""Tenant-scoped archive-sync status and manual trigger endpoints (RND-211)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db.models import AdminUser, SyncState, TenantWecomConfig
from app.db.session import get_db, get_engine
from app.schemas.sync import SyncNowResponse, SyncStatusResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_SYNC_NOW_COOLDOWN = timedelta(seconds=30)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = _BACKEND_DIR / "scripts" / "run_archive_worker_once.py"


def _active_config(db: Session, tenant_id: str) -> Optional[TenantWecomConfig]:
    return (
        db.query(TenantWecomConfig)
        .filter(
            TenantWecomConfig.tenant_id == tenant_id,
            TenantWecomConfig.is_active.is_(True),
        )
        .first()
    )


def _sync_state(db: Session, tenant_id: str, corp_id: str) -> Optional[SyncState]:
    return (
        db.query(SyncState)
        .filter(SyncState.tenant_id == tenant_id, SyncState.corp_id == corp_id)
        .first()
    )


def _status_response(row: Optional[SyncState]) -> SyncStatusResponse:
    if row is None:
        return SyncStatusResponse(
            status="idle",
            lastSeq=0,
            startTime=None,
            errorMessage=None,
            seqVersion=0,
        )
    return SyncStatusResponse(
        status=row.status,
        lastSeq=row.last_seq,
        startTime=row.started_at,
        errorMessage=row.error_message,
        seqVersion=row.seq_version,
    )


def _as_utc(value: datetime) -> datetime:
    """Normalise SQLite's naive DateTime test values before comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mark_worker_failed(tenant_id: str, corp_id: str) -> None:
    """Record only a generic error code after a child process failure."""
    try:
        with Session(get_engine()) as db:
            row = _sync_state(db, tenant_id, corp_id)
            if row is None:
                return
            row.status = "error"
            row.error_message = "sync_failed"
            db.commit()
    except Exception:
        # A status-write failure must not make the request/background runner
        # emit database details to the browser or application logs.
        logger.error("archive worker failed and sync status could not be updated")


def _run_archive_worker(tenant_id: str, corp_id: str) -> None:
    """Run the production worker after the 202 response has been sent.

    The worker itself owns the cross-trigger ``fcntl.flock``. Its output is
    inherited so its existing safe operational logs remain available; neither
    child output nor exception text is placed in the browser-visible status.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(_WORKER_SCRIPT)],
            cwd=str(_BACKEND_DIR),
            check=False,
        )
    except Exception:
        _mark_worker_failed(tenant_id, corp_id)
        return

    if result.returncode != 0:
        _mark_worker_failed(tenant_id, corp_id)


@router.get("/sync-status", response_model=SyncStatusResponse)
def get_sync_status(
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
) -> SyncStatusResponse:
    """Return only the authenticated tenant's archive-sync status."""
    _, tenant_id = auth
    config = _active_config(db, tenant_id)
    if config is None:
        return _status_response(None)
    return _status_response(_sync_state(db, tenant_id, config.corp_id))


@router.post(
    "/sync-now",
    response_model=SyncNowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def sync_now(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    auth: Tuple[AdminUser, str] = Depends(get_current_user),
) -> SyncNowResponse:
    """Queue one real archive-worker run for the authenticated tenant.

    The database status gives quick feedback for repeated clicks; the shared
    worker's ``flock`` remains the final concurrency guard across web,
    systemd-timer, and event-triggered invocation paths.
    """
    _, tenant_id = auth
    config = _active_config(db, tenant_id)
    if config is None:
        raise HTTPException(status_code=409, detail="Sync is not configured")

    # The deployed worker has one environment-scoped archive credential. Do
    # not let an authenticated user of a different tenant accidentally cause
    # that worker to archive another tenant's corp data.
    configured_corp_id = os.environ.get("WECOM_CORP_ID", "").strip()
    if configured_corp_id and configured_corp_id != config.corp_id:
        raise HTTPException(status_code=409, detail="Sync worker is unavailable for this tenant")

    now = datetime.now(timezone.utc)
    row = _sync_state(db, tenant_id, config.corp_id)
    if row is not None and row.status == "syncing":
        return SyncNowResponse(accepted=False, message="already_running")

    if row is not None and row.started_at is not None:
        elapsed = now - _as_utc(row.started_at)
        if elapsed < _SYNC_NOW_COOLDOWN:
            remaining = max(1, int((_SYNC_NOW_COOLDOWN - elapsed).total_seconds()))
            return SyncNowResponse(
                accepted=False,
                message="rate_limited",
                retryAfterSeconds=remaining,
            )

    if row is None:
        row = SyncState(tenant_id=tenant_id, corp_id=config.corp_id, last_seq=0)
        db.add(row)
    row.status = "syncing"
    row.started_at = now
    row.error_message = None
    try:
        db.commit()
    except IntegrityError:
        # Two simultaneous first clicks can race before either sees a row.
        # The unique tenant/corp constraint resolves it; report the winner's
        # in-progress state instead of returning an avoidable 500.
        db.rollback()
        return SyncNowResponse(accepted=False, message="already_running")

    background_tasks.add_task(_run_archive_worker, tenant_id, config.corp_id)
    return SyncNowResponse(accepted=True, message="started")
