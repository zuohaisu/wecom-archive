"""Owner-facing data portability API for RND-360 and RND-393."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_export_audit
from app.auth import require_role
from app.db.models import AdminUser, ExportJob, MediaFile
from app.db.session import get_db
from app.export_approval import ExportNotApprovedError, require_export_approval
from app.media_storage import (
    MediaObjectNotFound,
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    get_media_storage_provider_for_backend,
)
from app.schemas.export import ExportFormat, ExportSelection
from app.settings import get_email_settings
from app.services.export_jobs import (
    create_media_export_job,
    export_job_view,
    export_storage_reference_is_valid,
)
from app.services.export_quota import (
    ExportQuotaBucket,
    ExportQuotaExceeded,
    consume_export_quota,
    get_export_quota_summary,
)
from app.services.export_service import ExportTooLargeError, generate_export


router = APIRouter(prefix="/api/admin/exports", tags=["exports"])
MEDIA_EXPORT_PARAMS = {"kind": "media_zip", "scope": {"tenant_wide": True}}


class TextSelectionIn(BaseModel):
    roomid: Optional[str] = Field(default=None, max_length=255)
    participant_id: Optional[str] = Field(default=None, max_length=255)
    start_ms: Optional[int] = Field(default=None, ge=0)
    end_ms: Optional[int] = Field(default=None, ge=0)
    message_ids: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_bounds(self):
        normalized_ids = []
        for value in self.message_ids:
            candidate = value.strip()
            if not candidate or len(candidate) > 64:
                raise ValueError("message_ids must contain non-empty identifiers up to 64 characters")
            normalized_ids.append(candidate)
        self.message_ids = normalized_ids
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("start_ms and end_ms must be supplied together")
        if (
            self.start_ms is not None
            and self.end_ms is not None
            and self.start_ms > self.end_ms
        ):
            raise ValueError("start_ms must not be after end_ms")
        if not (
            self.roomid
            or self.participant_id
            or self.message_ids
            or self.start_ms is not None
        ):
            raise ValueError("at least one export filter is required")
        return self

    def domain(self) -> ExportSelection:
        return ExportSelection(
            roomid=(self.roomid or "").strip() or None,
            participant_id=(self.participant_id or "").strip() or None,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            message_ids=tuple(sorted(set(self.message_ids))),
        )

    def canonical(self) -> dict:
        selection = self.domain()
        value: dict[str, object] = {}
        if selection.roomid:
            value["roomid"] = selection.roomid
        if selection.participant_id:
            value["participant_id"] = selection.participant_id
        if selection.start_ms is not None:
            value["start_ms"] = selection.start_ms
            value["end_ms"] = selection.end_ms
        if selection.message_ids:
            value["message_ids"] = list(selection.message_ids)
        return value


class TextExportRequest(BaseModel):
    approval_token: str = Field(min_length=20, max_length=512)
    format: Literal["pdf", "excel"]
    selection: TextSelectionIn


class MediaExportRequest(BaseModel):
    approval_token: str = Field(min_length=20, max_length=512)


def text_export_params(req: TextExportRequest) -> dict:
    return {
        "kind": "text",
        "format": req.format,
        "selection": req.selection.canonical(),
    }


def _bucket_json(bucket: ExportQuotaBucket) -> dict:
    return {
        "type": bucket.export_type,
        "limit": bucket.limit,
        "used": bucket.used,
        "remaining": bucket.remaining,
    }


def _quota_http_error(error: ExportQuotaExceeded) -> HTTPException:
    bucket = error.bucket
    return HTTPException(
        status_code=429,
        detail={
            "code": "export_quota_exceeded",
            "export_type": bucket.export_type,
            "limit": bucket.limit,
            "used": bucket.used,
            "remaining": bucket.remaining,
            "resets_at": bucket.resets_at.isoformat(),
        },
    )


def _approval_ref(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _owner_email(user: AdminUser) -> str | None:
    candidate = (user.email or "").strip()
    return candidate if "@" in candidate and not candidate.startswith("@") else None


def _masked_email(value: str | None) -> str | None:
    if not value:
        return None
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _notification_delivery_ready(user: AdminUser) -> bool:
    settings = get_email_settings()
    return bool(_owner_email(user) and settings.smtp_host and settings.smtp_from)


@router.get("/quota")
def get_export_quota(
    auth: tuple[AdminUser, str] = Depends(require_role("owner")),
    db: Session = Depends(get_db),
):
    user, tenant_id = auth
    summary = get_export_quota_summary(db, tenant_id)
    return {
        "period_start": summary.period_start,
        "resets_at": summary.resets_at,
        "text": _bucket_json(summary.text),
        "media_zip": _bucket_json(summary.media_zip),
        "notification_email_configured": _notification_delivery_ready(user),
        "notification_email_hint": _masked_email(_owner_email(user)),
    }


@router.post("/text")
def export_text(
    req: TextExportRequest,
    auth: tuple[AdminUser, str] = Depends(require_role("owner")),
    db: Session = Depends(get_db),
):
    user, tenant_id = auth
    params = text_export_params(req)
    try:
        require_export_approval(
            db=db,
            token=req.approval_token,
            params=params,
            admin_user_id=user.id,
            tenant_id=tenant_id,
        )
        consume_export_quota(db, tenant_id, "text")
        result = generate_export(
            db,
            tenant_id,
            req.selection.domain(),
            ExportFormat(req.format),
        )
        audit_recorded = record_export_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=user.id,
            export_format=req.format,
            record_count=result.record_count,
            scope=req.selection.canonical(),
            approval_ref=_approval_ref(req.approval_token),
            gate_enforced=True,
        )
        if not audit_recorded:
            raise RuntimeError("export audit unavailable")
        db.commit()
    except ExportNotApprovedError as error:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(error))
    except ExportQuotaExceeded as error:
        db.rollback()
        raise _quota_http_error(error)
    except ExportTooLargeError as error:
        db.rollback()
        raise HTTPException(status_code=413, detail=str(error))
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(error))
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="export_generation_failed")

    return Response(
        content=result.content,
        media_type=result.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/media", status_code=202)
def request_media_export(
    req: MediaExportRequest,
    auth: tuple[AdminUser, str] = Depends(require_role("owner")),
    db: Session = Depends(get_db),
):
    user, tenant_id = auth
    if not _notification_delivery_ready(user):
        raise HTTPException(status_code=409, detail="export_notification_unavailable")
    try:
        require_export_approval(
            db=db,
            token=req.approval_token,
            params=MEDIA_EXPORT_PARAMS,
            admin_user_id=user.id,
            tenant_id=tenant_id,
        )
        consume_export_quota(db, tenant_id, "media_zip")
        job = create_media_export_job(
            db,
            tenant_id=tenant_id,
            requested_by=user.id,
        )
        media_count = int(
            db.scalar(
                select(func.count(MediaFile.id)).where(
                    MediaFile.tenant_id == tenant_id,
                    MediaFile.download_status == "downloaded",
                )
            )
            or 0
        )
        audit_recorded = record_export_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=user.id,
            export_format="zip",
            record_count=media_count,
            scope=MEDIA_EXPORT_PARAMS["scope"],
            approval_ref=_approval_ref(req.approval_token),
            gate_enforced=True,
        )
        if not audit_recorded:
            raise RuntimeError("export audit unavailable")
        db.commit()
        db.refresh(job)
    except ExportNotApprovedError as error:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(error))
    except ExportQuotaExceeded as error:
        db.rollback()
        raise _quota_http_error(error)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="export_request_failed")
    return export_job_view(job)


@router.get("/jobs")
def list_export_jobs(
    auth: tuple[AdminUser, str] = Depends(require_role("owner")),
    db: Session = Depends(get_db),
):
    _user, tenant_id = auth
    jobs = db.scalars(
        select(ExportJob)
        .where(ExportJob.tenant_id == tenant_id)
        .order_by(ExportJob.requested_at.desc())
        .limit(20)
    ).all()
    return {"items": [export_job_view(job) for job in jobs]}


@router.get("/jobs/{job_id}")
def get_export_job(
    job_id: str,
    auth: tuple[AdminUser, str] = Depends(require_role("owner")),
    db: Session = Depends(get_db),
):
    _user, tenant_id = auth
    job = db.scalar(
        select(ExportJob).where(
            ExportJob.id == job_id,
            ExportJob.tenant_id == tenant_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="export_not_found")
    return export_job_view(job)


@router.get("/jobs/{job_id}/download")
def download_export_job(
    job_id: str,
    auth: tuple[AdminUser, str] = Depends(require_role("owner")),
    db: Session = Depends(get_db),
):
    _user, tenant_id = auth
    job = db.scalar(
        select(ExportJob).where(
            ExportJob.id == job_id,
            ExportJob.tenant_id == tenant_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="export_not_found")
    view = export_job_view(job)
    if view["status"] == "expired":
        raise HTTPException(status_code=410, detail="export_expired")
    if job.status != "ready":
        raise HTTPException(status_code=409, detail="export_not_ready")
    if not job.storage_backend or not job.storage_ref:
        raise HTTPException(status_code=410, detail="export_file_missing")
    if not export_storage_reference_is_valid(
        tenant_id=tenant_id,
        job_id=job.id,
        storage_backend=job.storage_backend,
        storage_ref=job.storage_ref,
    ):
        raise HTTPException(status_code=410, detail="export_file_missing")

    try:
        provider = get_media_storage_provider_for_backend(job.storage_backend)
        if not provider.exists(job.storage_ref):
            raise HTTPException(status_code=410, detail="export_file_missing")
        filename = f"wecom-media-export-{job.requested_at.date().isoformat()}.zip"
        if provider.supports_local_path():
            path = provider.get_local_path(job.storage_ref)
            if path is None:
                raise HTTPException(status_code=410, detail="export_file_missing")
            return FileResponse(
                path,
                media_type="application/zip",
                filename=filename,
                headers={"Cache-Control": "private, no-store"},
            )
        expires_at = job.expires_at
        if expires_at is None:
            raise HTTPException(status_code=410, detail="export_expired")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining = max(
            1,
            min(900, int((expires_at - datetime.now(timezone.utc)).total_seconds())),
        )
        url = provider.get_download_url(job.storage_ref, expires_in=remaining)
        if not url:
            raise HTTPException(status_code=503, detail="export_download_unavailable")
        return RedirectResponse(
            url,
            status_code=307,
            headers={"Cache-Control": "private, no-store"},
        )
    except HTTPException:
        raise
    except MediaObjectNotFound:
        raise HTTPException(status_code=410, detail="export_file_missing")
    except MediaStorageConfigurationError:
        raise HTTPException(status_code=500, detail="export_storage_misconfigured")
    except (MediaStorageUnavailable, MediaStorageOperationError):
        raise HTTPException(status_code=503, detail="export_storage_unavailable")
