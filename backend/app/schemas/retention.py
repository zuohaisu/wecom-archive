"""Request and response schemas for tenant retention configuration."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RetentionConfigUpdate(BaseModel):
    retention_days: int = Field(ge=1, le=3650)
    lock: bool


class RetentionConfigOut(BaseModel):
    configured: bool
    retention_days: Optional[int]
    is_locked: bool
