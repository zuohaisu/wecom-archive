import html as _html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, get_current_user
from app.db.models import AdminSession, ArchiveMessage, ArchiveMessageRecipient
from app.db.schema_check import full_readiness_check
from app.db.session import get_db, get_engine
from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG
from app.message_type_registry import build_filterable_type_options, build_frontend_registry_entries
from app.routers.auth import router as auth_router
from app.routers.conversations import MediaAccessNoStoreMiddleware
from app.routers.conversations import router as conversations_router
from app.routers.reachability_audit import router as reachability_audit_router
from app.routers.search import router as search_router
from app.routers.wecom_events import router as wecom_events_router
from app.web import render_template

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

    body = render_template("messages", sender_val=sender_val, q_val=q_val, row_html=row_html)
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


# RND-196 — the embedded MessageTypeRegistry's `entries` object (injected
# into templates/review_console.html via render_template, see
# admin_conversations below) is generated from app.message_type_registry
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

# RND-230 — the search page's "消息类型" filter options are generated from
# the same app.message_type_registry catalog as the console's
# MessageTypeRegistry above, instead of a second hand-maintained list —
# see build_filterable_type_options()'s docstring for why a hand-rolled
# subset previously drifted (missing raw types entirely, and mismatched
# "system"/"miniprogram" against the real stored "sys"/"weapp" values).
_SEARCH_MSGTYPE_OPTIONS_JSON = json.dumps(build_filterable_type_options())



@app.get("/admin/conversations", response_class=HTMLResponse)
def admin_conversations(request: Request, db: Session = Depends(get_db)):
    """Three-column conversation review console. Requires valid session."""
    if _resolve_session_tenant_id(request, db) is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "review_console",
            i18n_script=I18N_SCRIPT_TAG,
            mtr_entries_json=_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON,
        )
    )


@app.get("/admin/search", response_class=HTMLResponse)
def admin_search_page(request: Request, db: Session = Depends(get_db)):
    """Standalone search-results page (RND-229). Requires valid session."""
    if _resolve_session_tenant_id(request, db) is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "search",
            i18n_script=I18N_SCRIPT_TAG,
            msgtype_options_json=_SEARCH_MSGTYPE_OPTIONS_JSON,
        )
    )


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
    return HTMLResponse(content=render_template("diagnostics", i18n_script=I18N_SCRIPT_TAG))


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


# ---------------------------------------------------------------------------
# RND-216 — admin console static assets (base.css/diagnostics.css,
# search.js/diagnostics.js, and — since RND-217 split it into 8 modules —
# web/static/console/*.js). Mounted last so it never shadows an API route.
# Every template references these through
# app.web.STATIC_VERSION's `?v=<hash>` query string (see render_template),
# so a long, immutable Cache-Control here is safe: any content change
# produces a new URL, and stale-cached responses under the old URL are
# simply never requested again. This is unrelated to (and does not need an
# exemption from) app.routers.conversations.MediaAccessNoStoreMiddleware —
# that middleware only touches the media-access-descriptor path pattern
# (see _MEDIA_ACCESS_PATH_RE), never /web/static.
# ---------------------------------------------------------------------------
class _VersionedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


_STATIC_DIR = Path(__file__).parent / "web" / "static"
app.mount(
    "/web/static",
    _VersionedStaticFiles(directory=str(_STATIC_DIR), check_dir=False),
    name="web-static",
)
