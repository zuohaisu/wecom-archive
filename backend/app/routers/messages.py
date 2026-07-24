from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_html_session
from app.db.models import ArchiveMessage, ArchiveMessageRecipient
from app.db.session import get_db
from app.html_helpers import _badge, _e, _fmt_msgtime
from app.schemas.messages import MessageDetailOut, MessageOut, RecipientOut
from app.web import render_template

router = APIRouter()

_MAX_LIMIT = 100


@router.get("/admin/messages", response_class=HTMLResponse)
def admin_messages(
    sender: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    db: Session = Depends(get_db),
    tenant_id: Optional[str] = Depends(require_html_session),
):
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)

    query = db.query(ArchiveMessage).filter(ArchiveMessage.tenant_id == tenant_id)
    if sender:
        query = query.filter(ArchiveMessage.sender == sender)
    if q:
        query = query.filter(ArchiveMessage.content_text.ilike(f"%{q}%"))
    rows = query.order_by(ArchiveMessage.msgtime.desc()).limit(limit).all()

    sender_val = _e(sender or "")
    q_val = _e(q or "")

    row_html = ""
    for msg in rows:
        snippet = (msg.content_text or "")[:120]
        if len(msg.content_text or "") > 120:
            snippet += "…"
        row_html += (
            f"<tr>"
            f"<td><a href='/admin/messages/{_e(msg.msgid)}'>{_e(msg.msgid)}</a></td>"
            f"<td>{_e(msg.sender)}</td>"
            f"<td>{_e(msg.roomid)}</td>"
            f"<td>{_e(msg.msgtype)}</td>"
            f"<td>{_fmt_msgtime(msg.msgtime)}</td>"
            f"<td>{_e(snippet)}</td>"
            f"<td>{_badge(msg.decrypt_status)}</td>"
            f"</tr>"
        )

    if not row_html:
        row_html = "<tr><td colspan='7' style='color:#888'>No messages found.</td></tr>"

    body = render_template("messages", sender_val=sender_val, q_val=q_val, row_html=row_html)
    return HTMLResponse(content=body)


@router.get("/admin/messages/{msgid}", response_class=HTMLResponse)
def admin_message_detail(
    msgid: str,
    db: Session = Depends(get_db),
    tenant_id: Optional[str] = Depends(require_html_session),
):
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)

    msg = db.query(ArchiveMessage).filter(
        ArchiveMessage.msgid == msgid,
        ArchiveMessage.tenant_id == tenant_id,
    ).first()
    if msg is None:
        body = render_template("message_detail_404", msgid=_e(msgid))
        return HTMLResponse(content=body, status_code=404)

    recipients = (
        db.query(ArchiveMessageRecipient)
        .filter(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.message_id == msg.id,
        )
        .all()
    )
    recipient_html = "".join(
        f"<li>{_e(r.receiver_userid)} <span class='badge'>{_e(r.receiver_type or '')}</span></li>"
        for r in recipients
    ) or "<li><em>none</em></li>"

    body = render_template(
        "message_detail",
        msgid=_e(msg.msgid),
        seq=_e(msg.seq),
        msgtype=_e(msg.msgtype),
        sender=_e(msg.sender),
        roomid=_e(msg.roomid),
        msgtime=_fmt_msgtime(msg.msgtime),
        decrypt_status_badge=_badge(msg.decrypt_status),
        content_text=_e(msg.content_text),
        recipient_html=recipient_html,
    )
    return HTMLResponse(content=body)


@router.get("/api/messages", response_model=list[MessageOut])
def get_messages(
    sender: Optional[str] = Query(None, description="Exact sender user ID"),
    q: Optional[str] = Query(None, description="Case-insensitive substring match on content_text"),
    msgtype: Optional[str] = Query(None, description="Exact message type (text, image, …)"),
    roomid: Optional[str] = Query(None, description="Exact room ID; omit for 1:1 messages"),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT, description="Max rows to return (1–100)"),
    db: Session = Depends(get_db),
    auth: Tuple = Depends(get_current_user),
):
    _, tenant_id = auth
    query = db.query(ArchiveMessage).filter(ArchiveMessage.tenant_id == tenant_id)
    if sender:
        query = query.filter(ArchiveMessage.sender == sender)
    if q:
        query = query.filter(ArchiveMessage.content_text.ilike(f"%{q}%"))
    if msgtype:
        query = query.filter(ArchiveMessage.msgtype == msgtype)
    if roomid:
        query = query.filter(ArchiveMessage.roomid == roomid)
    return query.order_by(ArchiveMessage.msgtime.desc()).limit(limit).all()


@router.get("/api/messages/{msgid}", response_model=MessageDetailOut)
def get_message(
    msgid: str,
    db: Session = Depends(get_db),
    auth: Tuple = Depends(get_current_user),
):
    _, tenant_id = auth
    msg = db.query(ArchiveMessage).filter(
        ArchiveMessage.msgid == msgid,
        ArchiveMessage.tenant_id == tenant_id,
    ).first()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # message_id is a global PK — parent tenant verification above is not
    # sufficient on its own; a malformed/mistagged recipient row could
    # still carry a different tenant_id, so it must be filtered here too.
    recipients = (
        db.query(ArchiveMessageRecipient)
        .filter(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.message_id == msg.id,
        )
        .all()
    )

    return MessageDetailOut(
        msgid=msg.msgid,
        seq=msg.seq,
        msgtype=msg.msgtype,
        action=None,
        sender=msg.sender,
        roomid=msg.roomid,
        msgtime=msg.msgtime,
        content_text=msg.content_text,
        decrypt_status=msg.decrypt_status,
        recipients=[RecipientOut.model_validate(r) for r in recipients],
    )
