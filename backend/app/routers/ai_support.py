"""AI support entry point: page route + chat/feedback/handoff APIs
(RND-357 / T3). Every write here is user-initiated and consent-gated —
diagnostics are only ever read when the caller sets include_diagnostics on
the very message that triggers them, never proactively.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_html_session, require_platform_admin
from app.db.models import AiChatMessage, AiChatSession, AiFeedback, AiHandoff
from app.db.session import get_db
from app.i18n_assets import I18N_SCRIPT_TAG
from app.schemas.ai_support import (
    AiChatSessionOut,
    AiSupportStatusOut,
    FeedbackIn,
    HandoffOut,
    HandoffPreviewOut,
    HandoffResolveIn,
    HandoffResolveOut,
    HandoffSubmitIn,
    SendMessageIn,
    UserFeedbackIn,
    UserFeedbackOut,
)
from app.services.ai.answer_service import answer_query
from app.services.ai.feedback_notify import notify_new_feedback
from app.services.ai.handoff import build_redacted_summary
from app.services.ai.llm_provider import ai_support_is_enabled
from app.services import product_analytics
from app.schemas.product_analytics import FEEDBACK_SUBMITTED
from app.services.ai_tools import handlers as ai_tool_handlers  # noqa: F401 - populates the tool registry
from app.services.ai_tools.registry import ToolContext, ToolResultStatus, ToolScope, invoke_tool
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()

# Minimal in-process rate limit (mirrors app.auth's documented "in-memory,
# single-process, sufficient for single-instance MVP" pattern) — a tenant
# sending more than this many messages in the window gets a clear 429
# instead of hammering the LLM provider.
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX_MESSAGES = 20
_rate_limit_state: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()

_DIAGNOSTIC_TOOL_NAMES = (
    "product_version",
    "current_page",
    "tenant_service_status",
    "sync_status_summary",
    "storage_quota_summary",
)


def _rate_limited(tenant_id: str) -> bool:
    now = time.monotonic()
    with _rate_limit_lock:
        timestamps = [t for t in _rate_limit_state.get(tenant_id, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
        limited = len(timestamps) >= _RATE_LIMIT_MAX_MESSAGES
        if not limited:
            timestamps.append(now)
        _rate_limit_state[tenant_id] = timestamps
        return limited


def _collect_diagnostic_context(db: Session, tenant_id: str, admin_user_id: str, page_id: Optional[str]) -> dict:
    context = ToolContext(
        tenant_id=tenant_id, admin_user_id=admin_user_id, scope=ToolScope.TENANT_ADMIN, page_context={"page_id": page_id}
    )
    collected: dict = {}
    for name in _DIAGNOSTIC_TOOL_NAMES:
        result = invoke_tool(db, name, context, consent_given=True)
        if result.status == ToolResultStatus.SUCCESS:
            collected[name] = result.data
    return collected


def _allowlisted_page_id(db: Session, tenant_id: str, admin_user_id: str, page_id: Optional[str]) -> Optional[str]:
    """Always run client-supplied page_id through RND-358's current_page
    tool — it is routing metadata, not sensitive diagnostic content, so
    this is cheap allowlist validation rather than a consent-gated read;
    never store an arbitrary client-supplied string verbatim."""
    context = ToolContext(
        tenant_id=tenant_id, admin_user_id=admin_user_id, scope=ToolScope.TENANT_ADMIN, page_context={"page_id": page_id}
    )
    result = invoke_tool(db, "current_page", context, consent_given=True)
    return result.data.get("page_id") if result.status == ToolResultStatus.SUCCESS else None


def _diagnostic_product_version(db: Session, tenant_id: str, admin_user_id: str) -> Optional[str]:
    context = ToolContext(tenant_id=tenant_id, admin_user_id=admin_user_id, scope=ToolScope.TENANT_ADMIN, page_context={})
    result = invoke_tool(db, "product_version", context, consent_given=True)
    return result.data.get("version") if result.status == ToolResultStatus.SUCCESS else None


def _own_session(db: Session, chat_session_id: str, tenant_id: str) -> AiChatSession:
    session = db.get(AiChatSession, chat_session_id)
    if session is None or session.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    return session


@router.get("/admin/support", response_class=HTMLResponse)
def support_page(request: Request, tenant_id: Optional[str] = Depends(require_html_session)):
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "support",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav(
                "support",
                {route.path for route in request.app.routes if hasattr(route, "path")},
            ),
        )
    )


@router.get("/api/ai/support/status", response_model=AiSupportStatusOut)
def support_status(auth: tuple = Depends(get_current_user)) -> AiSupportStatusOut:
    return AiSupportStatusOut(enabled=ai_support_is_enabled())


@router.post("/api/ai/support/sessions", response_model=AiChatSessionOut, status_code=201)
def create_session(
    auth: tuple = Depends(get_current_user), db: Session = Depends(get_db)
) -> AiChatSessionOut:
    user, tenant_id = auth
    session = AiChatSession(id=str(uuid.uuid4()), tenant_id=tenant_id, admin_user_id=user.id, status="active")
    db.add(session)
    db.commit()
    return AiChatSessionOut(id=session.id, created_at=session.created_at)


@router.delete("/api/ai/support/sessions/{chat_session_id}", status_code=204, response_model=None)
def delete_session(
    chat_session_id: str, auth: tuple = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """User-initiated retention control (RND-357 AC: "对话历史的...删除入口
    ...必须明确"). Deletes the session's own messages; the audit trail in
    ai_query_audit_logs is retained separately under RND-359's retention
    policy, matching how other audit logs in this codebase outlive the
    record they audit."""
    user, tenant_id = auth
    session = _own_session(db, chat_session_id, tenant_id)
    db.query(AiChatMessage).filter(AiChatMessage.session_id == session.id).delete()
    db.delete(session)
    db.commit()


@router.post("/api/ai/support/sessions/{chat_session_id}/messages")
def send_message(
    chat_session_id: str,
    payload: SendMessageIn,
    auth: tuple = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user, tenant_id = auth
    session = _own_session(db, chat_session_id, tenant_id)

    if _rate_limited(tenant_id):
        return JSONResponse(status_code=429, content={"detail": "rate_limited"})

    diagnostic_context = None
    if payload.include_diagnostics:
        diagnostic_context = _collect_diagnostic_context(db, tenant_id, user.id, payload.page_id)

    result = answer_query(
        db,
        tenant_id=tenant_id,
        admin_user_id=user.id,
        query=payload.message,
        access_levels=["customer"],
        locale="zh-CN",
        session_id=session.id,
        diagnostic_context=diagnostic_context,
    )
    session.last_activity_at = datetime.now(timezone.utc)
    db.commit()

    assistant_message = (
        db.query(AiChatMessage)
        .filter(AiChatMessage.session_id == session.id, AiChatMessage.role == "assistant")
        .order_by(AiChatMessage.id.desc())
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


@router.post("/api/ai/support/messages/{message_id}/feedback")
def submit_feedback(
    message_id: int,
    payload: FeedbackIn,
    auth: tuple = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _user, tenant_id = auth
    message = db.get(AiChatMessage, message_id)
    if message is None or message.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="message_not_found")
    message.helpful = payload.helpful
    message.feedback_note = payload.note
    db.commit()
    return {"ok": True}


@router.post("/api/ai/support/sessions/{chat_session_id}/handoff/preview", response_model=HandoffPreviewOut)
def preview_handoff(
    chat_session_id: str, auth: tuple = Depends(get_current_user), db: Session = Depends(get_db)
) -> HandoffPreviewOut:
    _user, tenant_id = auth
    session = _own_session(db, chat_session_id, tenant_id)
    messages = (
        db.query(AiChatMessage)
        .filter(AiChatMessage.session_id == session.id)
        .order_by(AiChatMessage.id.asc())
        .all()
    )
    return HandoffPreviewOut(summary=build_redacted_summary(messages))


@router.post("/api/ai/support/sessions/{chat_session_id}/handoff", response_model=HandoffOut, status_code=201)
def submit_handoff(
    chat_session_id: str,
    payload: HandoffSubmitIn,
    auth: tuple = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HandoffOut:
    """The summary submitted here is exactly what the user previewed and
    was free to edit — never regenerated server-side after the fact."""
    user, tenant_id = auth
    session = _own_session(db, chat_session_id, tenant_id)
    handoff = AiHandoff(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        session_id=session.id,
        admin_user_id=user.id,
        redacted_summary=payload.summary,
        contact=payload.contact,
        status="pending",
        reason=payload.reason or "user_requested",
    )
    db.add(handoff)
    db.commit()
    return HandoffOut(id=handoff.id, status=handoff.status)


@router.post("/api/platform/ai/handoffs/{handoff_id}/resolve", response_model=HandoffResolveOut)
def resolve_handoff(
    handoff_id: str,
    payload: HandoffResolveIn,
    _platform_admin=Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> HandoffResolveOut:
    """RND-359 (T5): internal-staff-only triage action — resolution_category
    is exactly the taxonomy the gap-report aggregation groups by. Never
    reachable by a tenant AdminUser session; this is cross-tenant by design
    (an internal reviewer triages handoffs from any tenant)."""
    handoff = db.get(AiHandoff, handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail="handoff_not_found")
    handoff.status = "resolved"
    handoff.resolution_category = payload.resolution_category
    handoff.resolved_by = payload.resolved_by
    handoff.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return HandoffResolveOut(
        id=handoff.id,
        status=handoff.status,
        resolution_category=handoff.resolution_category,
        resolved_at=handoff.resolved_at,
    )


@router.post("/api/ai/support/feedback", response_model=UserFeedbackOut, status_code=201)
def submit_user_feedback(
    payload: UserFeedbackIn,
    auth: tuple = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserFeedbackOut:
    """RND-161 (T6): proactive feedback, independent of any AI conversation
    — this endpoint never requires an existing chat session."""
    user, tenant_id = auth

    page_id = _allowlisted_page_id(db, tenant_id, user.id, payload.page_id)
    product_version = _diagnostic_product_version(db, tenant_id, user.id) if payload.include_diagnostics else None

    feedback = AiFeedback(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        admin_user_id=user.id,
        feedback_type=payload.feedback_type,
        body=payload.body,
        contact=payload.contact,
        product_version=product_version,
        page_id=page_id,
        browser_info=payload.browser_info,
        status="new",
    )
    db.add(feedback)
    db.commit()
    product_analytics.record_backend_event_best_effort(
        db,
        event_name=FEEDBACK_SUBMITTED,
        tenant_id=tenant_id,
        admin_user_id=user.id,
    )
    notify_new_feedback(feedback_id=feedback.id, tenant_id=tenant_id, feedback_type=feedback.feedback_type)
    return UserFeedbackOut(id=feedback.id, status=feedback.status)
