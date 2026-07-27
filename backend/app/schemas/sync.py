"""Response contracts for the tenant-scoped archive sync controls."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class SyncStatusResponse(BaseModel):
    status: Literal["idle", "syncing", "error"]
    lastSeq: int
    startTime: Optional[datetime] = None
    errorMessage: Optional[str] = None
    seqVersion: int


class SyncNowResponse(BaseModel):
    accepted: bool
    message: Literal["started", "already_running", "rate_limited"]
    retryAfterSeconds: Optional[int] = None
