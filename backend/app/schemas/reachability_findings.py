"""Minimal, agent-safe reachability finding read contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

FindingStatus = Literal["active", "resolved"]
# These are classifier-owned, static reason codes. They are repeated here only
# to validate the public filter; automation never branches on them.
ALLOWED_FINDING_REASONS = frozenset({
    "sender_null_or_unresolvable",
    "roomid_missing_for_multi_recipient_fanout",
    "direct_message_has_no_recipient_rows",
    "conversation_membership_lookup_did_not_return_message",
    "conversation_membership_lookup_errored",
    "not_decrypt_success",
})


class ReachabilityFindingOut(BaseModel):
    public_finding_id: str
    reason: str
    status: FindingStatus
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: Optional[datetime] = None
    occurrence_count: int
    algorithm_version: str
    remediation_code: Literal["review_reachability"] = "review_reachability"


class ReachabilityFindingSummaryOut(BaseModel):
    total: int
    active: int
    resolved: int


class ReachabilityFindingListOut(BaseModel):
    items: list[ReachabilityFindingOut]
    next_cursor: Optional[str] = None
    summary: ReachabilityFindingSummaryOut
