"""Wire models for platform operator account self-service (RND-415)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PlatformPasswordChangeIn(BaseModel):
    """Password fields are accepted only for the change operation.

    No response model in this module ever includes these fields or a hash.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    current_password: str
    new_password: str
    new_password_confirmation: str
    revoke_other_sessions: bool = True
    reason_code: str
    note: str


class PlatformOperatorInviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    reason_code: str
    note: str


class PlatformOperatorInviteAcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: str
    password: str
    password_confirmation: str


class PlatformOperatorOut(BaseModel):
    id: str
    name: Optional[str]
    email: str
    # The storage enum is ``superadmin``; this presentation label matches the
    # approved v1 UI copy without implying additional roles exist.
    role: str
    status: str
    last_login_at: Optional[datetime]


class PlatformOperatorListOut(BaseModel):
    operators: list[PlatformOperatorOut]


class PlatformOperatorInviteOut(BaseModel):
    operator_id: str
    email: str
    role: str
    status: str
    audit_id: str


class PlatformPasswordChangeOut(BaseModel):
    ok: bool
    revoked_other_sessions: bool
