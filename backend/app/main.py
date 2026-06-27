import html as _html
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient
from app.db.session import get_db
from app.routers.conversations import router as conversations_router

app = FastAPI(title="365 WeCom Archive")
app.include_router(conversations_router)

_MAX_LIMIT = 100


class MessageOut(BaseModel):
    msgid: str
    seq: int
    msgtype: Optional[str]
    sender: Optional[str]
    roomid: Optional[str]
    msgtime: Optional[int]
    content_text: Optional[str]
    decrypt_status: str

    model_config = {"from_attributes": True}


class RecipientOut(BaseModel):
    receiver_userid: str
    receiver_type: Optional[str]

    model_config = {"from_attributes": True}


class MessageDetailOut(BaseModel):
    msgid: str
    seq: int
    msgtype: Optional[str]
    action: Optional[str]
    sender: Optional[str]
    roomid: Optional[str]
    msgtime: Optional[int]
    content_text: Optional[str]
    decrypt_status: str
    decrypted_payload: Optional[dict]
    recipients: list[RecipientOut]

    model_config = {"from_attributes": True}


def _e(value) -> str:
    """HTML-escape any value for safe inline rendering."""
    return _html.escape(str(value)) if value is not None else ""


def _fmt_msgtime(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except Exception:
        return _e(ms)


_PAGE_CSS = """
body{font-family:system-ui,sans-serif;margin:2rem;color:#111}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;vertical-align:top}
th{background:#f4f4f4}
tr:hover td{background:#fafafa}
a{color:#0070f3}
form{margin-bottom:1.5rem;display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
input[type=text]{border:1px solid #ccc;padding:.3rem .5rem;border-radius:3px;width:18rem}
button{padding:.3rem .8rem;border:1px solid #888;border-radius:3px;cursor:pointer}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.3rem .8rem}
dt{font-weight:600}
.badge{font-size:.8em;padding:.1rem .4rem;border-radius:3px;background:#eee}
.badge-success{background:#d1fae5;color:#065f46}
.badge-failed{background:#fee2e2;color:#991b1b}
.badge-pending{background:#fef3c7;color:#92400e}
"""


def _badge(status: str) -> str:
    cls = f"badge badge-{_e(status)}"
    return f'<span class="{cls}">{_e(status)}</span>'


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/messages", response_class=HTMLResponse)
def admin_messages(
    sender: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    db: Session = Depends(get_db),
):
    query = db.query(ArchiveMessage)
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

    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Archive Messages</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>Archive Messages</h1>
<form method="get" action="/admin/messages">
  <label>Sender: <input type="text" name="sender" value="{sender_val}" placeholder="user ID"></label>
  <label>Keyword: <input type="text" name="q" value="{q_val}" placeholder="search content"></label>
  <button type="submit">Filter</button>
  <a href="/admin/messages">Clear</a>
</form>
<table>
  <thead><tr>
    <th>msgid</th><th>sender</th><th>room</th><th>type</th>
    <th>time (UTC)</th><th>content</th><th>status</th>
  </tr></thead>
  <tbody>{row_html}</tbody>
</table>
</body></html>"""
    return HTMLResponse(content=body)


@app.get("/admin/messages/{msgid}", response_class=HTMLResponse)
def admin_message_detail(msgid: str, db: Session = Depends(get_db)):
    msg = db.query(ArchiveMessage).filter(ArchiveMessage.msgid == msgid).first()
    if msg is None:
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>404 Not Found</title>
<style>{_PAGE_CSS}</style>
</head><body>
<h1>404 – Message not found</h1>
<p>No message with msgid <code>{_e(msgid)}</code>.</p>
<p><a href="/admin/messages">← Back to list</a></p>
</body></html>"""
        return HTMLResponse(content=body, status_code=404)

    recipients = (
        db.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id == msg.id)
        .all()
    )
    recipient_html = "".join(
        f"<li>{_e(r.receiver_userid)} <span class='badge'>{_e(r.receiver_type or '')}</span></li>"
        for r in recipients
    ) or "<li><em>none</em></li>"

    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Message {_e(msg.msgid)}</title>
<style>{_PAGE_CSS}</style>
</head><body>
<p><a href="/admin/messages">← Back to list</a></p>
<h1>Message detail</h1>
<dl>
  <dt>msgid</dt><dd><code>{_e(msg.msgid)}</code></dd>
  <dt>seq</dt><dd>{_e(msg.seq)}</dd>
  <dt>msgtype</dt><dd>{_e(msg.msgtype)}</dd>
  <dt>sender</dt><dd>{_e(msg.sender)}</dd>
  <dt>roomid</dt><dd>{_e(msg.roomid)}</dd>
  <dt>msgtime</dt><dd>{_fmt_msgtime(msg.msgtime)}</dd>
  <dt>decrypt_status</dt><dd>{_badge(msg.decrypt_status)}</dd>
  <dt>content_text</dt><dd><pre style="white-space:pre-wrap">{_e(msg.content_text)}</pre></dd>
</dl>
<h2>Recipients</h2>
<ul>{recipient_html}</ul>
</body></html>"""
    return HTMLResponse(content=body)


@app.get("/api/messages", response_model=list[MessageOut])
def get_messages(
    sender: Optional[str] = Query(None, description="Exact sender user ID"),
    q: Optional[str] = Query(None, description="Case-insensitive substring match on content_text"),
    msgtype: Optional[str] = Query(None, description="Exact message type (text, image, …)"),
    roomid: Optional[str] = Query(None, description="Exact room ID; omit for 1:1 messages"),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT, description="Max rows to return (1–100)"),
    db: Session = Depends(get_db),
):
    query = db.query(ArchiveMessage)
    if sender:
        query = query.filter(ArchiveMessage.sender == sender)
    if q:
        query = query.filter(ArchiveMessage.content_text.ilike(f"%{q}%"))
    if msgtype:
        query = query.filter(ArchiveMessage.msgtype == msgtype)
    if roomid:
        query = query.filter(ArchiveMessage.roomid == roomid)
    return query.order_by(ArchiveMessage.msgtime.desc()).limit(limit).all()


@app.get("/api/messages/{msgid}", response_model=MessageDetailOut)
def get_message(msgid: str, db: Session = Depends(get_db)):
    msg = db.query(ArchiveMessage).filter(ArchiveMessage.msgid == msgid).first()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")

    recipients = (
        db.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id == msg.id)
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
        decrypted_payload=msg.decrypted_payload,
        recipients=[RecipientOut.model_validate(r) for r in recipients],
    )
