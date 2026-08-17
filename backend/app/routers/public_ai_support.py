"""Public AI support entry point for anonymous pre-sales visitors
(RND-408).

No login, no tenant, no diagnostic tools, no archived data. Every write
is user-initiated; contact info is only collected when the visitor
explicitly fills out and confirms a handoff form.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import AiPublicChatMessage, AiPublicChatSession, AiPublicHandoff
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.services.ai.llm_provider import ai_public_support_is_enabled
from app.services.ai.public_answer_service import answer_public_query
from app.web import render_template

router = APIRouter()

_VISITOR_ID_COOKIE = "visitor_id"
_VISITOR_ID_MAX_AGE_SECONDS = 60 * 60 * 24 * 365  # 1 year
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX_MESSAGES = 20
_rate_limit_state: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()


class _AiPublicSupportStatusOut(BaseModel):
    enabled: bool


class _AiPublicChatSessionOut(BaseModel):
    id: str
    created_at: datetime


class _SendMessageIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class _CitationOut(BaseModel):
    source_id: str
    title: str
    heading_path: str
    doc_path: str
    doc_version: str


class _HandoffPreviewOut(BaseModel):
    summary: str


class _HandoffSubmitIn(BaseModel):
    summary: str = Field(..., min_length=1)
    contact: Optional[str] = Field(None, max_length=255)
    reason: Optional[str] = Field(None, max_length=32)


class _HandoffOut(BaseModel):
    id: str
    status: str


def _ensure_visitor_id(request: Request, response: Response) -> str:
    """Return the visitor_id cookie if present and well-formed; otherwise
    set a new opaque random cookie. The value has no identity information."""
    cookie = request.cookies.get(_VISITOR_ID_COOKIE)
    if cookie and len(cookie) >= 32 and "|" not in cookie:
        return cookie
    visitor_id = secrets.token_urlsafe(32)
    response.set_cookie(
        key=_VISITOR_ID_COOKIE,
        value=visitor_id,
        max_age=_VISITOR_ID_MAX_AGE_SECONDS,
        httponly=True,
        secure=False,  # overridden by reverse-proxy / HSTS in production
        samesite="lax",
    )
    return visitor_id


def _rate_limited(visitor_id: str) -> bool:
    now = time.monotonic()
    with _rate_limit_lock:
        timestamps = [t for t in _rate_limit_state.get(visitor_id, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
        limited = len(timestamps) >= _RATE_LIMIT_MAX_MESSAGES
        if not limited:
            timestamps.append(now)
        _rate_limit_state[visitor_id] = timestamps
        return limited


def _own_public_session(db: Session, session_id: str, visitor_id: str) -> AiPublicChatSession:
    session = db.get(AiPublicChatSession, session_id)
    if session is None or session.visitor_id != visitor_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    return session


def _build_public_redacted_summary(messages: list[AiPublicChatMessage]) -> str:
    lines: list[str] = ["【官网售前 AI 客服转人工摘要 — 以下内容均来自本次匿名访客对话】", ""]
    recent = messages[-12:]
    if not recent:
        lines.append("（本次转人工发起前访客尚未提问）")
        return "\n".join(lines)
    for message in recent:
        if message.role == "user":
            lines.append(f"访客提问：{message.content}")
        elif message.role == "assistant":
            status_label = {
                "answered": "已回答",
                "insufficient_evidence": "证据不足，未能确定回答",
                "error": "AI 服务出错",
                "disabled": "AI 客服未启用",
                "budget_exceeded": "用量已达上限",
            }.get(message.response_status or "", message.response_status or "未知")
            lines.append(f"AI 回答（{status_label}）：{message.content}")
            citations = message.citations or []
            if citations:
                titles = "、".join(c.get("title", "") for c in citations if isinstance(c, dict))
                if titles:
                    lines.append(f"引用文档：{titles}")
        lines.append("")
    return "\n".join(lines).strip()


@router.get("/public/support", response_class=HTMLResponse)
def public_support_page(request: Request):
    """Public pre-sales support page. Sets an anonymous visitor_id cookie
    but never requires login or collects contact info."""
    response = HTMLResponse(
        content=render_template(
            "public_support",
            i18n_script=I18N_SCRIPT_TAG,
        )
    )
    _ensure_visitor_id(request, response)
    return response


@router.get("/api/ai/public/support/status", response_model=_AiPublicSupportStatusOut)
def public_support_status() -> _AiPublicSupportStatusOut:
    return _AiPublicSupportStatusOut(enabled=ai_public_support_is_enabled())


@router.post("/api/ai/public/support/sessions", response_model=_AiPublicChatSessionOut, status_code=201)
def create_public_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> _AiPublicChatSessionOut:
    visitor_id = _ensure_visitor_id(request, response)
    session = AiPublicChatSession(id=str(uuid.uuid4()), visitor_id=visitor_id, status="active")
    db.add(session)
    db.commit()
    return _AiPublicChatSessionOut(id=session.id, created_at=session.created_at)


@router.delete("/api/ai/public/support/sessions/{chat_session_id}", status_code=204, response_model=None)
def delete_public_session(
    chat_session_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> None:
    """Visitor-initiated retention control. Deletes the session and its
    messages; audit logs are retained separately under the public retention
    policy."""
    visitor_id = _ensure_visitor_id(request, response)
    session = _own_public_session(db, chat_session_id, visitor_id)
    db.query(AiPublicChatMessage).filter(AiPublicChatMessage.session_id == session.id).delete()
    db.delete(session)
    db.commit()


@router.post("/api/ai/public/support/sessions/{chat_session_id}/messages")
def send_public_message(
    chat_session_id: str,
    payload: _SendMessageIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    visitor_id = _ensure_visitor_id(request, response)
    session = _own_public_session(db, chat_session_id, visitor_id)

    if _rate_limited(visitor_id):
        return JSONResponse(status_code=429, content={"detail": "rate_limited"})

    result = answer_public_query(
        db,
        visitor_id=visitor_id,
        query=payload.message,
        session_id=session.id,
    )
    session.last_activity_at = datetime.now(timezone.utc)
    db.commit()

    assistant_message = (
        db.query(AiPublicChatMessage)
        .filter(AiPublicChatMessage.session_id == session.id, AiPublicChatMessage.role == "assistant")
        .order_by(AiPublicChatMessage.id.desc())
        .first()
    )

    def _events():
        words = result.text.split(" ")
        for word in words:
            yield f"event: token\ndata: {json.dumps(word + ' ', ensure_ascii=False)}\n\n"
        done_payload = {
            "message_id": assistant_message.id if assistant_message else None,
            "citations": [c.__dict__ for c in result.citations],
            "response_status": result.response_status,
            "escalation_reason": result.escalation_reason,
        }
        yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@router.post("/api/ai/public/support/sessions/{chat_session_id}/handoff/preview", response_model=_HandoffPreviewOut)
def preview_public_handoff(
    chat_session_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> _HandoffPreviewOut:
    visitor_id = _ensure_visitor_id(request, response)
    session = _own_public_session(db, chat_session_id, visitor_id)
    messages = (
        db.query(AiPublicChatMessage)
        .filter(AiPublicChatMessage.session_id == session.id)
        .order_by(AiPublicChatMessage.id.asc())
        .all()
    )
    return _HandoffPreviewOut(summary=_build_public_redacted_summary(messages))


@router.post("/api/ai/public/support/sessions/{chat_session_id}/handoff", response_model=_HandoffOut, status_code=201)
def submit_public_handoff(
    chat_session_id: str,
    payload: _HandoffSubmitIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> _HandoffOut:
    """The summary submitted here is exactly what the visitor previewed and
    was free to edit — never regenerated server-side after the fact."""
    visitor_id = _ensure_visitor_id(request, response)
    session = _own_public_session(db, chat_session_id, visitor_id)
    handoff = AiPublicHandoff(
        id=str(uuid.uuid4()),
        visitor_id=visitor_id,
        session_id=session.id,
        redacted_summary=payload.summary,
        contact=payload.contact,
        status="pending",
        reason=payload.reason or "user_requested",
    )
    db.add(handoff)
    db.commit()
    return _HandoffOut(id=handoff.id, status=handoff.status)
