from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class AiSupportStatusOut(BaseModel):
    enabled: bool


class AiChatSessionOut(BaseModel):
    id: str
    created_at: datetime


class SendMessageIn(BaseModel):
    message: str
    include_diagnostics: bool = False
    page_id: Optional[str] = None


class CitationOut(BaseModel):
    source_id: str
    title: str
    heading_path: str
    doc_path: str
    doc_version: str


class ChatAnswerOut(BaseModel):
    message_id: int
    text: str
    citations: list[CitationOut]
    response_status: str
    escalation_reason: Optional[str]


class FeedbackIn(BaseModel):
    helpful: bool
    note: Optional[str] = None


class HandoffPreviewOut(BaseModel):
    summary: str


class HandoffSubmitIn(BaseModel):
    summary: str
    contact: Optional[str] = None


class HandoffOut(BaseModel):
    id: str
    status: str


class UserFeedbackIn(BaseModel):
    feedback_type: Literal["bug", "question", "suggestion", "other"]
    body: str
    contact: Optional[str] = None
    page_id: Optional[str] = None
    include_diagnostics: bool = False
    browser_info: Optional[str] = None


class UserFeedbackOut(BaseModel):
    id: str
    status: str
