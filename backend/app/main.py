import html as _html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, get_current_user
from app.db.models import AdminSession, ArchiveMessage, ArchiveMessageRecipient
from app.db.schema_check import full_readiness_check
from app.db.session import get_db, get_engine
from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG
from app.message_type_registry import build_frontend_registry_entries
from app.routers.auth import router as auth_router
from app.routers.conversations import MediaAccessNoStoreMiddleware
from app.routers.conversations import router as conversations_router
from app.routers.reachability_audit import router as reachability_audit_router
from app.routers.search import router as search_router
from app.routers.wecom_events import router as wecom_events_router

logger = logging.getLogger(__name__)


class _RedactOAuthCallbackQueryFilter(logging.Filter):
    """
    RND-225: uvicorn's default access logger (enabled unless the process is
    started with --no-access-log — see docs/DEPLOYMENT.md, which does not
    pass it) records the full request line for every request, including the
    query string. The WeCom OAuth callback's `code` (a short-lived but
    directly replayable authorization code) and `state` (CSRF token) would
    otherwise be written verbatim to that log on every login. This does not
    cover a reverse proxy's own access log, if one is placed in front of
    this app — that needs equivalent redaction configured separately.
    """

    _REDACT_PREFIXES = ("/api/auth/wecom/callback?",)

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2]
            if path.startswith(self._REDACT_PREFIXES):
                redacted = path.split("?", 1)[0] + "?[REDACTED]"
                record.args = (args[0], args[1], redacted, args[3], args[4])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactOAuthCallbackQueryFilter())

app = FastAPI(title="365 WeCom Archive")
# RND-187: guarantees Cache-Control: no-store on every response (success or
# error, any status code) for the media access descriptor endpoint — see
# MediaAccessNoStoreMiddleware's docstring for why this must be a
# response-side middleware rather than a header set inside the route.
app.add_middleware(MediaAccessNoStoreMiddleware)
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(reachability_audit_router)
app.include_router(search_router)
app.include_router(wecom_events_router)

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
    recipients: list[RecipientOut]

    model_config = {"from_attributes": True}


def _e(value) -> str:
    """HTML-escape any value for safe inline rendering."""
    return _html.escape(str(value)) if value is not None else ""


# Beijing time has used a fixed UTC+8 offset (no DST) since 1991; a fixed-offset
# timezone avoids depending on system tzdata being installed at deploy time.
_BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _fmt_msgtime(ms: Optional[int]) -> str:
    """Format an epoch-ms timestamp as Beijing time (UTC+8) for admin display only.

    Stored/raw msgtime values are untouched; this is purely for rendering.
    """
    if ms is None:
        return ""
    try:
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .astimezone(_BEIJING_TZ)
            .strftime("%Y-%m-%d %H:%M:%S")
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


def _resolve_session_tenant_id(request: Request, db: Session) -> Optional[str]:
    """
    Return tenant_id for the authenticated session, or None if unauthenticated.
    Used by HTML routes that redirect to /admin/login instead of returning 401.
    tenant_id is the authoritative scope for all subsequent archive queries.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    now = datetime.now(timezone.utc)
    try:
        session = (
            db.query(AdminSession)
            .filter(
                AdminSession.id == session_id,
                AdminSession.expires_at > now,
                AdminSession.is_revoked.is_(False),
            )
            .first()
        )
    except Exception as exc:
        # A DB failure means identity cannot be confirmed — treat as
        # unauthenticated (redirect to login), never as authenticated.
        # Log only the exception type — DBAPI errors often embed bound
        # parameters (here, the session token) in their string repr.
        logger.error("_resolve_session_tenant_id: session lookup failed: %s", type(exc).__name__)
        return None
    return session.tenant_id if session is not None else None


@app.get("/health/live")
def health_live():
    """Liveness only: the process can respond to HTTP at all. Does not
    touch the database — a DB outage must not make this fail, or
    orchestration tooling would kill/restart a process that isn't the
    actual problem. Use /health/ready (or /health, see below) to gate
    deploys and load-balancer readiness."""
    return {"status": "ok"}


def _readiness_body(response: Response) -> dict:
    """RND-227: real readiness, not a static ok. A DB outage or a schema
    that hasn't been migrated to the repository's head must never report
    HTTP 200 — that was the exact gap that let deploys restart the
    service ahead of `alembic upgrade head` and serve UndefinedColumn
    500s. Never exposes exception text, connection strings, or revision
    ids in the response body (kept generic on purpose for an
    unauthenticated endpoint) — see app.db.schema_check.full_readiness_check
    for the detail that stays server-side only."""
    try:
        ok, _detail = full_readiness_check(get_engine())
    except Exception:
        # get_engine() itself can raise (e.g. DATABASE_URL unset) before
        # full_readiness_check's own try/except ever starts — caught
        # here too so this endpoint can never surface a 500/stack trace,
        # only the documented 200/503 contract.
        ok = False
    if not ok:
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(response: Response):
    return _readiness_body(response)


@app.get("/health")
def health(response: Response):
    """Kept as an alias for /health/ready (not liveness) for backward
    compatibility: every existing caller (scripts/deploy_server.sh's
    public check, external uptime monitoring) already treats this path
    as "is the service actually usable", and downgrading it to a
    liveness-only check would silently reintroduce the schema-drift gap
    this endpoint exists to close."""
    return _readiness_body(response)


@app.get("/admin/messages", response_class=HTMLResponse)
def admin_messages(
    request: Request,
    sender: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_session_tenant_id(request, db)
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
    <th>time (UTC+8)</th><th>content</th><th>status</th>
  </tr></thead>
  <tbody>{row_html}</tbody>
</table>
</body></html>"""
    return HTMLResponse(content=body)


@app.get("/admin/messages/{msgid}", response_class=HTMLResponse)
def admin_message_detail(
    msgid: str,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_session_tenant_id(request, db)
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)

    msg = db.query(ArchiveMessage).filter(
        ArchiveMessage.msgid == msgid,
        ArchiveMessage.tenant_id == tenant_id,
    ).first()
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
  <dt>msgtime (UTC+8)</dt><dd>{_fmt_msgtime(msg.msgtime)}</dd>
  <dt>decrypt_status</dt><dd>{_badge(msg.decrypt_status)}</dd>
  <dt>content_text</dt><dd><pre style="white-space:pre-wrap">{_e(msg.content_text)}</pre></dd>
</dl>
<h2>Recipients</h2>
<ul>{recipient_html}</ul>
</body></html>"""
    return HTMLResponse(content=body)


# RND-196 — the embedded MessageTypeRegistry's `entries` object (below,
# inside _REVIEW_CONSOLE_HTML) is generated from app.message_type_registry
# instead of hand-duplicated: the backend Registry is now the runtime
# authority and the frontend consumes its exported metadata. Restricted to
# placeholder.<type> i18n keys that actually exist in app/assets/i18n.js
# so this can never emit a placeholderKey with no translation behind it —
# see build_frontend_registry_entries()'s docstring.
_KNOWN_PLACEHOLDER_I18N_KEYS = frozenset(
    re.findall(r'"(placeholder\.[a-zA-Z0-9_]+)"', I18N_JS_SOURCE)
)
_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON = json.dumps(
    build_frontend_registry_entries(known_placeholder_keys=_KNOWN_PLACEHOLDER_I18N_KEYS)
)

_REVIEW_CONSOLE_HTML = """\
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Conversation Review Console</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bubble-self-bg:#cfe6fd;--bubble-self-text:#000;--bubble-other-bg:#fff;--bubble-other-text:#000;--link-color:#417ce8}
body{font-family:system-ui,sans-serif;color:#222;background:#f0f2f5;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.top-bar{height:44px;background:#001529;color:#fff;display:flex;align-items:center;padding:0 1rem;gap:1rem;flex-shrink:0}
.top-bar h1{font-size:.95rem;font-weight:600;letter-spacing:.01em}
.top-bar a{color:#8ca0b3;font-size:.82rem;text-decoration:none}
.top-bar a:hover{color:#fff}
.top-bar-user{margin-left:auto;font-size:.8rem;color:#8ca0b3;display:flex;align-items:center;gap:.75rem}
.tz-note{font-size:.72rem;color:#5c7185}
.btn-logout{background:transparent;border:1px solid #3a4a5a;color:#8ca0b3;padding:.2rem .65rem;border-radius:3px;cursor:pointer;font-size:.78rem}
.btn-logout:hover{border-color:#8ca0b3;color:#fff}
.refresh-bar{display:flex;align-items:center;gap:.5rem;font-size:.76rem;color:#8ca0b3;flex-shrink:0}
.refresh-status{white-space:nowrap}
.refresh-status .refresh-status-error{color:#ff7875}
.btn-refresh{background:transparent;border:1px solid #3a4a5a;color:#8ca0b3;padding:.2rem .65rem;border-radius:3px;cursor:pointer;font-size:.78rem}
.btn-refresh:hover{border-color:#8ca0b3;color:#fff}
.layout{display:flex;flex:1;overflow:hidden}
.col{display:flex;flex-direction:column;overflow:hidden;background:#fff;border-right:1px solid #e8e8e8}
.col-left{width:220px;flex-shrink:0}
.col-mid{width:304px;flex-shrink:0}
.col-right{flex:1;border-right:none;position:relative}
.col-header{padding:.4rem .75rem;font-size:.74rem;font-weight:600;color:#666;background:#fafafa;border-bottom:1px solid #ebebeb;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mode-tabs{display:flex;flex-shrink:0;border-bottom:1px solid #e8e8e8}
.mode-tab{flex:1;padding:.45rem 0;font-size:.83rem;text-align:center;cursor:pointer;border:none;background:transparent;color:#666;border-bottom:2px solid transparent}
.mode-tab.active{color:#1890ff;border-bottom-color:#1890ff;font-weight:600}
.mode-tab:hover:not(.active){background:#f5f5f5}
.col-body{flex:1;overflow-y:auto}
.entity-item{display:flex;align-items:center;gap:.45rem;padding:.45rem .75rem;cursor:pointer;border-bottom:1px solid #f5f5f5;font-size:.83rem}
.entity-item:hover{background:#f0f5ff}
.entity-item.active{background:#e6f4ff;color:#0958d9}
.entity-avatar{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;color:#fff;flex-shrink:0}
.entity-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.entity-raw{color:#bbb;font-weight:400}
.seat-badge{font-size:.62rem;padding:.05rem .35rem;border-radius:2px;margin-left:.3rem;font-weight:600;flex-shrink:0}
.seat-badge-active{background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f}
.seat-badge-history{background:#f5f5f5;color:#999;border:1px solid #e8e8e8}
.conv-card{padding:.55rem .75rem;border-bottom:1px solid #f0f0f0;cursor:pointer}
.conv-card:hover{background:#f5f8ff}
.conv-card.active{background:#e6f4ff;border-left:3px solid #1890ff}
.conv-top{display:flex;align-items:baseline;gap:.3rem;margin-bottom:.15rem}
.conv-name{font-weight:600;font-size:.83rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv-time{font-size:.7rem;color:#bbb;white-space:nowrap;flex-shrink:0}
.conv-secondary{font-size:.7rem;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:.15rem}
.conv-snippet{font-size:.78rem;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:.2rem}
.conv-meta{display:flex;align-items:center;gap:.3rem;flex-wrap:wrap}
.badge{display:inline-block;font-size:.7rem;padding:.05rem .3rem;border-radius:2px;font-weight:500;line-height:1.4}
.badge-direct{background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f}
.badge-group{background:#e6f7ff;color:#0958d9;border:1px solid #91caff}
.badge-count{background:#f5f5f5;color:#999;border:1px solid #e8e8e8}
.badge-account{font-size:.68rem;color:#888}
.badge-revoked{background:#fafafa;color:#aaa;border:1px solid #eee;font-style:italic}
.timeline{padding:.6rem .75rem;display:flex;flex-direction:column;gap:.65rem}
.tl-msg{display:flex;flex-direction:column;gap:.12rem}
.tl-row{display:flex;flex-direction:column;gap:.12rem;max-width:100%}
.tl-row-self{align-items:flex-end}
.tl-row-other{align-items:flex-start}
.tl-meta{display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}
.tl-sender{font-size:.78rem;font-weight:600;color:#555}
.tl-staff{color:#0958d9}
.tl-sender-raw{font-size:.7rem;color:#bbb;font-weight:400}
.tl-time{font-size:.72rem;color:#bbb}
.tl-bubble{background:#f0f0f0;border-radius:11px;padding:.35rem .6rem;font-size:.83rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-width:580px}
.tl-bubble a{color:var(--link-color)}
.tl-bubble-staff{background:#e6f4ff;border-left:3px solid #1890ff}
.tl-bubble-self{background:var(--bubble-self-bg);color:var(--bubble-self-text)}
.tl-bubble-other{background:var(--bubble-other-bg);color:var(--bubble-other-text);border:1px solid #e8e8e8}
.tl-rcpt{font-size:.7rem;color:#ccc}
#timeline-top-sentinel{height:1px}
.history-status{margin:0 0 .5rem;padding:.35rem .6rem;text-align:center;font-size:.75rem;border-radius:3px}
.history-loading{color:#888}
.history-end{color:#bbb}
.history-error{color:#cf1322;background:#fff2f0;border:1px solid #ffccc7}
.history-retry-btn{margin-left:.5rem;padding:.15rem .6rem;font-size:.72rem;border:1px solid #d9d9d9;border-radius:3px;background:#fff;color:#555;cursor:pointer}
.history-retry-btn:hover{background:#f5f5f5}
.empty-state{padding:2.5rem 1rem;text-align:center;color:#ccc;font-size:.83rem}
.loading{padding:1rem;text-align:center;color:#bbb;font-size:.82rem}
.error-msg{margin:.5rem;padding:.6rem .75rem;background:#fff2f0;color:#cf1322;border:1px solid #ffccc7;border-radius:3px;font-size:.8rem}
.media-placeholder{background:#fafafa;border:1px dashed #d9d9d9;border-radius:4px;padding:.35rem .6rem;font-size:.8rem;color:#888;font-style:italic}
.revoke-time{font-size:.7rem;color:#bbb;margin-top:.2rem}
.media-preview{max-width:280px;max-height:280px;border-radius:4px;display:block}
.structured-card{background:#fff;border:1px solid #e8e8e8;border-radius:6px;padding:.5rem .65rem;font-size:.8rem;max-width:320px;overflow:hidden}
.structured-card-title{font-weight:600;margin-bottom:.2rem;word-break:break-word}
.structured-card-desc{color:#666;font-size:.76rem;margin-bottom:.3rem;word-break:break-word}
.structured-card-img{max-width:100%;max-height:180px;border-radius:4px;display:block;margin-bottom:.3rem;object-fit:cover}
.structured-card-meta{color:#999;font-size:.72rem;word-break:break-word}
.structured-card-hostname{color:#999;font-size:.72rem;word-break:break-word;margin-bottom:.2rem}
.structured-card-link-action{display:inline-block;font-size:.76rem;color:var(--link-color);text-decoration:none;border:1px solid currentColor;border-radius:3px;padding:.1rem .5rem}
.structured-card-link-disabled{color:#bbb;border-color:#eee;cursor:not-allowed}
.structured-card-degraded{color:#888;font-style:italic;font-size:.78rem}
.structured-card-type-label{display:inline-block;font-size:.68rem;color:#999;text-transform:uppercase;letter-spacing:.02em;margin-bottom:.25rem}
.structured-card-news-item{border-top:1px solid #f0f0f0;padding-top:.35rem;margin-top:.35rem}
.structured-card-news-item:first-child{border-top:none;padding-top:0;margin-top:0}
.structured-card-markdown ul{margin:.2rem 0 .2rem 1.1rem}
.structured-card-markdown li{margin-bottom:.1rem}
.new-msg-indicator{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);background:#1890ff;color:#fff;border:none;border-radius:999px;padding:.35rem 1rem;font-size:.78rem;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.new-msg-indicator:hover{background:#0958d9}
.lang-switch{position:relative}
.btn-lang{background:transparent;border:1px solid #3a4a5a;color:#8ca0b3;padding:.2rem .65rem;border-radius:3px;cursor:pointer;font-size:.78rem}
.btn-lang:hover{border-color:#8ca0b3;color:#fff}
.lang-menu{position:absolute;top:135%;right:0;background:#fff;border:1px solid #e8e8e8;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.18);min-width:7rem;overflow:hidden;z-index:50}
.lang-option{padding:.4rem .7rem;font-size:.8rem;color:#333;cursor:pointer;white-space:nowrap}
.lang-option:hover{background:#f5f5f5}
.lang-option.active{color:#1890ff;font-weight:600;background:#e6f4ff}
.media-video{max-width:320px;max-height:320px;border-radius:4px;display:block;background:#000}
.media-audio{width:260px;display:block}
.media-rich-loading{background:#fafafa;border:1px dashed #d9d9d9;border-radius:4px;padding:.35rem .6rem;font-size:.8rem;color:#888}
.file-card{display:flex;align-items:center;gap:.5rem;background:#fff;border:1px solid #e8e8e8;border-radius:6px;padding:.5rem .65rem;max-width:280px;text-decoration:none;color:inherit}
.file-card:hover{background:#f5f8ff}
.file-card-icon{font-size:1.3rem;flex-shrink:0}
.file-card-info{flex:1;min-width:0}
.file-card-type{font-size:.8rem;font-weight:600;word-break:break-word}
.file-card-meta{font-size:.72rem;color:#999}
.file-card-download{font-size:.72rem;color:#417ce8;flex-shrink:0}
.emotion-preview{max-width:150px;max-height:150px;border-radius:4px;display:block;cursor:zoom-in}
.chatrecord-card{background:#fff;border:1px solid #e8e8e8;border-radius:6px;padding:.5rem .65rem;max-width:300px;cursor:pointer}
.chatrecord-card:hover{background:#f5f8ff}
.chatrecord-card-title{font-weight:600;font-size:.83rem;margin-bottom:.15rem}
.chatrecord-card-summary{font-size:.78rem;color:#888;margin-bottom:.15rem;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.chatrecord-card-count{font-size:.72rem;color:#aaa}
.composite-wrap{display:flex;flex-direction:column;gap:.3rem;max-width:400px}
.composite-node{}
.composite-node-text{white-space:pre-wrap;word-break:break-word}
.composite-node-meta{font-size:.7rem;color:#aaa;margin-bottom:.1rem}
.composite-unknown{font-size:.78rem;color:#999;font-style:italic}
.v-overlay{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center}
.v-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.82)}
.v-close{position:absolute;top:16px;right:20px;z-index:2;background:transparent;border:none;color:#fff;font-size:2rem;line-height:1;cursor:pointer}
.v-nav{position:absolute;top:50%;transform:translateY(-50%);z-index:2;background:rgba(255,255,255,.12);border:none;color:#fff;font-size:2rem;width:2.6rem;height:2.6rem;border-radius:50%;cursor:pointer}
.v-prev{left:16px}
.v-next{right:16px}
.v-nav:hover{background:rgba(255,255,255,.25)}
.v-body{position:relative;z-index:1;max-width:90vw;max-height:88vh;display:flex;align-items:center;justify-content:center}
.v-media{max-width:90vw;max-height:88vh;object-fit:contain;border-radius:4px}
.v-loading,.v-error{color:#fff;font-size:.9rem;padding:2rem}
.v-chatrecord{background:#fff;border-radius:8px;padding:1rem 1.25rem;max-width:520px;max-height:80vh;overflow-y:auto}
.v-chatrecord-title{font-weight:700;font-size:1rem;margin-bottom:.6rem}
.v-chatrecord-node{border-bottom:1px solid #f0f0f0;padding:.5rem 0}
.v-chatrecord-node:last-child{border-bottom:none}
.v-chatrecord-sender{font-size:.78rem;font-weight:600;color:#555}
.v-chatrecord-time{font-size:.7rem;color:#bbb;margin-left:.4rem}
/* RND-159 search */
.search-bar{position:relative;flex:0 1 300px;min-width:0}
.search-bar input{width:100%;padding:.22rem .6rem;border:1px solid #3a4a5a;border-radius:4px;background:#0a1a2e;color:#ddd;font-size:.8rem;outline:none;box-sizing:border-box}
.search-bar input:focus{border-color:#1890ff;background:#0d2137}
.search-bar input::placeholder{color:#5c7185}
.search-results{position:absolute;top:115%;left:0;right:0;background:#fff;border:1px solid #e8e8e8;border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.15);max-height:460px;overflow-y:auto;z-index:200;display:none}
.sr-section{padding:.3rem 0}
.sr-section-header{padding:.2rem .6rem;font-size:.72rem;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.03em}
.sr-item{padding:.35rem .6rem;cursor:pointer;font-size:.8rem;color:#333;display:flex;flex-direction:column;gap:.08rem}
.sr-item:hover{background:#e6f4ff}
.sr-item-main{display:flex;align-items:center;gap:.4rem}
.sr-item-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sr-item-raw{font-size:.7rem;color:#bbb;flex-shrink:0}
.sr-item-snippet{font-size:.75rem;color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.4}
.sr-item-time{font-size:.7rem;color:#bbb}
.sr-item-conv{font-size:.72rem;color:#888}
.sr-highlight{color:#e00;font-weight:600}
.sr-empty,.sr-loading,.sr-error{padding:.6rem;text-align:center;font-size:.8rem;color:#ccc}
.sr-error{color:#cf1322;background:#fff2f0}
.sr-loading{color:#888}
.sr-divider{height:1px;background:#f0f0f0;margin:0}
</style>
</head>
<body>
""" + I18N_SCRIPT_TAG + """
<div class="top-bar">
  <h1 data-i18n="app.subtitle">对话审阅控制台</h1>
  <span class="tz-note" data-i18n="console.tzNote">时间均为北京时间 (UTC+8)</span>
  <div class="refresh-bar">
    <span id="refresh-status" class="refresh-status"></span>
    <button class="btn-refresh" id="btn-refresh" onclick="refreshNow('manual')" data-i18n="refresh.manual">刷新</button>
  </div>
  <div class="search-bar" id="search-bar">
    <input type="text" id="search-input" placeholder="搜索联系人或聊天内容…" autocomplete="off">
    <div class="search-results" id="search-results"></div>
  </div>
  <div class="top-bar-user">
    <span id="current-user"></span>
    <a href="/admin/messages"><span data-i18n="nav.messages">消息记录</span> ↗</a>
    <a href="/admin/diagnostics/reachability"><span data-i18n="nav.diagnostics">系统诊断</span> ↗</a>
    <div class="lang-switch" id="lang-switch">
      <button class="btn-lang" id="btn-lang-toggle" type="button" onclick="toggleLangMenu()" data-i18n="nav.language">语言</button>
      <div class="lang-menu" id="lang-menu" style="display:none"></div>
    </div>
    <button class="btn-logout" onclick="doLogout()" data-i18n="nav.logout">退出登录</button>
  </div>
</div>
<div class="layout">
  <div class="col col-left">
    <div class="mode-tabs">
      <button class="mode-tab active" id="tab-staff" onclick="setMode('staff')" data-i18n="nav.staff">员工</button>
      <button class="mode-tab" id="tab-contact" onclick="setMode('contact')" data-i18n="nav.contact">联系人</button>
    </div>
    <div class="col-header" id="entity-header" data-i18n="console.monitoredAccounts">监控账号</div>
    <div class="col-body" id="entity-body"><div class="loading" data-i18n="console.loading">加载中…</div></div>
  </div>
  <div class="col col-mid">
    <div class="col-header" id="conv-header" data-i18n="nav.conversations">会话</div>
    <div class="col-body" id="conv-body"><div class="empty-state" data-i18n="console.selectAccountOrContact">请选择账号或联系人</div></div>
  </div>
  <div class="col col-right">
    <div class="col-header" id="timeline-header" data-i18n="console.timelineHeader">消息时间线</div>
    <div class="col-body" id="timeline-body"><div class="empty-state" data-i18n="console.selectConversation">请选择会话</div></div>
    <button class="new-msg-indicator" id="new-msg-indicator" style="display:none" onclick="scrollTimelineToBottom()"><span data-i18n="refresh.newMessages">有新消息</span> ↓</button>
  </div>
</div>
<script>
var mode='staff',selEntityId=null,selConvId=null,selEntityName=null,selConvName=null;
var lastEntityItems=null,lastConvItems=null;
// RND-204: signatures of the exact entity/conversation lists last painted,
// used only to skip a full list re-render during a background auto-refresh
// when nothing changed -- this is what stops the every-cycle whole-list
// rebuild (and its jitter / active-selection churn) called out in the
// incremental-refresh redesign. The guard lives ONLY in the refresh path
// (refreshEntityList/refreshConversationList); the initial-load and
// locale-switch paths always render.
var lastEntitySig=null,lastConvSig=null;
var timelineConvId=null,timelineMsgs=[],timelineHasOlder=false,timelineNextBefore=null,timelineLoadingOlder=false;
// RND-158 Phase 2 (API-contract round): entity context captured at the
// moment loadTimeline() is called for the CURRENTLY selected conversation
// -- not read live from mode/selEntityId at fetch time, since the user can
// switch entities while a timeline request is in flight. Same
// capture-at-call-time principle timelineRequestGen already establishes.
var timelineConvType=null,timelineMode=null,timelineEntityId=null;
var timelineHistoryError=null,timelineTopObserver=null;
// RND-206 QA fix: a monotonically increasing generation token bumped every
// time the active conversation changes (loadTimeline). Every in-flight
// timeline request captures its own requestConvId+gen at send time and
// re-checks both before applying its response -- a response that arrives
// after the user has switched (or switched back to) a conversation is
// silently dropped rather than clobbering newer state. See fetchTimelinePage/
// refreshTimelineIfSelected/fetchOlderMessages.
var timelineRequestGen=0;
// Signature of the exact array renderTimeline() last painted, used only to
// detect a semantically-unchanged auto-refresh (RND-206 QA fix #6) so an
// unchanged refresh can skip the DOM rebuild entirely and preserve live
// <video>/<audio> playback state instead of tearing it down and rebuilding.
var lastRenderedTimelineSignature=null;
var REFRESH_INTERVAL_SEC=30;
var refreshCountdownSec=REFRESH_INTERVAL_SEC;
var refreshTickTimer=null;
var refreshInFlight=false;
var refreshErrorText=null;
var lastRefreshAt=null;
function esc(s){
  return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtTime(ms){
  // Display-only: renders Beijing time (UTC+8, no DST) from a UTC epoch-ms value.
  // Raw ms is never mutated; ordering/pagination always use the original value.
  // Timezone is communicated once via the page-level tz-note, not per value —
  // this returns a bare "YYYY-MM-DD HH:mm:ss" with no timezone suffix.
  if(!ms)return'';
  var d=new Date(ms+8*3600*1000);
  return d.getUTCFullYear()+'-'+pad(d.getUTCMonth()+1)+'-'+pad(d.getUTCDate())+' '+pad(d.getUTCHours())+':'+pad(d.getUTCMinutes())+':'+pad(d.getUTCSeconds());
}
function pad(n){return String(n).padStart(2,'0');}
function handleUnauth(r){
  if(r.status===401){window.location.href='/admin/login';return true;}
  return false;
}
function applyStaticI18n(){
  document.documentElement.lang=I18N.getLocale();
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent=I18N.t(el.getAttribute('data-i18n'));
  });
}
function renderLangMenu(){
  var menu=document.getElementById('lang-menu');
  if(!menu)return;
  var current=I18N.getLocale();
  var html='';
  I18N.availableLocales().forEach(function(loc){
    var cls='lang-option'+(loc.code===current?' active':'');
    html+='<div class="'+cls+'" onclick="selectLocale(&quot;'+loc.code+'&quot;)">'+esc(loc.nativeName)+'</div>';
  });
  menu.innerHTML=html;
}
function toggleLangMenu(){
  var menu=document.getElementById('lang-menu');
  if(!menu)return;
  if(menu.style.display==='block'){menu.style.display='none';return;}
  renderLangMenu();
  menu.style.display='block';
}
function selectLocale(code){
  I18N.setLocale(code);
  var menu=document.getElementById('lang-menu');
  if(menu)menu.style.display='none';
  applyLocale();
}
function applyLocale(){
  applyStaticI18n();
  renderLangMenu();
  rebuildMediaLabels();
  document.getElementById('entity-header').textContent=mode==='staff'?I18N.t('console.monitoredAccounts'):I18N.t('console.contactsHeader');
  document.getElementById('conv-header').textContent=selEntityName?(I18N.t('nav.conversations')+' — '+selEntityName):I18N.t('nav.conversations');
  document.getElementById('timeline-header').textContent=selConvName?(I18N.t('console.timelineHeader')+' — '+selConvName):I18N.t('console.timelineHeader');
  if(lastEntityItems)renderEntityList(lastEntityItems);
  if(selEntityId&&lastConvItems){
    renderConvList(lastConvItems);
  }else if(!selEntityId){
    document.getElementById('conv-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectAccountOrContact')+'</div>';
  }
  if(timelineConvId&&timelineMsgs.length){
    renderTimeline(false);
  }else if(!timelineConvId){
    document.getElementById('timeline-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  }
  updateRefreshStatus();
}
document.addEventListener('click',function(e){
  var sw=document.getElementById('lang-switch');
  var menu=document.getElementById('lang-menu');
  if(sw&&menu&&!sw.contains(e.target))menu.style.display='none';
});
function loadCurrentUser(){
  fetch('/api/auth/me').then(function(r){return r.json();}).then(function(d){
    if(!d.authenticated){window.location.href='/admin/login';return;}
    var el=document.getElementById('current-user');
    if(el)el.textContent=d.display_name||d.wecom_user_id||'';
  }).catch(function(){});
}
function doLogout(){
  fetch('/api/auth/logout',{method:'POST'}).then(function(){
    window.location.href='/admin/login';
  }).catch(function(){window.location.href='/admin/login';});
}
function setMode(m){
  mode=m; selEntityId=null; selConvId=null; selEntityName=null; selConvName=null;
  lastConvItems=null;
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.getElementById('tab-staff').classList.toggle('active',m==='staff');
  document.getElementById('tab-contact').classList.toggle('active',m==='contact');
  document.getElementById('entity-header').textContent=m==='staff'?I18N.t('console.monitoredAccounts'):I18N.t('console.contactsHeader');
  document.getElementById('conv-header').textContent=I18N.t('nav.conversations');
  document.getElementById('timeline-header').textContent=I18N.t('console.timelineHeader');
  document.getElementById('conv-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectAccountOrContact')+'</div>';
  document.getElementById('timeline-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  hideNewMessageIndicator();
  loadEntityList();
}
function loadEntityList(){
  var url=mode==='staff'?'/api/monitored-accounts':'/api/contacts';
  document.getElementById('entity-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
  fetch(url).then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(items){if(items)renderEntityList(items);})
    .catch(function(){document.getElementById('entity-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadEntities')+'</div>';});
}
function entityListSignature(items){
  return JSON.stringify((items||[]).map(function(it){
    return [mode==='staff'?it.staff_id:it.contact_id,it.raw_id,it.display_name,it.seat_status];
  }));
}
function convListSignature(convs){
  return JSON.stringify((convs||[]).map(function(c){
    return [c.conversation_id,c.conversation_type,c.display_name,c.last_message_text,
      c.last_message_time,c.message_count,c.raw_id||c.room_raw_id||null,
      (c.monitored_account_display_names||c.monitored_account_ids||[]).join(',')];
  }));
}
function renderEntityList(items){
  lastEntityItems=items;
  lastEntitySig=entityListSignature(items);
  var body=document.getElementById('entity-body');
  if(!items||!items.length){body.innerHTML='<div class="empty-state">'+I18N.t('console.noneFound')+'</div>';return;}
  var html='';
  items.forEach(function(item){
    var id=mode==='staff'?item.staff_id:item.contact_id;
    var rawId=item.raw_id||id;
    var name=item.display_name||id;
    var av=esc(name.charAt(0).toUpperCase());
    var bg=mode==='staff'?'#1890ff':'#389e0d';
    var secondary=(rawId&&rawId!==name)?'<span class="entity-raw"> · '+esc(rawId)+'</span>':'';
    var seatBadge='';
    if(mode==='staff'&&item.seat_status){
      var seatCls=item.seat_status==='active'?'seat-badge-active':'seat-badge-history';
      var seatLabel=item.seat_status==='active'?I18N.t('console.seatActive'):I18N.t('console.seatHistory');
      seatBadge='<span class="seat-badge '+seatCls+'">'+esc(seatLabel)+'</span>';
    }
    html+='<div class="entity-item" data-id="'+esc(id)+'" data-name="'+esc(name)+'" onclick="onEntityClick(this)">'
      +'<div class="entity-avatar" style="background:'+bg+'">'+av+'</div>'
      +'<span class="entity-name">'+esc(name)+secondary+'</span>'+seatBadge+'</div>';
  });
  body.innerHTML=html;
  if(selEntityId){
    body.querySelectorAll('.entity-item').forEach(function(el){
      el.classList.toggle('active',el.dataset.id===selEntityId);
    });
  }
}
function onEntityClick(el){
  selEntityId=el.dataset.id; selEntityName=el.dataset.name; selConvId=null; selConvName=null;
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.querySelectorAll('.entity-item').forEach(function(e){e.classList.remove('active');});
  el.classList.add('active');
  document.getElementById('conv-header').textContent=I18N.t('nav.conversations')+' — '+el.dataset.name;
  document.getElementById('timeline-header').textContent=I18N.t('console.timelineHeader');
  document.getElementById('timeline-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  loadConversations(selEntityId);
}
function loadConversations(entityId){
  var url=mode==='staff'
    ?'/api/conversations?mode=staff&staff_id='+encodeURIComponent(entityId)
    :'/api/conversations?mode=contact&contact_id='+encodeURIComponent(entityId);
  document.getElementById('conv-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
  fetch(url).then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(convs){if(convs)renderConvList(convs);})
    .catch(function(){document.getElementById('conv-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadConversations')+'</div>';});
}
function renderConvList(convs){
  lastConvItems=convs;
  lastConvSig=convListSignature(convs);
  var body=document.getElementById('conv-body');
  if(!convs||!convs.length){body.innerHTML='<div class="empty-state">'+I18N.t('console.noConversations')+'</div>';return;}
  var html='';
  convs.forEach(function(c){
    var tb=c.conversation_type==='group'
      ?'<span class="badge badge-group">'+esc(I18N.t('convList.groupBadge'))+'</span>'
      :'<span class="badge badge-direct">'+esc(I18N.t('convList.directBadge'))+'</span>';
    var raw=c.last_message_text||'';
    var snip=raw.length>60?esc(raw.substring(0,60))+'…':esc(raw);
    var t=fmtTime(c.last_message_time);
    var acctNames=(c.monitored_account_display_names&&c.monitored_account_display_names.length)
      ?c.monitored_account_display_names:(c.monitored_account_ids||[]);
    var acct=(mode==='contact'&&acctNames.length)
      ?'<span class="badge-account">'+esc(acctNames.join(', '))+'</span>':'';
    var rawId=c.raw_id||c.room_raw_id||'';
    var secondary=(rawId&&rawId!==c.display_name)
      ?'<div class="conv-secondary" title="'+esc(rawId)+'">'+esc(rawId)+'</div>':'';
    html+='<div class="conv-card" data-id="'+esc(c.conversation_id)+'" data-name="'+esc(c.display_name)+'" data-type="'+esc(c.conversation_type)+'" onclick="onConvClick(this)">'
      +'<div class="conv-top"><span class="conv-name" title="'+esc(rawId)+'">'+esc(c.display_name)+'</span><span class="conv-time">'+esc(t)+'</span></div>'
      +secondary
      +(snip?'<div class="conv-snippet">'+snip+'</div>':'')
      +'<div class="conv-meta">'+tb+' <span class="badge badge-count">'+esc(c.message_count)+' '+esc(I18N.t('convList.messagesSuffix'))+'</span>'+acct+'</div>'
      +'</div>';
  });
  body.innerHTML=html;
  if(selConvId){
    body.querySelectorAll('.conv-card').forEach(function(el){
      el.classList.toggle('active',el.dataset.id===selConvId);
    });
  }
}
function onConvClick(el){
  selConvId=el.dataset.id; selConvName=el.dataset.name;
  document.querySelectorAll('.conv-card').forEach(function(e){e.classList.remove('active');});
  el.classList.add('active');
  document.getElementById('timeline-header').textContent=I18N.t('console.timelineHeader')+' — '+el.dataset.name;
  loadTimeline(selConvId, el.dataset.type);
}
function loadTimeline(convId, convType){
  timelineConvId=convId; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  timelineLoadingOlder=false; timelineHistoryError=null;
  // RND-158 Phase 2: capture the entity context for THIS timeline
  // selection now, not read live later -- see the declaration comment on
  // timelineConvType/timelineMode/timelineEntityId above.
  timelineConvType=convType||null; timelineMode=mode; timelineEntityId=selEntityId;
  timelineRequestGen++;
  stopHistoryObserver();
  hideNewMessageIndicator();
  document.getElementById('timeline-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
  fetchTimelinePage(null, true);
}
// RND-158 Phase 2: builds the '&mode=...&staff_id=/contact_id=...&conversation_type=...'
// query-string suffix from the entity context captured for the current
// timeline selection, mirroring refreshConversationList()'s staff/contact
// URL-building style. Shared by all three timeline URL-building call
// sites (fetchTimelinePage, fetchOlderMessages, refreshTimelineIfSelected)
// so they never diverge.
function timelineEntityQueryParams(){
  var qs='';
  if(timelineMode==='staff'&&timelineEntityId){
    qs+='&mode=staff&staff_id='+encodeURIComponent(timelineEntityId);
  }else if(timelineMode==='contact'&&timelineEntityId){
    qs+='&mode=contact&contact_id='+encodeURIComponent(timelineEntityId);
  }
  if(timelineConvType)qs+='&conversation_type='+encodeURIComponent(timelineConvType);
  return qs;
}
// RND-206 QA fix: captures requestConvId+gen at send time (not at resolve
// time, when the user may have already switched conversations) and drops
// the response if either no longer matches current state -- fixes "a slow
// response from the previous conversation overwrote the current
// conversation" (confirmed browser defect).
function fetchTimelinePage(before, isInitial){
  var requestConvId=timelineConvId, gen=timelineRequestGen;
  var url='/api/conversations/'+encodeURIComponent(requestConvId)+'/messages?limit=20';
  if(before)url+='&before='+encodeURIComponent(before);
  url+=timelineEntityQueryParams();
  return fetch(url)
    .then(function(r){if(handleUnauth(r))return null;if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(function(data){
      if(!data)return;
      if(timelineConvId!==requestConvId||timelineRequestGen!==gen)return;
      timelineMsgs=before?data.messages.concat(timelineMsgs):data.messages;
      timelineHasOlder=data.pagination.has_older;
      timelineNextBefore=data.pagination.next_before;
      renderTimeline(isInitial);
      startHistoryObserver();
    })
    .catch(function(e){
      if(timelineConvId!==requestConvId||timelineRequestGen!==gen)return;
      document.getElementById('timeline-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadPrefix')+esc(e.message)+'</div>';
    });
}
function isNearTop(){
  var body=document.getElementById('timeline-body');
  if(!body)return false;
  return body.scrollTop<80;
}
function preserveScrollPosition(body,beforeHeight){
  if(!body)return;
  body.scrollTop+=(body.scrollHeight-beforeHeight);
}
function historyStatusEl(){return document.getElementById('timeline-history-status');}
function showLoadingOlder(){
  var el=historyStatusEl();
  if(el)el.innerHTML='<div class="history-status history-loading">'+I18N.t('history.loadingOlder')+'</div>';
}
function showEndOfHistory(){
  var el=historyStatusEl();
  if(el)el.innerHTML='<div class="history-status history-end">'+I18N.t('history.noMore')+'</div>';
}
function historyRetryHtml(){
  return '<div class="history-status history-error">'+I18N.t('history.failedToLoad')
    +'<button class="history-retry-btn" onclick="retryLoadOlder()">'+I18N.t('history.retry')+'</button></div>';
}
function showHistoryRetry(){
  var el=historyStatusEl();
  if(el)el.innerHTML=historyRetryHtml();
}
function fetchOlderMessages(convId,before){
  // RND-206 QA fix: captures the generation token at call time (this is
  // always invoked synchronously from loadOlderAutomatically, right when
  // the request starts, same guarantee as requestConvId below) rather than
  // taking a third parameter, so the tested two-argument signature stays
  // unchanged.
  // RND-158 Phase 2: entity context is likewise read synchronously here
  // (not passed as a new parameter, for the same 2-arg-signature-stability
  // reason as `gen` above) via timelineEntityQueryParams(), which reads
  // timelineMode/timelineEntityId/timelineConvType -- captured once at
  // loadTimeline() time for the conversation this call is already scoped
  // to via the timelineConvId/timelineRequestGen guard below.
  var gen=timelineRequestGen;
  var url='/api/conversations/'+encodeURIComponent(convId)+'/messages?limit=20&before='+encodeURIComponent(before)+timelineEntityQueryParams();
  return fetch(url).then(function(r){
    if(handleUnauth(r)){var e=new Error('unauthorized');e.handled=true;throw e;}
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(data){
    if(timelineConvId!==convId||timelineRequestGen!==gen)return;
    timelineMsgs=data.messages.concat(timelineMsgs);
    timelineHasOlder=data.pagination.has_older;
    timelineNextBefore=data.pagination.next_before;
  });
}
function loadOlderAutomatically(){
  if(timelineLoadingOlder||!timelineHasOlder||timelineHistoryError)return;
  var requestConvId=timelineConvId, requestGen=timelineRequestGen;
  var body=document.getElementById('timeline-body');
  var beforeHeight=body?body.scrollHeight:0;
  timelineLoadingOlder=true;
  showLoadingOlder();
  preserveScrollPosition(body,beforeHeight);
  var beforeHeight2=body?body.scrollHeight:0;
  fetchOlderMessages(requestConvId,timelineNextBefore).then(function(){
    if(timelineConvId!==requestConvId||timelineRequestGen!==requestGen)return;
    timelineLoadingOlder=false;
    renderTimeline(false);
    startHistoryObserver();
    preserveScrollPosition(body,beforeHeight2);
  }).catch(function(e){
    if(timelineConvId!==requestConvId||timelineRequestGen!==requestGen)return;
    timelineLoadingOlder=false;
    if(e&&e.handled)return;
    timelineHistoryError=(e&&e.message)?e.message:'load failed';
    showHistoryRetry();
    preserveScrollPosition(body,beforeHeight2);
  });
}
function retryLoadOlder(){
  timelineHistoryError=null;
  loadOlderAutomatically();
}
function startHistoryObserver(){
  stopHistoryObserver();
  var root=document.getElementById('timeline-body');
  var sentinel=document.getElementById('timeline-top-sentinel');
  if(!root||!sentinel||typeof IntersectionObserver==='undefined')return;
  timelineTopObserver=new IntersectionObserver(function(entries){
    entries.forEach(function(entry){
      if(entry.isIntersecting&&isNearTop())loadOlderAutomatically();
    });
  },{root:root,threshold:0});
  timelineTopObserver.observe(sentinel);
}
function stopHistoryObserver(){
  if(timelineTopObserver){timelineTopObserver.disconnect();timelineTopObserver=null;}
}
var MEDIA_LABELS={image:I18N.t('media.image'),video:I18N.t('media.video'),voice:I18N.t('media.voice'),file:I18N.t('media.file')};
var MEDIA_STATUS_LABELS={not_downloaded:I18N.t('media.status.notDownloaded'),unsupported:I18N.t('media.status.unsupported'),unknown:I18N.t('media.status.unknown'),failed:I18N.t('media.status.failed')};
function rebuildMediaLabels(){
  MEDIA_LABELS={image:I18N.t('media.image'),video:I18N.t('media.video'),voice:I18N.t('media.voice'),file:I18N.t('media.file')};
  MEDIA_STATUS_LABELS={not_downloaded:I18N.t('media.status.notDownloaded'),unsupported:I18N.t('media.status.unsupported'),unknown:I18N.t('media.status.unknown'),failed:I18N.t('media.status.failed')};
}
var MessageTypeRegistry=(function(){
  var entries=""" + _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON + """;
  var FALLBACK={category:'placeholder',placeholderKey:'placeholder.unsupported'};
  function resolve(msgtype){
    return (msgtype&&Object.prototype.hasOwnProperty.call(entries,msgtype))?entries[msgtype]:null;
  }
  function resolvePlaceholder(msgtype){
    var entry=resolve(msgtype);
    return (entry&&entry.category==='placeholder')?entry:null;
  }
  // RND-198: resolveSystem() returns the entry if it is a "system" category
  // entry (used by the system card renderer dispatch). Returns null otherwise.
  function resolveSystem(msgtype){
    var entry=resolve(msgtype);
    return (entry&&entry.category==='system')?entry:null;
  }
  return {entries:entries,resolve:resolve,resolvePlaceholder:resolvePlaceholder,resolveSystem:resolveSystem,fallback:FALLBACK};
})();
// RND-197 — structured card rendering. isSafeUrl mirrors the backend's
// app.structured_message_parser.safe_url (http/https absolute URLs only)
// as defense-in-depth: structured_content.fields is already filtered
// server-side, but nothing here should trust that without re-checking at
// the point a value becomes an href/src.
function isSafeUrl(u){
  if(!u||typeof u!=='string')return false;
  try{
    var parsed=new URL(u);
    return (parsed.protocol==='http:'||parsed.protocol==='https:')&&!!parsed.host;
  }catch(e){return false;}
}
function hostnameOf(u){
  try{return new URL(u).hostname;}catch(e){return null;}
}
function fmtCoord(n){return (typeof n==='number'&&!isNaN(n))?n.toFixed(6):'';}
// Shared "known type, content unavailable" card — used for card/docmsg/
// audio_doc (no field extraction attempted at all — see
// app.structured_message_parser module docstring) and as the terminal
// fallback for any structured type whose fields failed to parse
// (malformed/historical dirty data).
function renderStructuredFallback(m){
  var extra=(m.normalized_type==='audio_doc')
    ?I18N.t('card.audioDoc.playbackUnavailable')
    :I18N.t('card.generic.unavailable');
  return '<div class="structured-card structured-card-fallback">'
    +'<div class="structured-card-type-label">'+esc(I18N.t(m.display_label_key))+'</div>'
    +'<div class="structured-card-degraded">'+esc(extra)+'</div>'
    +'</div>';
}
function renderLinkCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var url=isSafeUrl(f.url)?f.url:null;
  var host=url?hostnameOf(url):null;
  var title=f.title||host||I18N.t('messageType.link');
  var html='<div class="structured-card structured-card-link">';
  if(f.image_url&&isSafeUrl(f.image_url)){
    html+='<img class="structured-card-img" src="'+esc(f.image_url)+'" alt="" loading="lazy" onerror="this.remove()">';
  }
  html+='<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.description)html+='<div class="structured-card-desc">'+esc(f.description)+'</div>';
  // RND-206 QA fix #14: hostname shown as its own line (distinct from the
  // title, which may equal it as a fallback above) and a localized
  // "open link" action instead of duplicating the full raw URL as the
  // link's visible text. href/target/rel and the http(s)-only safety
  // check (isSafeUrl) are unchanged; an unsafe/missing URL renders a
  // disabled, non-navigable action instead of ever falling back to an
  // unvalidated href.
  if(host)html+='<div class="structured-card-hostname">'+esc(host)+'</div>';
  html+=url
    ?'<a class="structured-card-link-action" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(I18N.t('link.openLink'))+'</a>'
    :'<span class="structured-card-link-action structured-card-link-disabled" aria-disabled="true">'+esc(I18N.t('card.link.unavailable'))+'</span>';
  html+='</div>';
  return html;
}
function renderLocationCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var primary=f.name||f.address||null;
  var hasCoords=typeof f.latitude==='number'&&typeof f.longitude==='number';
  var html='<div class="structured-card structured-card-location">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.location'))+'</div>';
  if(primary){
    html+='<div class="structured-card-title">'+esc(primary)+'</div>';
    if(f.name&&f.address&&f.address!==f.name)html+='<div class="structured-card-desc">'+esc(f.address)+'</div>';
    if(hasCoords)html+='<div class="structured-card-meta">'+fmtCoord(f.latitude)+', '+fmtCoord(f.longitude)+'</div>';
  }else if(hasCoords){
    html+='<div class="structured-card-title">'+fmtCoord(f.latitude)+', '+fmtCoord(f.longitude)+'</div>';
  }else{
    html+='<div class="structured-card-degraded">'+esc(I18N.t('card.location.unknown'))+'</div>';
  }
  html+='</div>';
  return html;
}
// Escape-first, fixed-subset sanitizer — no markdown dependency. Link
// URLs are validated against the raw (pre-escape) value with isSafeUrl
// and substituted back in after the rest of the text is escaped, so an
// unsafe/malformed link degrades to its escaped literal text rather than
// ever reaching innerHTML unescaped.
function renderSanitizedMarkdown(raw){
  var text=String(raw);
  var links=[];
  text=text.replace(/\\[([^\\]\\n]*)\\]\\(([^)\\n]*)\\)/g,function(whole,label,url){
    var token=' LINK'+links.length+' ';
    links.push({label:label||url,url:isSafeUrl(url)?url:null});
    return token;
  });
  text=esc(text);
  text=text.replace(/\\*\\*([^*\\n]+)\\*\\*/g,'<strong>$1</strong>');
  var out=[];var inList=false;
  text.split(/\\n/).forEach(function(line){
    var bullet=line.match(/^\\s*[-*]\\s+(.*)$/);
    if(bullet){
      if(!inList){out.push('<ul>');inList=true;}
      out.push('<li>'+bullet[1]+'</li>');
    }else{
      if(inList){out.push('</ul>');inList=false;}
      out.push(line+'<br>');
    }
  });
  if(inList)out.push('</ul>');
  var html=out.join('').replace(/<br>$/,'');
  links.forEach(function(link,i){
    var token=' LINK'+i+' ';
    var replacement=link.url
      ?'<a href="'+esc(link.url)+'" target="_blank" rel="noopener noreferrer">'+esc(link.label)+'</a>'
      :esc('['+link.label+']');
    html=html.split(token).join(replacement);
  });
  return html;
}
function renderMarkdownCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f||!f.content){
    return '<div class="structured-card structured-card-markdown"><div class="structured-card-degraded">'+esc(I18N.t('card.markdown.empty'))+'</div></div>';
  }
  return '<div class="structured-card structured-card-markdown">'+renderSanitizedMarkdown(f.content)+'</div>';
}
function renderNewsCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  var articles=(f&&f.articles)||[];
  if(!Array.isArray(articles))articles=[];
  if(!articles.length){
    return '<div class="structured-card structured-card-news"><div class="structured-card-degraded">'+esc(I18N.t('card.news.empty'))+'</div></div>';
  }
  var html='<div class="structured-card structured-card-news">';
  articles.forEach(function(a){
    var url=isSafeUrl(a.url)?a.url:null;
    var title=a.title||I18N.t('card.news.noTitle');
    html+='<div class="structured-card-news-item">';
    if(a.image_url&&isSafeUrl(a.image_url)){
      html+='<img class="structured-card-img" src="'+esc(a.image_url)+'" alt="" loading="lazy" onerror="this.remove()">';
    }
    html+=url
      ?'<a class="structured-card-title" style="color:inherit;text-decoration:none;display:block" href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(title)+'</a>'
      :'<div class="structured-card-title">'+esc(title)+'</div>';
    if(a.description)html+='<div class="structured-card-desc">'+esc(a.description)+'</div>';
    html+='</div>';
  });
  html+='</div>';
  return html;
}
function renderMiniprogramCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||f.display_name||I18N.t('messageType.miniprogram');
  var html='<div class="structured-card structured-card-miniprogram">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.miniprogram'))+'</div>';
  if(f.icon_url&&isSafeUrl(f.icon_url)){
    html+='<img class="structured-card-img" style="max-height:60px;max-width:60px" src="'+esc(f.icon_url)+'" alt="" loading="lazy" onerror="this.remove()">';
  }
  html+='<div class="structured-card-title">'+esc(title)+'</div>';
  // pagepath is an internal mini-program route, not a browser URL — never
  // rendered as a clickable link (ticket requirement).
  if(f.username)html+='<div class="structured-card-meta">'+esc(f.username)+'</div>';
  html+='</div>';
  return html;
}
// RND-198: business card renderers for interactive message types.
function renderVoteCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.vote');
  var html='<div class="structured-card structured-card-vote">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.vote'))+'</div>'
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.type)html+='<div class="structured-card-meta">'+esc(I18N.t('card.vote.type'))+esc(f.type)+'</div>';
  if(Array.isArray(f.items)&&f.items.length){
    html+='<ul style="margin:.2rem 0 .2rem 1.1rem">';
    f.items.forEach(function(item){
      var name=item.name||I18N.t('card.vote.unnamed');
      var count=(typeof item.count==='number')?' ('+item.count+')':'';
      html+='<li>'+esc(name)+count+'</li>';
    });
    html+='</ul>';
  }else{
    html+='<div class="structured-card-degraded">'+esc(I18N.t('card.vote.noItems'))+'</div>';
  }
  html+='</div>';
  return html;
}
function renderTodoCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.todo');
  var html='<div class="structured-card structured-card-todo">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.todo'))+'</div>'
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.content)html+='<div class="structured-card-desc">'+esc(f.content)+'</div>';
  html+='</div>';
  return html;
}
function renderCollectCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.collect');
  var html='<div class="structured-card structured-card-collect">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.collect'))+'</div>'
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(Array.isArray(f.details)&&f.details.length){
    html+='<div class="structured-card-meta">'+esc(f.details.length+' '+I18N.t('card.collect.entries'))+'</div>';
  }
  html+='</div>';
  return html;
}
function renderMeetingCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.meeting');
  var html='<div class="structured-card structured-card-meeting">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.meeting'))+'</div>'
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.time)html+='<div class="structured-card-meta">'+esc(I18N.t('card.meeting.time'))+fmtTime(f.time)+'</div>';
  if(f.place)html+='<div class="structured-card-meta">'+esc(I18N.t('card.meeting.place'))+esc(f.place)+'</div>';
  if(f.agenda)html+='<div class="structured-card-desc">'+esc(f.agenda)+'</div>';
  html+='</div>';
  return html;
}
function renderScheduleCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||I18N.t('messageType.schedule');
  var html='<div class="structured-card structured-card-schedule">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.schedule'))+'</div>'
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.starttime)html+='<div class="structured-card-meta">'+esc(I18N.t('card.schedule.start'))+fmtTime(f.starttime)+'</div>';
  if(f.endtime)html+='<div class="structured-card-meta">'+esc(I18N.t('card.schedule.end'))+fmtTime(f.endtime)+'</div>';
  if(f.place)html+='<div class="structured-card-meta">'+esc(I18N.t('card.schedule.place'))+esc(f.place)+'</div>';
  if(f.description)html+='<div class="structured-card-desc">'+esc(f.description)+'</div>';
  html+='</div>';
  return html;
}
function renderRedpacketCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  // Redpacket card never shows monetary amounts (security).
  var label=I18N.t('card.redpacket.label');
  var wishing=f&&f.wishing||null;
  var html='<div class="structured-card structured-card-redpacket">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.redpacket'))+'</div>'
    +'<div class="structured-card-title">'+esc(label)+'</div>';
  if(wishing)html+='<div class="structured-card-desc">'+esc(wishing)+'</div>';
  if(f&&typeof f.totalnum==='number')html+='<div class="structured-card-meta">'+esc(f.totalnum+' '+I18N.t('card.redpacket.nPackets'))+'</div>';
  html+='</div>';
  return html;
}
function renderSwitchCorpCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var corpName=f.corp_name||'';
  var html='<div class="structured-card structured-card-switchcorp">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.switchCorp'))+'</div>';
  if(corpName)html+='<div class="structured-card-title">'+esc(I18N.t('card.switchCorp.switchedTo'))+esc(corpName)+'</div>';
  html+='</div>';
  return html;
}
// RND-198: system event card renderer — dispatches by action subtype,
// renders a distinct (non-bubble) card visually separate from chat messages.
// QA fix: unknown subtypes must never render raw i18n keys (e.g.
// "system.event.future_action"). If I18N.t() returns the key itself
// (no translation exists), fall back to the generic localized label.
function renderSystemCard(m){
  var f=m.structured_content&&m.structured_content.fields;
  var subtype=f&&f.subtype||null;
  var displayText=f&&f.display_text||null;
  var html='<div class="system-card" style="text-align:center;font-size:.78rem;color:#999;padding:.25rem .5rem;">';
  if(displayText){
    html+=esc(displayText);
  }else if(subtype){
    var key='system.event.'+subtype;
    var translated=I18N.t(key);
    // I18N.t() returns the key itself when no translation exists —
    // detect this and fall back to the generic system event label.
    if(translated===key){
      html+=esc(I18N.t('system.event.unknown'));
    }else{
      html+=esc(translated);
    }
  }else{
    html+=esc(I18N.t('system.event.unknown'));
  }
  html+='</div>';
  return html;
}
var STRUCTURED_CARD_RENDERERS={
  link:renderLinkCard,
  location:renderLocationCard,
  markdown:renderMarkdownCard,
  news:renderNewsCard,
  miniprogram:renderMiniprogramCard,
  card:renderStructuredFallback,
  docmsg:renderStructuredFallback,
  audio_doc:renderStructuredFallback,
  // RND-198 interactive business types
  vote:renderVoteCard,
  todo:renderTodoCard,
  collect:renderCollectCard,
  meeting:renderMeetingCard,
  schedule:renderScheduleCard,
  redpacket:renderRedpacketCard,
  switch_corp:renderSwitchCorpCard
};
function renderStructuredCard(m){
  var fn=STRUCTURED_CARD_RENDERERS[m.normalized_type];
  return fn?fn(m):renderStructuredFallback(m);
}
// ---------------------------------------------------------------------------
// RND-206 — unified rich-media rendering: MediaAccessCache, the shared
// Viewer, and video/voice/file/emotion/composite (mixed/chatrecord)
// renderers. Every renderer here consumes already-normalized data (a
// TimelineMessageOut row, or a composite node's {type,text,fields,media,
// children} shape from structured_content.fields) -- never a raw backend
// payload, storage path, media_id, or sdkfileid.
// ---------------------------------------------------------------------------

// In-memory only (no localStorage/sessionStorage) media access descriptor
// cache, keyed by access-url. Reused by every lazy media renderer below
// (video/voice/file/emotion + nested/composite media + the Viewer) so a
// re-render (composite re-render, refresh poll) does not re-mint a Qiniu
// signed URL unnecessarily. Refreshes automatically once the cached
// descriptor's expires_at has passed (or is within 5s of expiring) --
// access_type="proxy" descriptors have no expiry and are cached forever
// for the page lifetime.
// RND-206 QA fix: malformed/unparseable expires_at must never be treated as
// "cache forever" -- it is treated as already-expired (non-cacheable), the
// opposite of the pre-fix behavior. Descriptor fetch failures carry a
// numeric `.status` (HTTP status) or `.network=true` so callers can
// classify the failure (401/403/404/network) instead of a single generic
// error bucket.
var MediaAccessCache=(function(){
  var store={};
  function isFresh(entry){
    if(!entry)return false;
    if(!entry.expires_at)return true;
    var expiresMs=Date.parse(entry.expires_at);
    if(isNaN(expiresMs))return false;
    return expiresMs-Date.now()>5000;
  }
  function get(accessUrl){
    if(!accessUrl){var e0=new Error('no access url');e0.status=0;return Promise.reject(e0);}
    var cached=store[accessUrl];
    if(isFresh(cached))return Promise.resolve(cached);
    return fetch(accessUrl,{credentials:'same-origin'}).then(function(r){
      if(!r.ok){var e=new Error('HTTP '+r.status);e.status=r.status;throw e;}
      return r.json();
    }).then(function(desc){
      store[accessUrl]=desc;
      return desc;
    }).catch(function(err){
      if(typeof err.status!=='number')err.network=true;
      throw err;
    });
  }
  function invalidate(accessUrl){delete store[accessUrl];}
  return {get:get,invalidate:invalidate};
})();

// Classifies a MediaAccessCache error into one of the required buckets
// (RND-206 QA fix #8): auth (401) / forbidden-or-expired (403) / missing
// (404) / network (no HTTP status at all, e.g. offline) / generic error.
function classifyMediaError(e){
  if(e&&e.status===401)return 'auth';
  if(e&&e.status===403)return 'forbidden';
  if(e&&e.status===404)return 'missing';
  if(e&&e.network)return 'network';
  return 'error';
}
function redirectToLogin(){
  if(typeof window!=='undefined'&&window.location)window.location.href='/admin/login';
}
// Controlled, bounded refresh: on a 403 (permission denied or an access
// grant that expired between mint and use) invalidate the cached
// descriptor and request exactly one fresh one. `retried` prevents any
// possibility of an infinite retry loop -- a second failure of any kind is
// surfaced to the caller as-is.
function fetchDescriptorWithRecovery(accessUrl,retried){
  return MediaAccessCache.get(accessUrl).catch(function(e){
    if(!retried&&e&&e.status===403){
      MediaAccessCache.invalidate(accessUrl);
      return fetchDescriptorWithRecovery(accessUrl,true);
    }
    if(e&&e.status===401)redirectToLogin();
    throw e;
  });
}

function fmtBytes(n){
  if(typeof n!=='number'||isNaN(n))return I18N.t('file.sizeUnknown');
  var units=['B','KB','MB','GB'];var i=0;var v=n;
  while(v>=1024&&i<units.length-1){v/=1024;i++;}
  return (i===0?String(v):v.toFixed(1))+' '+units[i];
}
// RND-206 QA fix #12: display the authorized descriptor's filename when the
// backend provides one (currently always null -- see NestedMediaAccessOut's
// docstring, a documented backend-contract gap, not fabricated client-side)
// with a localized fallback. Never derived from local_path/object_key/URL.
function fmtFileName(desc){
  if(desc&&typeof desc.filename==='string'&&desc.filename.trim())return desc.filename;
  return I18N.t('file.fallbackName');
}

// Registry of items the Viewer can page through for the CURRENT
// renderTimeline() pass -- reset at the top of renderTimeline(), populated
// as each image/emotion/video element is rendered so prev/next navigates
// every viewable item across the whole visible timeline, not just siblings
// within one message.
var timelineViewerItems=[];
function registerViewerItem(item){
  timelineViewerItems.push(item);
  return timelineViewerItems.length-1;
}

// ---------------------------------------------------------------------------
// Unified Viewer -- one shared overlay for image/emotion/video preview and
// chatrecord nested-message browsing, built once and reused for every
// message (never one popup per message type). Never touches the timeline
// DOM/scroll position behind it, and is completely independent of the 30s
// auto-refresh poller / renderTimeline() re-renders. role="dialog"/
// aria-modal (RND-206 QA fix #11) with focus moved in on open and restored
// to the triggering element (or a safe fallback) on close.
// ---------------------------------------------------------------------------
var viewerItems=[];
var viewerIndex=-1;
var viewerKeyHandlerBound=false;
// Bumped on every open/close so an in-flight descriptor fetch belonging to
// a viewer session that has since closed (or been reopened with different
// items) can never apply its result (RND-206 QA fix #5).
var viewerGen=0;
var viewerFocusTrigger=null;

function ensureViewerRoot(){
  var root=document.getElementById('rnd206-viewer');
  if(root)return root;
  root=document.createElement('div');
  root.id='rnd206-viewer';
  root.className='v-overlay';
  root.style.display='none';
  root.setAttribute('role','dialog');
  root.setAttribute('aria-modal','true');
  root.setAttribute('aria-label',I18N.t('viewer.dialogLabel'));
  root.innerHTML=
    '<div class="v-backdrop" onclick="closeViewer()"></div>'
    +'<button type="button" class="v-close" onclick="closeViewer()" aria-label="'+esc(I18N.t('viewer.close'))+'">&times;</button>'
    +'<button type="button" class="v-nav v-prev" onclick="viewerShow(viewerIndex-1)" style="display:none" aria-label="'+esc(I18N.t('viewer.prev'))+'">&#8249;</button>'
    +'<button type="button" class="v-nav v-next" onclick="viewerShow(viewerIndex+1)" style="display:none" aria-label="'+esc(I18N.t('viewer.next'))+'">&#8250;</button>'
    +'<div class="v-body" id="rnd206-viewer-body"></div>';
  document.body.appendChild(root);
  if(!viewerKeyHandlerBound){
    document.addEventListener('keydown',function(e){
      var root2=document.getElementById('rnd206-viewer');
      if(!root2||root2.style.display==='none')return;
      if(e.key==='Escape')closeViewer();
      else if(e.key==='ArrowLeft')viewerShow(viewerIndex-1);
      else if(e.key==='ArrowRight')viewerShow(viewerIndex+1);
    });
    viewerKeyHandlerBound=true;
  }
  return root;
}
// RND-206 QA fix #11/#15: re-applies I18N labels to the persistent viewer
// chrome (built once by ensureViewerRoot and never rebuilt otherwise) so a
// runtime locale switch updates them without a page reload. If the viewer
// is currently open, also re-renders the current item so any visible
// loading/error/action copy picks up the new locale immediately.
function refreshViewerLabels(){
  var root=typeof document!=='undefined'?document.getElementById('rnd206-viewer'):null;
  if(!root)return;
  root.setAttribute('aria-label',I18N.t('viewer.dialogLabel'));
  var closeBtn=root.querySelector('.v-close');
  if(closeBtn)closeBtn.setAttribute('aria-label',I18N.t('viewer.close'));
  var prevBtn=root.querySelector('.v-prev');
  if(prevBtn)prevBtn.setAttribute('aria-label',I18N.t('viewer.prev'));
  var nextBtn=root.querySelector('.v-next');
  if(nextBtn)nextBtn.setAttribute('aria-label',I18N.t('viewer.next'));
  if(root.style.display!=='none'&&viewerIndex>=0)viewerShow(viewerIndex);
}
function openViewer(items,startIndex){
  viewerItems=items||[];
  viewerGen++;
  viewerFocusTrigger=typeof document!=='undefined'?document.activeElement:null;
  var root=ensureViewerRoot();
  root.style.display='flex';
  if(typeof document!=='undefined')document.body.style.overflow='hidden';
  viewerShow(startIndex||0);
  var closeBtn=root.querySelector('.v-close');
  if(closeBtn&&typeof closeBtn.focus==='function')closeBtn.focus();
}
function restoreViewerFocus(){
  var trigger=viewerFocusTrigger;
  viewerFocusTrigger=null;
  if(trigger&&typeof trigger.focus==='function'&&typeof document!=='undefined'&&document.body
     &&typeof document.body.contains==='function'&&document.body.contains(trigger)){
    trigger.focus();
    return;
  }
  if(typeof document==='undefined')return;
  var fallback=document.getElementById('timeline-body');
  if(fallback&&typeof fallback.focus==='function')fallback.focus();
}
function closeViewer(){
  viewerGen++;
  var root=document.getElementById('rnd206-viewer');
  if(root)root.style.display='none';
  if(typeof document!=='undefined')document.body.style.overflow='';
  viewerItems=[];
  viewerIndex=-1;
  restoreViewerFocus();
}
function openChatrecordViewer(node,depth){
  openViewer([{kind:'chatrecord',node:node,depth:depth||0}],0);
}
function viewerShow(idx){
  if(!viewerItems.length||idx<0||idx>=viewerItems.length)return;
  viewerIndex=idx;
  var gen=viewerGen;
  var root=ensureViewerRoot();
  var body=document.getElementById('rnd206-viewer-body');
  var item=viewerItems[idx];
  var multi=viewerItems.length>1&&item.kind!=='chatrecord';
  root.querySelector('.v-prev').style.display=(multi&&idx>0)?'block':'none';
  root.querySelector('.v-next').style.display=(multi&&idx<viewerItems.length-1)?'block':'none';
  if(item.kind==='chatrecord'){
    body.innerHTML='<div class="v-chatrecord"><div class="v-chatrecord-title">'
      +esc((item.node.fields&&item.node.fields.title)||I18N.t('chatrecord.title'))+'</div>'
      +renderCompositeChildren(item.node,item.depth||0)+'</div>';
    // RND-206 QA fix #3: viewer-mounted content (nested media inside a
    // chatrecord's expanded view) was never hydrated -- this is the exact
    // "viewer content is inserted without running the required hydration
    // flow" defect. hydrateRichMedia is the SAME engine used for the
    // timeline itself, applied here too.
    hydrateRichMedia(body);
    return;
  }
  body.innerHTML='<div class="v-loading" role="status">'+esc(I18N.t('viewer.loading'))+'</div>';
  fetchDescriptorWithRecovery(item.accessUrl).then(function(desc){
    if(gen!==viewerGen||viewerIndex!==idx)return;
    if(item.kind==='video'){
      body.innerHTML='<video class="v-media" src="'+esc(desc.url)+'" controls playsinline></video>';
    }else{
      body.innerHTML='<img class="v-media" src="'+esc(desc.url)+'" alt="'+esc(item.label||'')+'">';
    }
  }).catch(function(e){
    if(gen!==viewerGen||viewerIndex!==idx)return;
    var errKind=classifyMediaError(e);
    var key=errKind==='auth'?'viewer.unauthorized':errKind==='forbidden'?'media.error.forbidden'
      :errKind==='missing'?'viewer.missingMedia':errKind==='network'?'media.error.network':'viewer.error';
    body.innerHTML='<div class="v-error" role="alert">'+esc(I18N.t(key))+'</div>';
  });
}

// ---------------------------------------------------------------------------
// Lazy rich-media hydration -- ONE shared engine (MediaAccessCache +
// data-rnd206-kind/data-rnd206-access-url) for every media kind
// (image/video/voice/file/emotion) in BOTH the top-level timeline and every
// nested mixed/chatrecord node (RND-206 QA fix #2/#3: no separate
// access-fetch logic duplicated inside the composite renderers -- they
// only ever emit a placeholder built by richMediaPlaceholder() below and
// this same hydrateRichMedia() call resolves it, wherever it was mounted).
// Called after every DOM insertion point that can contain one of these
// placeholders: renderTimeline() (top-level + inline mixed children) and
// viewerShow()'s chatrecord branch (nested chatrecord content).
// ---------------------------------------------------------------------------
function hydrateRichMedia(root){
  root.querySelectorAll('[data-rnd206-access-url]').forEach(function(el){loadRichMedia(el);});
}
function loadRichMedia(el){
  var accessUrl=el.getAttribute('data-rnd206-access-url');
  var kind=el.getAttribute('data-rnd206-kind');
  if(!accessUrl){showRichMediaError(el,kind,'missing');return;}
  fetchDescriptorWithRecovery(accessUrl).then(function(desc){
    swapRichMediaPlaceholder(el,kind,desc);
  }).catch(function(e){
    showRichMediaError(el,kind,classifyMediaError(e));
  });
}
function buildErrorBox(kind,errKind){
  var box=document.createElement('div');
  box.className='media-placeholder';
  box.setAttribute('role','alert');
  var key;
  if(errKind==='auth')key='viewer.unauthorized';
  else if(errKind==='forbidden')key='media.error.forbidden';
  else if(errKind==='missing')key='viewer.missingMedia';
  else if(errKind==='network')key='media.error.network';
  else key=kind==='video'?'video.playbackError':kind==='voice'?'voice.playbackError':kind==='file'?'file.unavailable':'media.loadFailed';
  box.textContent=I18N.t(key);
  return box;
}
function showRichMediaError(el,kind,errKind){
  var box=buildErrorBox(kind,errKind||'error');
  if(el.parentNode)el.parentNode.replaceChild(box,el);
  else if(el.tagName)el.textContent=box.textContent;
}
// RND-206 QA fix #8: a REAL playback/load failure of the mounted element
// (distinct from a descriptor-fetch failure, already handled by
// loadRichMedia/showRichMediaError above) invalidates the cached
// descriptor and retries exactly once, updating the SAME element in place
// (no DOM replacement, so an unaffected sibling video/audio never
// reloads). A second failure replaces `outerEl` (the element actually
// mounted in the timeline/viewer DOM -- may wrap `mediaEl`, e.g. an <img>
// inside a <button>) with a classified error box.
function handleRichMediaPlaybackFailure(kind,accessUrl,mediaEl,outerEl){
  if(mediaEl.getAttribute&&mediaEl.getAttribute('data-rnd206-retried')==='1'){
    var box=buildErrorBox(kind,'error');
    if(outerEl.parentNode)outerEl.parentNode.replaceChild(box,outerEl);
    return;
  }
  if(mediaEl.setAttribute)mediaEl.setAttribute('data-rnd206-retried','1');
  MediaAccessCache.invalidate(accessUrl);
  fetchDescriptorWithRecovery(accessUrl,true).then(function(desc){
    if(kind==='file'){mediaEl.href=desc.url;}else{mediaEl.src=desc.url;}
  }).catch(function(e){
    var box2=buildErrorBox(kind,classifyMediaError(e));
    if(outerEl.parentNode)outerEl.parentNode.replaceChild(box2,outerEl);
  });
}
function swapRichMediaPlaceholder(el,kind,desc){
  var accessUrl=el.getAttribute('data-rnd206-access-url');
  var viewerIdxAttr=el.getAttribute('data-rnd206-viewer-idx');
  var replacement;
  if(kind==='video'){
    var video=document.createElement('video');
    video.className='media-video';
    video.controls=true;
    video.preload='metadata';
    video.src=desc.url;
    if(viewerIdxAttr!==null){
      var vwrap=document.createElement('div');
      vwrap.className='media-video-wrap';
      video.onerror=function(){handleRichMediaPlaybackFailure('video',accessUrl,video,vwrap);};
      var expandBtn=document.createElement('button');
      expandBtn.type='button';
      expandBtn.className='media-video-expand';
      expandBtn.setAttribute('aria-label',I18N.t('viewer.expand'));
      expandBtn.textContent='⤢';
      expandBtn.onclick=(function(idx){return function(){openViewer(timelineViewerItems,idx);};})(parseInt(viewerIdxAttr,10));
      vwrap.appendChild(video);
      vwrap.appendChild(expandBtn);
      replacement=vwrap;
    }else{
      video.onerror=function(){handleRichMediaPlaybackFailure('video',accessUrl,video,video);};
      replacement=video;
    }
  }else if(kind==='voice'){
    var audio=document.createElement('audio');
    audio.className='media-audio';
    audio.controls=true;
    audio.preload='metadata';
    audio.src=desc.url;
    audio.onerror=function(){handleRichMediaPlaybackFailure('voice',accessUrl,audio,audio);};
    replacement=audio;
  }else if(kind==='file'){
    var link=document.createElement('a');
    link.className='file-card';
    link.href=desc.url;
    link.target='_blank';
    link.rel='noopener noreferrer';
    var icon=document.createElement('span');
    icon.className='file-card-icon';
    icon.setAttribute('aria-hidden','true');
    icon.textContent='📄';
    var info=document.createElement('span');
    info.className='file-card-info';
    var nameLine=document.createElement('div');
    nameLine.className='file-card-name';
    nameLine.textContent=fmtFileName(desc);
    var typeLine=document.createElement('div');
    typeLine.className='file-card-type';
    typeLine.textContent=desc.content_type||I18N.t('media.file');
    var metaLine=document.createElement('div');
    metaLine.className='file-card-meta';
    metaLine.textContent=fmtBytes(desc.size_bytes);
    info.appendChild(nameLine);
    info.appendChild(typeLine);
    info.appendChild(metaLine);
    var dl=document.createElement('span');
    dl.className='file-card-download';
    dl.textContent=I18N.t('file.download');
    link.appendChild(icon);
    link.appendChild(info);
    link.appendChild(dl);
    replacement=link;
  }else if(kind==='image'||kind==='emotion'){
    var img=document.createElement('img');
    img.className=kind==='emotion'?'emotion-preview':'media-preview';
    img.src=desc.url;
    img.alt=kind==='emotion'?I18N.t('emotion.alt'):I18N.t('media.image');
    img.loading='lazy';
    // RND-207: async decode keeps a large-ish thumbnail off the main thread;
    // the reserved w/h (from the placeholder, computed from intrinsic dims)
    // are carried onto the <img> so the swap-in causes no layout shift.
    img.decoding='async';
    var rw=el.getAttribute('data-rnd207-w'),rh=el.getAttribute('data-rnd207-h');
    if(rw&&rh){img.width=parseInt(rw,10);img.height=parseInt(rh,10);}
    if(viewerIdxAttr!==null){
      var btn=document.createElement('button');
      btn.type='button';
      btn.className='media-preview-btn';
      btn.setAttribute('aria-label',img.alt);
      img.onerror=function(){handleRichMediaPlaybackFailure(kind,accessUrl,img,btn);};
      btn.appendChild(img);
      btn.onclick=(function(idx){return function(){openViewer(timelineViewerItems,idx);};})(parseInt(viewerIdxAttr,10));
      replacement=btn;
    }else{
      img.onerror=function(){handleRichMediaPlaybackFailure(kind,accessUrl,img,img);};
      replacement=img;
    }
  }else{
    return;
  }
  if(el.parentNode)el.parentNode.replaceChild(replacement,el);
}
// One placeholder builder for every media kind (image/video/voice/file/
// emotion), top-level or nested -- the single "hydration contract": every
// consumer emits exactly this markup and nothing else, and hydrateRichMedia
// is the only code that ever resolves it into a live element.
function richMediaPlaceholder(kind,accessUrl,loadingKey,extraAttrs){
  var attrs='';
  if(extraAttrs){
    for(var k in extraAttrs){
      if(Object.prototype.hasOwnProperty.call(extraAttrs,k))attrs+=' '+k+'="'+esc(String(extraAttrs[k]))+'"';
    }
  }
  return '<div class="media-rich-loading" role="status" data-rnd206-kind="'+esc(kind)+'" data-rnd206-access-url="'+esc(accessUrl||'')+'"'+attrs+'>'
    +esc(I18N.t(loadingKey))+'</div>';
}
// Eagerly registers a Viewer slot at render time (not after the descriptor
// resolves) so top-level and nested image/emotion/video items behave
// identically and register in stable document order.
// opts (RND-207, optional): {thumbUrl,width,height} for image/emotion.
//  - The VIEWER always registers the ORIGINAL accessUrl (opened full-res).
//  - The LIST placeholder hydrates from thumbUrl when present (small
//    thumbnail), falling back to the original when there is no thumbnail.
//  - width/height (the original's intrinsic pixels) reserve an exact display
//    box on the placeholder so swapping the <img> in causes no layout shift.
function renderViewableMediaSlot(kind,accessUrl,label,loadingKey,opts){
  var idx=registerViewerItem({kind:kind==='emotion'?'image':kind,accessUrl:accessUrl,label:label});
  var extra={'data-rnd206-viewer-idx':idx};
  var hydrateUrl=accessUrl;
  if(opts){
    if(opts.thumbUrl)hydrateUrl=opts.thumbUrl;
    var w=parseInt(opts.width,10),h=parseInt(opts.height,10);
    if(w>0&&h>0){
      var cap=kind==='emotion'?150:280; // mirrors .emotion-preview / .media-preview CSS caps
      var scale=Math.min(cap/w,cap/h,1);
      var dw=Math.max(1,Math.round(w*scale)),dh=Math.max(1,Math.round(h*scale));
      extra['data-rnd207-w']=dw;extra['data-rnd207-h']=dh;
      extra['style']='width:'+dw+'px;height:'+dh+'px';
    }
  }
  return richMediaPlaceholder(kind,hydrateUrl,loadingKey,extra);
}
function renderVideoPreview(accessUrl){
  // RND-206 QA fix #7: video is registered with the same shared-viewer
  // dispatch as image/emotion (an explicit expand action alongside the
  // inline native-controls player -- see swapRichMediaPlaceholder's video
  // branch), not a separate independent video modal.
  return renderViewableMediaSlot('video',accessUrl,I18N.t('media.video'),'video.loading');
}
function renderVoicePreview(accessUrl){
  return richMediaPlaceholder('voice',accessUrl,'voice.loading');
}
function renderFilePreview(accessUrl){
  return richMediaPlaceholder('file',accessUrl,'console.loading');
}
function renderEmotionPreview(accessUrl,opts){
  return renderViewableMediaSlot('emotion',accessUrl,I18N.t('emotion.alt'),'viewer.loading',opts);
}
function renderNestedImageSlot(accessUrl,opts){
  return renderViewableMediaSlot('image',accessUrl,I18N.t('media.image'),'viewer.loading',opts);
}
// RND-207: pack the thumbnail/dimension hints a timeline message (m) or a
// nested media descriptor (media) carries into the opts renderViewableMediaSlot
// expects. Both shapes name the fields identically (thumbnail_access_url/
// image_width/image_height), so one helper serves both.
function thumbSlotOpts(src){
  return {thumbUrl:src&&src.thumbnail_access_url,width:src&&src.image_width,height:src&&src.image_height};
}
// RND-206 QA fix #2: registry-gated top-level dispatch for the three
// media-preview kinds whose element choice still needs a small kind->
// renderer map (same accepted pattern as STRUCTURED_CARD_RENDERERS) --
// used by renderMessageBody once app.message_type_registry reports
// renderer_strategy=="media_preview" for the message's type (see
// message_type_registry.py; the registry, not this list, is what marks a
// type as preview-capable).
var MEDIA_PREVIEW_KINDS={video:1,voice:1,file:1};
function renderMediaPreviewByKind(kind,accessUrl){
  if(kind==='video')return renderVideoPreview(accessUrl);
  if(kind==='voice')return renderVoicePreview(accessUrl);
  if(kind==='file')return renderFilePreview(accessUrl);
  return '';
}

// ---------------------------------------------------------------------------
// Composite (mixed/chatrecord) node rendering — RND-200's recursive
// structured_content.fields.items[...]/.children[...] shape, rendered
// in-order via the SAME renderer registry used for top-level messages
// (STRUCTURED_CARD_RENDERERS, renderVideoPreview/etc, nested media
// descriptors already enriched server-side to {status,media_type,
// mime_type,size_bytes,access_url} — see _enrich_nested_media_fields).
// Never displays raw JSON; an unrecognized child degrades to a safe,
// labeled fallback instead of breaking the whole message. Every node is
// rendered inside a try/catch (RND-206 QA fix #10) so one malformed child
// can never take down its siblings.
// ---------------------------------------------------------------------------
var COMPOSITE_MAX_DEPTH=8; // ONE documented maximum -- mirrors backend _MIXED_MAX_DEPTH, defense in depth only
var COMPOSITE_MEDIA_KINDS={image:1,video:1,voice:1,file:1,emotion:1};

function renderCompositeNodeMedia(node){
  var media=node.media;
  if(!media||typeof media!=='object')return '<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
  if(media.status!=='available'||!media.access_url){
    var label=MEDIA_LABELS[media.media_type]||I18N.t('media.generic');
    var statusLabel=MEDIA_STATUS_LABELS[media.status]||MEDIA_STATUS_LABELS.unsupported||I18N.t('media.status.unsupported');
    return '<div class="media-placeholder">'+esc(label)+' · '+esc(statusLabel)+'</div>';
  }
  var kind=node.type;
  if(kind==='image')return renderNestedImageSlot(media.access_url,thumbSlotOpts(media));
  if(kind==='emotion')return renderEmotionPreview(media.access_url,thumbSlotOpts(media));
  if(kind==='video')return renderVideoPreview(media.access_url);
  if(kind==='voice')return renderVoicePreview(media.access_url);
  if(kind==='file')return renderFilePreview(media.access_url);
  return '<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
}
function renderCompositeStructured(node){
  var fn=STRUCTURED_CARD_RENDERERS[node.type];
  var fake={structured_content:{fields:node.fields||null},normalized_type:node.type,display_label_key:'messageType.'+node.type};
  return fn?fn(fake):renderStructuredFallback(fake);
}
// Shared by BOTH the top-level chatrecord card (renderChatrecordMessage)
// and a chatrecord/mixed node nested inside another composite message
// (renderCompositeNode) -- ONE dispatch path, not two parallel
// implementations (RND-206 QA fix #2). data-depth carries the REAL
// recursion depth this card sits at so opening it in the Viewer resumes
// counting from there instead of silently restarting at 0 (QA fix #9). A
// semantic <button> (not a clickable <div>) so it is natively keyboard
// operable (QA fix #11) -- Enter/Space activation is free.
function renderChatrecordCard(node,depth,title,summaryText,count){
  return '<button type="button" class="chatrecord-card" data-depth="'+(depth||0)+'" data-node="'+esc(JSON.stringify(node))+'" '
    +'onclick="openChatrecordViewer(JSON.parse(this.getAttribute(&quot;data-node&quot;)),parseInt(this.getAttribute(&quot;data-depth&quot;),10))" '
    +'aria-haspopup="dialog">'
    +'<div class="chatrecord-card-title">'+esc(title)+'</div>'
    +(summaryText?'<div class="chatrecord-card-summary">'+esc(summaryText)+'</div>':'')
    +'<div class="chatrecord-card-count">'+esc(count+' '+I18N.t('chatrecord.itemsSuffix'))+'</div>'
    +'</button>';
}
function renderCompositeNode(node,depth){
  if(!node||typeof node!=='object')return '';
  if(depth>COMPOSITE_MAX_DEPTH)return '<div class="composite-unknown">'+esc(I18N.t('composite.depthLimitReached'))+'</div>';
  try{
    var metaBits=[];
    if(node.sender_name||node.sender)metaBits.push(esc(node.sender_name||node.sender));
    if(node.timestamp)metaBits.push(esc(fmtTime(node.timestamp)));
    var meta=metaBits.length?'<div class="composite-node-meta">'+metaBits.join(' · ')+'</div>':'';
    var body;
    if(node.type==='text'){
      body=node.text?'<div class="composite-node-text">'+esc(node.text)+'</div>'
        :'<div class="media-placeholder">'+esc(I18N.t('timeline.emptyText'))+'</div>';
    }else if(COMPOSITE_MEDIA_KINDS[node.type]){
      body=renderCompositeNodeMedia(node);
    }else if(STRUCTURED_CARD_RENDERERS[node.type]){
      body=renderCompositeStructured(node);
    }else if(node.type==='chatrecord'||node.type==='mixed'){
      body=renderChatrecordCard(node,depth,(node.fields&&node.fields.title)||I18N.t('chatrecord.title'),null,
        Array.isArray(node.children)?node.children.length:0);
    }else if(node.text){
      // Unknown/unsupported node type, but the parser still extracted a
      // best-effort text rendition (see structured_message_parser's
      // _extract_nested_text) -- show it rather than a bare "unsupported"
      // label, same spirit as the top-level unknown-type handling.
      body='<div class="composite-node-text">'+esc(node.text)+'</div>';
    }else{
      body='<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
    }
    return '<div class="composite-node">'+meta+body+'</div>';
  }catch(e){
    // RND-206 QA fix #10: this node's renderer threw -- isolate the
    // failure to this one node; siblings (already rendered, or rendered
    // next in the same forEach loop) are unaffected. Never surfaces the
    // exception message/stack or any node content.
    return '<div class="composite-node"><div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div></div>';
  }
}
function renderCompositeChildren(node,depth){
  var children=node&&(node.children||(node.fields&&node.fields.items));
  if(!Array.isArray(children)||!children.length){
    return '<div class="composite-unknown">'+esc(I18N.t('chatrecord.empty'))+'</div>';
  }
  var html='';
  children.forEach(function(child){
    html+='<div class="v-chatrecord-node">'+renderCompositeNode(child,(depth||0)+1)+'</div>';
  });
  return html;
}
// mixed: rendered inline, in order, recursively -- reusing the exact same
// per-node renderer as chatrecord's viewer (renderCompositeNode). Each
// item is independently failure-isolated by renderCompositeNode itself, so
// this loop never needs its own try/catch.
function renderMixedMessage(m){
  var fields=m.structured_content&&m.structured_content.fields;
  var items=fields&&fields.items;
  if(!Array.isArray(items)||!items.length){
    return '<div class="composite-unknown">'+esc(I18N.t('composite.unknownChild'))+'</div>';
  }
  var html='<div class="composite-wrap">';
  items.forEach(function(item){html+=renderCompositeNode(item,1);});
  html+='</div>';
  return html;
}
// chatrecord: a compact summary card in the timeline (depth 0 -- this is
// the recursion root); full nested content (sender/timestamp/nested media/
// nested chatrecord, all reusing renderCompositeNode) opens in the shared
// Viewer on click, via the exact same renderChatrecordCard() a nested
// chatrecord/mixed node uses.
function renderChatrecordMessage(m){
  var fields=m.structured_content&&m.structured_content.fields;
  var items=(fields&&fields.items)||[];
  var title=(fields&&fields.title)||I18N.t('chatrecord.title');
  var firstText='';
  for(var i=0;i<items.length&&!firstText;i++){
    if(items[i]&&items[i].text)firstText=items[i].text;
  }
  var node={fields:fields,children:items};
  return renderChatrecordCard(node,0,title,firstText,items.length);
}
function renderCompositeMessage(m){
  if(m.normalized_type==='chatrecord')return renderChatrecordMessage(m);
  if(m.normalized_type==='mixed')return renderMixedMessage(m);
  var typeEntry=MessageTypeRegistry.resolvePlaceholder(m.normalized_type)||MessageTypeRegistry.fallback;
  return '<div class="media-placeholder">'+I18N.t(typeEntry.placeholderKey)+'</div>';
}

function renderRevokePlaceholder(m){
  // RND-201: a standalone "revoke" event row that could not be linked to
  // its original (target not archived yet, or the event's own payload
  // was malformed) — the only content ever shown here is a stable,
  // i18n-driven status label; the original message's content is never
  // fabricated or guessed. A LINKED revoke event never reaches this
  // function — it is folded into the original message, which renders
  // through its own normal path below with an "already revoked" badge
  // instead (see renderTimeline's revokedBadge).
  var key='revoke.pending';
  if(m.revoke_association_status==='original_missing')key='revoke.originalMissing';
  else if(m.revoke_association_status==='malformed')key='revoke.malformed';
  // RND-206 QA fix #13: consumes the existing RND-201 revoked_at field
  // (already present on TimelineMessageOut for a standalone revoke-event
  // row -- see app.routers.conversations) when available; never fabricates
  // a time when it is absent, and never exposes revoke_event_msgid or any
  // other internal association id here.
  var timeLine=m.revoked_at?'<div class="revoke-time">'+esc(I18N.t('revoke.time'))+esc(fmtTime(m.revoked_at))+'</div>':'';
  return '<div class="media-placeholder">'+esc(I18N.t(key))+'</div>'+timeLine;
}
function renderMessageBody(m){
  if(m.revoke_association_status&&m.revoke_association_status!=='linked'){
    return renderRevokePlaceholder(m);
  }
  var mediaType=m.media_type||'text';
  if(mediaType==='text'){
    return m.content_text?esc(m.content_text):'<div class="media-placeholder">'+I18N.t('timeline.emptyText')+'</div>';
  }
  if(mediaType==='image'&&m.media_status==='available'&&m.media_access_url){
    // RND-206 QA fix (narrow remediation pass): top-level image now shares
    // the exact same hydration path as nested/video/voice/file/emotion
    // (renderViewableMediaSlot -> richMediaPlaceholder -> hydrateRichMedia
    // -> loadRichMedia -> fetchDescriptorWithRecovery -> MediaAccessCache
    // -> swapRichMediaPlaceholder) instead of the now-removed standalone
    // RND-187 loadMediaImage chain. No src/href is set here — the actual
    // URL (a short-lived Qiniu signed URL, or the local proxy path) is
    // only known once the descriptor is fetched, on demand, post-render.
    // Gated on media_access_url alone (no media_url fallback): the
    // backend always sets both together whenever media_status=="available"
    // (see app.routers.conversations.get_conversation_messages), the same
    // invariant video/voice/file already rely on.
    return renderViewableMediaSlot('image', m.media_access_url, I18N.t('media.image'), 'viewer.loading', thumbSlotOpts(m));
  }
  if(mediaType==='image'){
    var imgLabel=MEDIA_LABELS.image||I18N.t('media.generic');
    var imgStatusLabel=MEDIA_STATUS_LABELS[m.media_status]||I18N.t('media.status.unsupported');
    return '<div class="media-placeholder">'+esc(imgLabel)+' · '+esc(imgStatusLabel)+'</div>';
  }
  // RND-206 QA fix #2: video/voice/file are gated by renderer_strategy==
  // "media_preview" -- the authoritative Message Type Registry's own
  // capability signal (message_type_registry.py), not a hardcoded
  // mediaType literal list maintained independently of it. media_status/
  // media_access_url remain the separate, per-message "is THIS message's
  // media actually available right now" check. Lazily hydrated the same
  // way image is (see hydrateRichMedia/loadRichMedia below); falls through
  // to the shared "known type, not available" placeholder when
  // media_status isn't "available" so a not-yet-downloaded / failed video
  // still shows a clear, type-specific status instead of a broken player.
  if(m.renderer_strategy==='media_preview'&&MEDIA_PREVIEW_KINDS[mediaType]){
    if(m.media_status==='available'&&m.media_access_url)return renderMediaPreviewByKind(mediaType,m.media_access_url);
    var mLabel=MEDIA_LABELS[mediaType]||I18N.t('media.generic');
    var mStatusLabel=MEDIA_STATUS_LABELS[m.media_status]||I18N.t('media.status.unsupported');
    return '<div class="media-placeholder">'+esc(mLabel)+' · '+esc(mStatusLabel)+'</div>';
  }
  // RND-206 QA fix #2: emotion (sticker/GIF) preview, gated the same way --
  // renderer_strategy=="media_preview" is the registry's capability signal
  // for emotion too (message_type_registry.py). classify_media()
  // intentionally still reports media_type/media_status "unsupported" for
  // emotion (see app.media_classification), so this cannot be gated by
  // mediaType/media_status like video/voice/file above -- media_access_url
  // presence is the per-message availability signal instead. When no
  // access URL is available this falls through unchanged to the normal
  // registry-driven "unsupported" placeholder path below (same as every
  // other still-unsupported type), rather than a separate hand-written
  // fallback string.
  if(m.normalized_type==='emotion'&&m.renderer_strategy==='media_preview'&&m.media_access_url){
    return renderEmotionPreview(m.media_access_url,thumbSlotOpts(m));
  }
  if(mediaType==='unknown'){
    return '<div class="media-placeholder">'+I18N.t('media.unknownType')+'</div>';
  }
  // RND-206: mixed/chatrecord composite viewer.
  if(m.renderer_strategy==='composite_view'){
    return renderCompositeMessage(m);
  }
  if(m.renderer_strategy==='structured_card'){
    return renderStructuredCard(m);
  }
  // RND-198: system events rendered as centered non-bubble cards.
  if(m.renderer_strategy==='system_card'){
    return renderSystemCard(m);
  }
  // RND-197 fix: keyed by normalized_type, not raw msgtype —
  // MessageTypeRegistry.entries is keyed by normalized_type (e.g.
  // "miniprogram"), but msgtype is the raw wire value (e.g. "weapp").
  // Looking this up by m.msgtype silently missed every aliased type and
  // fell through to the generic fallback (see
  // test_message_type_registry_core.py's documented-divergence test, now
  // fixed). normalized_type is available on every message row, not just
  // structured ones, so this is safe unconditionally.
  //
  // RND-206 QA fix: this terminal fallback now reads MessageTypeRegistry.
  // resolve() (every registered entry) instead of the narrower
  // resolvePlaceholder() (only category=="placeholder" entries) so a
  // message that reaches here in an unexpected shape (e.g. a stale/missing
  // renderer_strategy on old cached data) still gets its own type's
  // specific label via .placeholderKey when the registry carries one
  // (video/voice/file/emotion still do, even though their normal dispatch
  // above never reaches this line) instead of collapsing into the generic
  // "unknown message type" text.
  var typeEntry=MessageTypeRegistry.resolve(m.normalized_type);
  var placeholderKey=(typeEntry&&typeEntry.placeholderKey)||MessageTypeRegistry.fallback.placeholderKey;
  return '<div class="media-placeholder">'+I18N.t(placeholderKey)+'</div>';
}
// RND-206 QA fix: per-message failure isolation -- a single message whose
// renderer throws (malformed structured_content, unexpected field shape,
// etc.) must never blank the rest of the timeline. Never logs message
// content/signed URLs, even in the caught-error path (dev diagnostics
// requirement) -- the error itself is discarded.
function safeRenderMessageBody(m){
  try{
    return renderMessageBody(m);
  }catch(e){
    return '<div class="media-placeholder">'+esc(I18N.t('render.messageFailed'))+'</div>';
  }
}
// RND-206 QA fix (narrow remediation pass): the standalone RND-187 image
// hydration chain (loadMediaImage/onMediaImageError/showMediaError/
// hydrateMediaImages) has been retired. Top-level images now render as a
// normal viewable media slot (renderViewableMediaSlot, same as nested/
// video/voice/file/emotion) and are hydrated exclusively by the shared
// hydrateRichMedia -> loadRichMedia -> fetchDescriptorWithRecovery ->
// swapRichMediaPlaceholder chain above, so top-level and nested images now
// share one descriptor cache, one retry policy, and one error
// classification (401/403/404/network) instead of two independent
// systems. See test_rnd_206_top_level_image.py for the full behavioral
// coverage this replaces (the old test_media_hydration.py, which tested
// the now-removed standalone chain by name, has been retired with it).
// RND-206 QA fix: a lightweight content signature used only to decide
// whether an auto-refresh's freshly-merged array is semantically identical
// to what is already painted, so refreshTimelineIfSelected() can skip
// touching the DOM entirely when nothing changed. RND-204 later built the
// incremental node-by-node reconcile (applyTimelineRefresh) that reuses this
// same signature per row to rebuild only the rows that actually changed.
function timelineSignature(msgs){
  return JSON.stringify((msgs||[]).map(function(m){
    // RND-204: the signature MUST cover every field timelineRowHtml(m)
    // actually renders, otherwise a change to one of them (e.g. a
    // backfilled sender/recipient display name, or a corrected msgtype)
    // would be misread as "unchanged" and the row would keep stale content.
    // Fields below map 1:1 to what the row emits: msgtype badge, sender
    // (self/staff class + name/raw fallbacks), roomid (group badge +
    // recipient line), recipient names, plus the body/media/revoke state.
    // Recipients are folded to their RENDERED shape, not the raw arrays: a
    // group row shows only a participant COUNT (RND-150 -- raw participant
    // ids are never rendered inline), a direct row shows the names. Signing
    // the rendered shape keeps raw group ids out of the data-msgsig
    // attribute (which is serialized into the DOM) while still detecting
    // recipient changes that actually alter what is painted.
    var rcptNames=(m.recipient_display_names&&m.recipient_display_names.length)?m.recipient_display_names:(m.recipients||[]);
    var rcptSig=m.roomid?rcptNames.length:rcptNames;
    return [
      m.msgid,m.msgtime,m.msgtype,m.sender,m.roomid,
      m.sender_display_name,m.sender_raw_id,rcptSig,
      m.content_text,m.media_status,m.media_access_url,
      // RND-207: stable endpoint path + intrinsic dims, NOT the signed URL
      // (which still never enters the signature). Included so a backfilled
      // thumbnail becoming available between refreshes re-renders the row to
      // use it; unchanged rows keep an identical signature and are untouched.
      m.thumbnail_access_url,m.image_width,m.image_height,
      m.is_revoked,m.revoked_at,m.revoke_association_status,
      m.structured_content?JSON.stringify(m.structured_content):null
    ];
  }));
}
// RND-204: single-message row builder, extracted from renderTimeline()'s
// loop so BOTH the full render and the incremental background-refresh
// updater (applyTimelineRefresh) emit byte-identical markup. Every row
// carries a stable data-msgid key and a per-message data-msgsig (the same
// content signature timelineSignature uses, scoped to one message) so the
// incremental updater can tell, per row, whether anything actually changed
// and skip rebuilding — and therefore re-requesting the media of — rows
// that did not. All prior contracts (RND-149 time, RND-150 group recipient
// overflow, RND-201 revoke badge, direction logic) live here unchanged.
function timelineRowHtml(m){
  var isSelf=mode==='staff'&&selEntityId&&m.sender===selEntityId;
  var isStaff=m.sender&&m.sender.indexOf('staff_')===0;
  var rowCls='tl-row '+(isSelf?'tl-row-self':'tl-row-other');
  var sc='tl-sender'+(isStaff?' tl-staff':'');
  var bc='tl-bubble '+(isSelf?'tl-bubble-self':(mode==='staff'?'tl-bubble-other':(isStaff?'tl-bubble-staff':'')));
  var text=safeRenderMessageBody(m);
  var mt=(m.msgtype&&m.msgtype!=='text')?' <span class="badge badge-count" style="font-size:.67rem">'+esc(m.msgtype)+'</span>':'';
  var grp=m.roomid?' <span class="badge badge-group" style="font-size:.65rem">'+esc(I18N.t('timeline.groupBadge'))+'</span>':'';
  // RND-201: secondary "已撤回" indicator on an original message that a
  // linked revoke event targets — deliberately visually secondary (a
  // small badge next to the existing type/group badges), never
  // replacing the message body rendered by renderMessageBody(m) above.
  var revokedBadge=m.is_revoked?' <span class="badge badge-revoked" style="font-size:.65rem">'+esc(I18N.t('timeline.revokedBadge'))
    // RND-206 QA fix #13: revoke time alongside the existing badge, using
    // the already-present revoked_at field -- omitted (never fabricated)
    // when absent. Original message content above is unchanged.
    +(m.revoked_at?' · '+esc(fmtTime(m.revoked_at)):'')+'</span>':'';
  var senderName=m.sender_display_name||m.sender||'?';
  var senderRaw=m.sender_raw_id||m.sender;
  var senderSecondary=(senderRaw&&senderRaw!==senderName)?' <span class="tl-sender-raw">('+esc(senderRaw)+')</span>':'';
  var rcptNames=(m.recipient_display_names&&m.recipient_display_names.length)?m.recipient_display_names:(m.recipients||[]);
  var rcpt='';
  if(m.roomid){
    if(rcptNames.length){
      rcpt='<div class="tl-rcpt">'+I18N.t('timeline.groupChat')+' · '+rcptNames.length+' '+(rcptNames.length===1?I18N.t('timeline.participant'):I18N.t('timeline.participants'))+'</div>';
    }
  }else if(rcptNames.length){
    rcpt='<div class="tl-rcpt">→ '+esc(rcptNames.join(', '))+'</div>';
  }
  return '<div class="'+rowCls+'" data-msgid="'+esc(m.msgid)+'" data-msgsig="'+esc(timelineSignature([m]))+'">'
    +'<div class="tl-meta"><span class="'+sc+'">'+esc(senderName)+'</span>'+senderSecondary
    +' <span class="tl-time">'+esc(fmtTime(m.msgtime))+'</span>'+mt+grp+revokedBadge+'</div>'
    +'<div class="'+bc+'">'+text+'</div>'
    +rcpt+'</div>';
}
function renderTimeline(scrollToBottom){
  var body=document.getElementById('timeline-body');
  timelineViewerItems=[];
  if(!timelineMsgs||!timelineMsgs.length){
    body.innerHTML='<div class="empty-state">'+I18N.t('console.noMessages')+'</div>';
    lastRenderedTimelineSignature=timelineSignature(timelineMsgs);
    return;
  }
  var pendingHistoryError=(typeof timelineHistoryError!=='undefined')&&timelineHistoryError;
  var html='<div id="timeline-history-status">'
    +(pendingHistoryError?historyRetryHtml():(timelineHasOlder?'':'<div class="history-status history-end">'+I18N.t('history.noMore')+'</div>'))
    +'</div>';
  html+='<div id="timeline-top-sentinel"></div>';
  html+='<div class="timeline">';
  timelineMsgs.forEach(function(m){
    html+=timelineRowHtml(m);
  });
  html+='</div>';
  body.innerHTML=html;
  hydrateRichMedia(body);
  lastRenderedTimelineSignature=timelineSignature(timelineMsgs);
  if(scrollToBottom){body.scrollTop=body.scrollHeight;}
}
function isNearBottom(){
  var body=document.getElementById('timeline-body');
  if(!body)return true;
  return (body.scrollHeight-body.scrollTop-body.clientHeight)<80;
}
function scrollTimelineToBottom(){
  var body=document.getElementById('timeline-body');
  if(body)body.scrollTop=body.scrollHeight;
  hideNewMessageIndicator();
}
function showNewMessageIndicator(){
  var el=document.getElementById('new-msg-indicator');
  if(el)el.style.display='block';
}
function hideNewMessageIndicator(){
  var el=document.getElementById('new-msg-indicator');
  if(el)el.style.display='none';
}
function mergeMessagesByMsgid(existing,incoming){
  var map={},order=[];
  (existing||[]).forEach(function(m){
    if(!Object.prototype.hasOwnProperty.call(map,m.msgid))order.push(m.msgid);
    map[m.msgid]=m;
  });
  (incoming||[]).forEach(function(m){
    if(!Object.prototype.hasOwnProperty.call(map,m.msgid))order.push(m.msgid);
    map[m.msgid]=m;
  });
  var merged=order.map(function(id){return map[id];});
  merged.sort(function(a,b){return(a.msgtime||0)-(b.msgtime||0);});
  return merged;
}
function refreshEntityList(){
  // RND-204: capture-at-call-time mode guard. refreshInFlight only prevents
  // overlapping refresh *cycles*; it does NOT stop a slow response from this
  // cycle applying after the user has switched mode (staff<->contact) while
  // the request was in flight. Mirror the timeline path's convId/gen guard so
  // a stale entity-list response can never overwrite the list the user has
  // since switched to.
  var reqMode=mode;
  var url=mode==='staff'?'/api/monitored-accounts':'/api/contacts';
  return fetch(url).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(items){
    // RND-204: only re-render the entity list when its content actually
    // changed since the last paint -- an unchanged background refresh must
    // not rebuild the whole list (avoids jitter / losing the active row).
    if(!items)return;
    if(mode!==reqMode)return; // stale: user switched mode mid-flight
    if(entityListSignature(items)===lastEntitySig)return;
    renderEntityList(items);
  });
}
function refreshConversationList(){
  if(!selEntityId)return Promise.resolve();
  // RND-204: capture BOTH the mode and the selected entity at request time.
  // A slow /api/conversations response must not repaint the list after the
  // user has switched to a different staff/contact (or flipped mode) -- the
  // convId/gen guard already protects the timeline; this protects the list.
  var reqMode=mode, reqEntityId=selEntityId;
  var url=mode==='staff'
    ?'/api/conversations?mode=staff&staff_id='+encodeURIComponent(selEntityId)
    :'/api/conversations?mode=contact&contact_id='+encodeURIComponent(selEntityId);
  return fetch(url).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(convs){
    // RND-204: skip the full conversation-list rebuild on an unchanged
    // background refresh (only re-render when a conversation's latest
    // message, ordering, or count actually changed).
    if(!convs)return;
    // stale: the selection this response was scoped to is no longer active.
    if(mode!==reqMode||selEntityId!==reqEntityId)return;
    if(convListSignature(convs)===lastConvSig)return;
    renderConvList(convs);
  });
}
// RND-204: build a single detached timeline row node from the same markup
// contract renderTimeline() uses (timelineRowHtml), so the incremental
// updater and the full render stay byte-identical per row.
function buildTimelineRowNode(m){
  var tmp=document.createElement('div');
  tmp.innerHTML=timelineRowHtml(m);
  return tmp.firstChild;
}
// RND-204: keep the history-status node (top of the timeline body) in sync
// during an incremental refresh without touching any message/media DOM.
function syncHistoryStatus(){
  var el=historyStatusEl();
  if(!el)return;
  if((typeof timelineHistoryError!=='undefined')&&timelineHistoryError){el.innerHTML=historyRetryHtml();}
  else if(!timelineHasOlder){el.innerHTML='<div class="history-status history-end">'+I18N.t('history.noMore')+'</div>';}
  else{el.innerHTML='';}
}
// RND-204: shared post-refresh scroll behaviour -- stick to the bottom when
// the user was already near it, otherwise preserve the exact scroll offset
// and surface the "new messages" pill instead of yanking them down.
function applyRefreshScroll(prevScrollTop,wasNearBottom,hasNew){
  var body=document.getElementById('timeline-body');
  if(!body)return;
  if(wasNearBottom){body.scrollTop=body.scrollHeight;hideNewMessageIndicator();}
  else{body.scrollTop=prevScrollTop;if(hasNew)showNewMessageIndicator();}
}
// RND-204: incremental timeline reconcile. Instead of replacing the whole
// timeline innerHTML (which destroyed every already-hydrated <img>/<video>
// -- forcing a re-request/flicker -- and reset scroll), this diffs the
// existing rows against timelineMsgs by stable data-msgid. Rows whose
// data-msgsig is unchanged are left completely untouched (their live media
// DOM and load state survive); only new/changed rows are built and only
// those get hydrateRichMedia(). Falls back to a full renderTimeline() when
// the live .timeline node can't be located (e.g. minimal test DOM) or a
// history error must be shown.
function applyTimelineRefresh(prevScrollTop,wasNearBottom,hasNew){
  var body=document.getElementById('timeline-body');
  if(!body)return;
  var timelineEl=body.querySelector?body.querySelector('.timeline'):null;
  if(!timelineEl||((typeof timelineHistoryError!=='undefined')&&timelineHistoryError)){
    renderTimeline(false);
    startHistoryObserver();
    applyRefreshScroll(prevScrollTop,wasNearBottom,hasNew);
    return;
  }
  var rows=timelineEl.querySelectorAll('[data-msgid]');
  var existing={},seen={};
  for(var i=0;i<rows.length;i++){existing[rows[i].getAttribute('data-msgid')]=rows[i];}
  var cursor=null;
  timelineMsgs.forEach(function(m){
    seen[m.msgid]=true;
    var row=existing[m.msgid];
    var sig=timelineSignature([m]);
    if(row&&row.getAttribute('data-msgsig')===sig){cursor=row;return;}
    var newRow=buildTimelineRowNode(m);
    if(row){timelineEl.replaceChild(newRow,row);}
    else if(cursor&&cursor.nextSibling){timelineEl.insertBefore(newRow,cursor.nextSibling);}
    else if(cursor){timelineEl.appendChild(newRow);}
    else{timelineEl.insertBefore(newRow,timelineEl.firstChild);}
    hydrateRichMedia(newRow);
    cursor=newRow;
  });
  for(var j=0;j<rows.length;j++){
    if(!seen[rows[j].getAttribute('data-msgid')]&&rows[j].parentNode)rows[j].parentNode.removeChild(rows[j]);
  }
  lastRenderedTimelineSignature=timelineSignature(timelineMsgs);
  syncHistoryStatus();
  applyRefreshScroll(prevScrollTop,wasNearBottom,hasNew);
}
function refreshTimelineIfSelected(){
  if(!timelineConvId||timelineLoadingOlder)return Promise.resolve();
  var convId=timelineConvId, gen=timelineRequestGen;
  var body=document.getElementById('timeline-body');
  var wasNearBottom=isNearBottom();
  var prevScrollTop=body?body.scrollTop:0;
  var url='/api/conversations/'+encodeURIComponent(convId)+'/messages?limit=20'+timelineEntityQueryParams();
  return fetch(url).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(data){
    // RND-206 QA fix: re-verify BOTH the selected conversation and the
    // generation token before applying -- a plain convId check alone is
    // insufficient because a user can switch away and back to the SAME
    // conversation while this request is still in flight, which would let
    // a genuinely stale response through a convId-only guard.
    if(!data||timelineConvId!==convId||timelineRequestGen!==gen||timelineLoadingOlder)return;
    var existingIds={};
    timelineMsgs.forEach(function(m){existingIds[m.msgid]=true;});
    var hasNew=data.messages.some(function(m){return!existingIds[m.msgid];});
    var merged=mergeMessagesByMsgid(timelineMsgs,data.messages);
    // RND-206 QA fix: an auto-refresh that produces a byte-identical
    // rendered set (same messages, same content/media/revoke state) must
    // not rebuild the timeline DOM -- doing so was destroying active
    // <video>/<audio> playback (currentTime reset to 0, paused, and the
    // browser re-requesting media bytes) even though nothing changed.
    var unchanged=lastRenderedTimelineSignature!==null&&timelineSignature(merged)===lastRenderedTimelineSignature;
    timelineMsgs=merged;
    // RND-204: an unchanged refresh must not touch the timeline DOM at all
    // (no re-render, no media re-request, no scroll jump).
    if(unchanged)return;
    // RND-204: apply only the rows that actually changed, preserving every
    // untouched message/media node and the current scroll position.
    applyTimelineRefresh(prevScrollTop,wasNearBottom,hasNew);
  });
}
function _refreshErrMsg(e){return(e&&e.message)?e.message:'refresh failed';}
function setRefreshError(msg){
  refreshErrorText=msg;
  updateRefreshStatus();
}
function updateRefreshStatus(){
  var el=document.getElementById('refresh-status');
  if(!el)return;
  var lastStr=lastRefreshAt?fmtTime(lastRefreshAt):'—';
  var html=I18N.t('refresh.lastUpdated')+esc(lastStr);
  if(typeof document!=='undefined'&&document.hidden){
    html+=' · '+I18N.t('refresh.paused');
  }else{
    html+=' · '+I18N.t('refresh.nextIn')+Math.max(refreshCountdownSec,0)+I18N.t('refresh.secondsSuffix');
  }
  if(refreshErrorText){
    html+=' · <span class="refresh-status-error">'+I18N.t('refresh.failedPrefix')+esc(refreshErrorText)+'</span>';
  }
  el.innerHTML=html;
}
function scheduleNextRefresh(){
  refreshCountdownSec=REFRESH_INTERVAL_SEC;
  updateRefreshStatus();
}
function refreshNow(reason){
  if(refreshInFlight)return;
  refreshInFlight=true;
  setRefreshError(null);
  var tasks=[refreshEntityList().catch(function(e){setRefreshError(_refreshErrMsg(e));})];
  if(selEntityId)tasks.push(refreshConversationList().catch(function(e){setRefreshError(_refreshErrMsg(e));}));
  if(timelineConvId)tasks.push(refreshTimelineIfSelected().catch(function(e){setRefreshError(_refreshErrMsg(e));}));
  Promise.all(tasks).then(function(){
    refreshInFlight=false;
    lastRefreshAt=Date.now();
    scheduleNextRefresh();
  });
}
function tickRefreshCountdown(){
  if(document.hidden)return;
  refreshCountdownSec--;
  if(refreshCountdownSec<=0){
    refreshNow('interval');
  }else{
    updateRefreshStatus();
  }
}
function startAutoRefresh(){
  if(refreshTickTimer)clearInterval(refreshTickTimer);
  refreshTickTimer=setInterval(tickRefreshCountdown,1000);
  document.addEventListener('visibilitychange',function(){
    if(document.hidden){
      updateRefreshStatus();
    }else{
      refreshNow('visibility');
    }
  });
}
applyStaticI18n();
loadCurrentUser();
setMode('staff');
lastRefreshAt=Date.now();
updateRefreshStatus();
startAutoRefresh();

// RND-159: Search
var searchTimer=null,searchLastQ='';
function highlightKeyword(text,keyword){
  if(!keyword)return text;
  var re=new RegExp('('+keyword.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\$&')+')','gi');
  return text.replace(re,'<span class="sr-highlight">$1</span>');
}
function doSearch(){
  var input=document.getElementById('search-input');
  var results=document.getElementById('search-results');
  var q=input.value.trim();
  if(q===searchLastQ)return;
  searchLastQ=q;
  if(!q){results.style.display='none';return;}
  results.innerHTML='<div class="sr-loading">'+I18N.t('search.loading')+'</div>';
  results.style.display='block';
  setTimeout(function(){results.style.maxHeight='';},50);
  var contactUrl='/api/search/contacts?q='+encodeURIComponent(q)+'&limit=5';
  var msgUrl='/api/search/messages?q='+encodeURIComponent(q)+'&limit=10';
  var contactDone=false,msgDone=false,contactError=false,msgError=false;
  var contactData=null,msgData=null;
  function renderResults(){
    if(!contactDone||!msgDone)return;
    if(contactError&&msgError){
      results.innerHTML='<div class="sr-error">'+I18N.t('search.error')+'</div>';
      return;
    }
    if((!contactData||!contactData.length)&&(!msgData||!msgData.results||!msgData.results.length)){
      results.innerHTML='<div class="sr-empty">'+I18N.t('search.noResults')+'</div>';
      return;
    }
    var html='';
    if(contactData&&contactData.length){
      html+='<div class="sr-section"><div class="sr-section-header">'+I18N.t('search.contacts')+' ('+contactData.length+')</div>';
      contactData.forEach(function(c){
        html+='<div class="sr-item" data-search-contact="'+esc(c.wecom_userid)+'"><div class="sr-item-main">'
          +'<span class="sr-item-name">'+esc(c.display_name)+'</span>'
          +'<span class="sr-item-raw">'+esc(c.wecom_userid)+'</span>'
          +'</div></div>';
      });
      html+='</div>';
    }
    if(contactData&&contactData.length&&msgData&&msgData.results&&msgData.results.length){
      html+='<div class="sr-divider"></div>';
    }
    if(msgData&&msgData.results&&msgData.results.length){
      html+='<div class="sr-section"><div class="sr-section-header">'+I18N.t('search.messages')+' ('+msgData.results.length+')</div>';
      msgData.results.forEach(function(m){
        var sender=esc(m.sender_display_name);
        var conv=esc(m.conversation_name);
        var snippet=highlightKeyword(esc(m.content_snippet),q);
        var t=m.msgtime?fmtTime(m.msgtime):'';
        html+='<div class="sr-item"'
          +' data-search-msg="'+esc(m.conversation_id)+'"'
          +' data-search-msg-type="'+esc(m.conversation_type)+'"'
          +' data-search-msg-sid="'+esc(m.entity_id||'')+'"'
          +' data-search-msg-stype="'+esc(m.entity_type||'')+'">'
          +'<div class="sr-item-main"><span class="sr-item-name">'+sender+'</span><span class="sr-item-conv">'+conv+'</span><span class="sr-item-time">'+t+'</span></div>'
          +'<div class="sr-item-snippet">'+snippet+'</div>'
          +'</div>';
      });
      html+='</div>';
    }
    results.innerHTML=html;
    attachSearchItemEvents();
  }
  contactDone=false;msgDone=false;contactError=false;msgError=false;
  contactData=null;msgData=null;
  fetch(contactUrl).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){
    contactData=d;contactDone=true;renderResults();
  }).catch(function(){
    contactError=true;contactDone=true;renderResults();
  });
  fetch(msgUrl).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){
    msgData=d;msgDone=true;renderResults();
  }).catch(function(){
    msgError=true;msgDone=true;renderResults();
  });
}
function attachSearchItemEvents(){
  document.querySelectorAll('[data-search-contact]').forEach(function(el){
    el.removeEventListener('click',onSearchContactItemClick);
    el.addEventListener('click',onSearchContactItemClick);
  });
  document.querySelectorAll('[data-search-msg]').forEach(function(el){
    el.removeEventListener('click',onSearchMsgItemClick);
    el.addEventListener('click',onSearchMsgItemClick);
  });
}
function onSearchContactItemClick(){
  var wecomUserId=this.dataset.searchContact;
  if(!wecomUserId)return;
  document.getElementById('search-input').value='';
  document.getElementById('search-results').style.display='none';
  searchLastQ='';
  setMode('staff');
  var foundInStaff=false;
  if(lastEntityItems){
    lastEntityItems.forEach(function(it){
      if(it.staff_id===wecomUserId||it.monitored_account_id===wecomUserId)foundInStaff=true;
    });
  }
  if(foundInStaff){
    var entityBody=document.getElementById('entity-body');
    var els=entityBody.querySelectorAll('.entity-item');
    els.forEach(function(el){
      if(el.dataset.id===wecomUserId)onEntityClick(el);
    });
  }else{
    setMode('contact');
    var checkInterval=setInterval(function(){
      var entityBody=document.getElementById('entity-body');
      var els=entityBody.querySelectorAll('.entity-item');
      var found=false;
      els.forEach(function(el){
        if(el.dataset.id===wecomUserId){onEntityClick(el);found=true;}
      });
      if(found)clearInterval(checkInterval);
      setTimeout(function(){clearInterval(checkInterval);},5000);
    },100);
  }
}
function onSearchMsgItemClick(){
  var convId=this.dataset.searchMsg;
  var convType=this.dataset.searchMsgType;
  var entityId=this.dataset.searchMsgSid;
  var entityType=this.dataset.searchMsgStype;
  if(!convId)return;
  document.getElementById('search-input').value='';
  document.getElementById('search-results').style.display='none';
  searchLastQ='';
  // Navigate using API-provided entity context
  if(entityId&&entityType){
    // Navigate to the entity first
    var targetMode=entityType==='staff'?'staff':'contact';
    setMode(targetMode);
    var waitForEntity=function(){
      var entityBody=document.getElementById('entity-body');
      var els=entityBody.querySelectorAll('.entity-item');
      var found=false;
      els.forEach(function(el){
        if(el.dataset.id===entityId){
          onEntityClick(el);
          found=true;
        }
      });
      if(found){
        // Wait for conv list to load, then select target conversation
        var waitForConv=setInterval(function(){
          var convBody=document.getElementById('conv-body');
          var cards=convBody.querySelectorAll('.conv-card');
          var cfound=false;
          cards.forEach(function(card){
            if(card.dataset.id===convId){
              onConvClick(card);
              cfound=true;
            }
          });
          if(cfound)clearInterval(waitForConv);
          setTimeout(function(){clearInterval(waitForConv);},5000);
        },100);
      }else{
        setTimeout(waitForEntity,200);
      }
    };
    setTimeout(waitForEntity,200);
    return;
  }
  // Fallback: try to find in current conv list
  if(lastConvItems){
    for(var i=0;i<lastConvItems.length;i++){
      if(lastConvItems[i].conversation_id===convId){
        var convBody=document.getElementById('conv-body');
        var cards=convBody.querySelectorAll('.conv-card');
        cards.forEach(function(card){
          if(card.dataset.id===convId)onConvClick(card);
        });
        return;
      }
    }
  }
}
function onSearchInput(){
  var input=document.getElementById('search-input');
  var results=document.getElementById('search-results');
  if(searchTimer)clearTimeout(searchTimer);
  if(!input.value.trim()){
    searchLastQ='';
    results.style.display='none';
    return;
  }
  searchTimer=setTimeout(doSearch,300);
}
function onSearchBlur(){
  setTimeout(function(){document.getElementById('search-results').style.display='none';},200);
}
function onSearchFocus(){
  var results=document.getElementById('search-results');
  if(searchLastQ){results.style.display='block';}
}
document.getElementById('search-input').addEventListener('input',onSearchInput);
document.getElementById('search-input').addEventListener('blur',onSearchBlur);
document.getElementById('search-input').addEventListener('focus',onSearchFocus);
</script>
</body>
</html>
"""


_DIAGNOSTICS_CSS = (
    _PAGE_CSS
    + """
body{margin:0;padding:1.5rem 2rem 3rem;max-width:64rem}
.diag-header{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem}
.diag-header-text h1{font-size:1.3rem;margin-bottom:.35rem}
.diag-header-text p{color:#666;font-size:.85rem;max-width:44rem;margin:0}
.diag-actions{display:flex;align-items:center;gap:.75rem;flex-shrink:0}
.diag-actions a{font-size:.85rem}
.diag-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.75rem;margin-bottom:1.25rem}
.diag-card{border:1px solid #e0e0e0;border-radius:6px;padding:.75rem 1rem;background:#fafafa}
.diag-card-label{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.03em;margin-bottom:.35rem}
.diag-card-value{font-size:1.6rem;font-weight:700}
.diag-note{margin:0 0 1.25rem;padding:.55rem .8rem;background:#fffbe6;border:1px solid #ffe58f;border-radius:4px;font-size:.82rem;color:#874d00}
.diag-section{margin-bottom:1.5rem}
.diag-section h2{font-size:1rem;margin-bottom:.5rem}
.diag-meta-list{display:flex;flex-wrap:wrap;gap:.35rem 1.5rem;font-size:.85rem;color:#444;margin:0}
.diag-loading,.diag-empty{padding:1.5rem;text-align:center;color:#888;font-size:.88rem}
.diag-error{padding:.7rem .9rem;text-align:center;color:#cf1322;background:#fff2f0;border:1px solid #ffccc7;border-radius:4px;font-size:.85rem}
.diag-retry-btn{margin-left:.5rem;padding:.15rem .7rem;font-size:.8rem;border:1px solid #d9d9d9;border-radius:3px;background:#fff;cursor:pointer}
"""
)


# ---------------------------------------------------------------------------
# RND-180 — Message Reachability diagnostics page.
#
# This page has no server-side knowledge of reachability data at all: it
# renders a static shell, then fetches aggregate statistics client-side from
# GET /api/admin/reachability-audit (the RND-178 endpoint) and renders them.
# It must never compute reachability itself, never request per-message
# samples (include_samples is left at its default False), and never render
# anything beyond the aggregate fields that endpoint already returns.
#
# ReachabilityStatusRegistry below mirrors the MessageTypeRegistry pattern
# from _REVIEW_CONSOLE_HTML (RND-173): one central status->label mapping
# with an explicit fallback, so an unrecognized future status still renders
# safely instead of being dropped or throwing.
# ---------------------------------------------------------------------------
_DIAGNOSTICS_HTML = (
    """\
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title></title>
<style>"""
    + _DIAGNOSTICS_CSS
    + """</style>
</head>
<body>
"""
    + I18N_SCRIPT_TAG
    + """
<div class="diag-header">
  <div class="diag-header-text">
    <h1 data-i18n="diagnostics.pageTitle">消息可达性诊断</h1>
    <p data-i18n="diagnostics.pageDescription">本页仅展示消息可达性审计的聚合统计数据，不包含消息内容或原始标识符。</p>
  </div>
  <div class="diag-actions">
    <a href="/admin/conversations"><span data-i18n="diagnostics.backToConsole">返回审阅控制台</span></a>
    <button id="btn-diag-refresh" data-i18n="diagnostics.refresh">刷新</button>
  </div>
</div>
<div id="diag-root"><div class="diag-loading" data-i18n="diagnostics.loading">正在加载诊断数据…</div></div>
<script>
function esc(s){
  return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function handleUnauth(r){
  if(r.status===401){window.location.href='/admin/login';return true;}
  return false;
}
function applyStaticI18n(){
  document.documentElement.lang=I18N.getLocale();
  document.title=I18N.t('diagnostics.pageTitle');
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent=I18N.t(el.getAttribute('data-i18n'));
  });
}

var ReachabilityStatusRegistry=(function(){
  var entries={
    reachable_direct:{labelKey:'reachability.status.reachable_direct',reachable:true},
    reachable_group:{labelKey:'reachability.status.reachable_group',reachable:true},
    unreachable_missing_recipient:{labelKey:'reachability.status.unreachable_missing_recipient',reachable:false},
    unreachable_missing_room:{labelKey:'reachability.status.unreachable_missing_room',reachable:false},
    unreachable_missing_sender:{labelKey:'reachability.status.unreachable_missing_sender',reachable:false},
    unreachable_membership:{labelKey:'reachability.status.unreachable_membership',reachable:false},
    unreachable_other:{labelKey:'reachability.status.unreachable_other',reachable:false}
  };
  var FALLBACK={labelKey:'reachability.status.unknown',reachable:false};
  function resolve(status){
    return (status&&Object.prototype.hasOwnProperty.call(entries,status))?entries[status]:FALLBACK;
  }
  function label(status){ return I18N.t(resolve(status).labelKey); }
  function isReachable(status){ return resolve(status).reachable===true; }
  return {entries:entries,fallback:FALLBACK,resolve:resolve,label:label,isReachable:isReachable};
})();

var lastReport=null;

function pct(n,d){
  if(!d)return'0.0%';
  return (n/d*100).toFixed(1)+'%';
}

function fmtInt(n){
  return (typeof n==='number')?n.toLocaleString():esc(n);
}

function cardHtml(labelKey,value){
  return '<div class="diag-card"><div class="diag-card-label">'+esc(I18N.t(labelKey))+'</div>'+
    '<div class="diag-card-value">'+esc(value)+'</div></div>';
}

function metaHtml(labelKey,value){
  return '<span><strong>'+esc(I18N.t(labelKey))+':</strong> '+esc(value)+'</span>';
}

function renderStatusTable(countsByStatus,unreachableTotal){
  countsByStatus=countsByStatus||{};
  var statuses=Object.keys(countsByStatus).filter(function(s){
    return countsByStatus[s]>0 && !ReachabilityStatusRegistry.isReachable(s);
  });
  if(!statuses.length){
    return '<div class="diag-empty">'+esc(I18N.t('diagnostics.noData'))+'</div>';
  }
  statuses.sort(function(a,b){ return (countsByStatus[b]||0)-(countsByStatus[a]||0); });
  var rows='';
  statuses.forEach(function(status){
    var count=countsByStatus[status]||0;
    rows+='<tr><td>'+esc(ReachabilityStatusRegistry.label(status))+'</td><td>'+fmtInt(count)+'</td><td>'+pct(count,unreachableTotal)+'</td></tr>';
  });
  return '<table><thead><tr><th>'+esc(I18N.t('diagnostics.reasonColumn'))+'</th><th>'+
    esc(I18N.t('diagnostics.countColumn'))+'</th><th>'+esc(I18N.t('diagnostics.percentColumn'))+'</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table>';
}

function renderTypeTable(countsByType){
  countsByType=countsByType||{};
  var types=Object.keys(countsByType).filter(function(t){ return countsByType[t]>0; });
  if(!types.length){
    return '<div class="diag-empty">'+esc(I18N.t('diagnostics.noData'))+'</div>';
  }
  types.sort(function(a,b){ return (countsByType[b]||0)-(countsByType[a]||0); });
  var rows='';
  types.forEach(function(t){
    rows+='<tr><td>'+esc(t)+'</td><td>'+fmtInt(countsByType[t])+'</td></tr>';
  });
  return '<table><thead><tr><th>'+esc(I18N.t('diagnostics.typeColumn'))+'</th><th>'+
    esc(I18N.t('diagnostics.totalColumn'))+'</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

function renderReport(data){
  var root=document.getElementById('diag-root');
  if(!data.matching_total){
    root.innerHTML='<div class="diag-empty">'+esc(I18N.t('diagnostics.noData'))+'</div>';
    return;
  }

  var html='';

  if(data.has_more){
    html+='<div class="diag-note">'+esc(I18N.t('diagnostics.moreMessagesExist'))+'</div>';
  }

  var reachRate=pct(data.reachable_count,(data.reachable_count||0)+(data.unreachable_count||0));

  html+='<div class="diag-cards">';
  html+=cardHtml('diagnostics.successfulDecrypted',fmtInt(data.matching_total));
  html+=cardHtml('diagnostics.reachableMessages',fmtInt(data.reachable_count));
  html+=cardHtml('diagnostics.unreachableMessages',fmtInt(data.unreachable_count));
  html+=cardHtml('diagnostics.reachabilityRate',reachRate);
  html+='</div>';

  html+='<div class="diag-section"><h2>'+esc(I18N.t('diagnostics.scanMetadataTitle'))+'</h2>';
  html+='<div class="diag-meta-list">';
  html+=metaHtml('diagnostics.scannedMessages',fmtInt(data.scanned_count));
  html+=metaHtml('diagnostics.matchingTotal',fmtInt(data.matching_total));
  html+=metaHtml('diagnostics.limitLabel',fmtInt(data.limit));
  html+=metaHtml('diagnostics.offsetLabel',fmtInt(data.offset));
  html+=metaHtml('diagnostics.hasMoreLabel',data.has_more?I18N.t('diagnostics.hasMoreYes'):I18N.t('diagnostics.hasMoreNo'));
  html+='</div></div>';

  html+='<div class="diag-section"><h2>'+esc(I18N.t('diagnostics.unreachableReasons'))+'</h2>';
  html+=renderStatusTable(data.counts_by_status,data.unreachable_count);
  html+='</div>';

  html+='<div class="diag-section"><h2>'+esc(I18N.t('diagnostics.byMessageType'))+'</h2>';
  html+=renderTypeTable(data.counts_by_message_type);
  html+='</div>';

  root.innerHTML=html;
}

function renderError(){
  var root=document.getElementById('diag-root');
  root.innerHTML='<div class="diag-error">'+esc(I18N.t('diagnostics.failedToLoad'))+
    ' <button class="diag-retry-btn" onclick="loadDiagnostics()">'+esc(I18N.t('diagnostics.retry'))+'</button></div>';
}

function loadDiagnostics(){
  var root=document.getElementById('diag-root');
  root.innerHTML='<div class="diag-loading">'+esc(I18N.t('diagnostics.loading'))+'</div>';
  fetch('/api/admin/reachability-audit').then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('request_failed');
    return r.json();
  }).then(function(data){
    if(!data)return;
    lastReport=data;
    renderReport(data);
  }).catch(function(){
    renderError();
  });
}

applyStaticI18n();
document.getElementById('btn-diag-refresh').addEventListener('click',loadDiagnostics);
I18N.onChange(function(){
  applyStaticI18n();
  if(lastReport){renderReport(lastReport);}
});
loadDiagnostics();
</script>
</body>
</html>
"""
)


@app.get("/admin/conversations", response_class=HTMLResponse)
def admin_conversations(request: Request, db: Session = Depends(get_db)):
    """Three-column conversation review console. Requires valid session."""
    if _resolve_session_tenant_id(request, db) is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=_REVIEW_CONSOLE_HTML)


@app.get("/admin/diagnostics/reachability", response_class=HTMLResponse)
def admin_diagnostics_reachability(request: Request, db: Session = Depends(get_db)):
    """
    Message Reachability diagnostics page (RND-180). Requires valid session.

    Thin UI over GET /api/admin/reachability-audit (RND-178) — this route
    only renders the page shell; all reachability classification and
    aggregation happens server-side in app.reachability_audit and is
    fetched client-side from that endpoint, never recomputed here.
    """
    if _resolve_session_tenant_id(request, db) is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=_DIAGNOSTICS_HTML)


@app.get("/api/messages", response_model=list[MessageOut])
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


@app.get("/api/messages/{msgid}", response_model=MessageDetailOut)
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
