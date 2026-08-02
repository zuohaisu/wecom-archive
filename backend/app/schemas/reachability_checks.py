"""Public, aggregate-only contracts for persistent reachability checks."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


ReachabilityCheckState = Literal[
    "healthy", "attention", "checking", "no_data", "incomplete", "error"
]


class ReachabilityCheckStartIn(BaseModel):
    """Deliberately empty: tenant scope only comes from the admin session."""

    model_config = ConfigDict(extra="forbid")


class ReachabilityCheckScopeOut(BaseModel):
    from_at: datetime
    to_at: datetime


class ReachabilityCheckCountsOut(BaseModel):
    matching: int
    checked: int
    reachable: int
    unreachable: int


class ReachabilityCheckSnapshotOut(BaseModel):
    # Public id is the explicitly approved, non-guessable record reference.
    public_run_id: Optional[str] = None
    state: ReachabilityCheckState
    complete: bool
    scope: ReachabilityCheckScopeOut
    counts: ReachabilityCheckCountsOut
    reason_counts: dict[str, int]
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    algorithm_version: str
    safe_error_code: Optional[str] = None
