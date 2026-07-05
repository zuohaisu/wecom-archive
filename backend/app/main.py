import html as _html
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, get_current_user
from app.db.models import AdminSession, ArchiveMessage, ArchiveMessageRecipient
from app.db.session import get_db
from app.routers.auth import router as auth_router
from app.routers.conversations import router as conversations_router
from app.routers.wecom_events import router as wecom_events_router

app = FastAPI(title="365 WeCom Archive")
app.include_router(auth_router)
app.include_router(conversations_router)
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
    session = (
        db.query(AdminSession)
        .filter(
            AdminSession.id == session_id,
            AdminSession.expires_at > now,
            AdminSession.is_revoked.is_(False),
        )
        .first()
    )
    return session.tenant_id if session is not None else None


@app.get("/health")
def health():
    return {"status": "ok"}


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
  <dt>msgtime (UTC+8)</dt><dd>{_fmt_msgtime(msg.msgtime)}</dd>
  <dt>decrypt_status</dt><dd>{_badge(msg.decrypt_status)}</dd>
  <dt>content_text</dt><dd><pre style="white-space:pre-wrap">{_e(msg.content_text)}</pre></dd>
</dl>
<h2>Recipients</h2>
<ul>{recipient_html}</ul>
</body></html>"""
    return HTMLResponse(content=body)


_REVIEW_CONSOLE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Conversation Review Console</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;color:#222;background:#f0f2f5;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.top-bar{height:44px;background:#001529;color:#fff;display:flex;align-items:center;padding:0 1rem;gap:1rem;flex-shrink:0}
.top-bar h1{font-size:.95rem;font-weight:600;letter-spacing:.01em}
.top-bar a{color:#8ca0b3;font-size:.82rem;text-decoration:none}
.top-bar a:hover{color:#fff}
.top-bar-user{margin-left:auto;font-size:.8rem;color:#8ca0b3;display:flex;align-items:center;gap:.75rem}
.tz-note{font-size:.72rem;color:#5c7185}
.btn-logout{background:transparent;border:1px solid #3a4a5a;color:#8ca0b3;padding:.2rem .65rem;border-radius:3px;cursor:pointer;font-size:.78rem}
.btn-logout:hover{border-color:#8ca0b3;color:#fff}
.layout{display:flex;flex:1;overflow:hidden}
.col{display:flex;flex-direction:column;overflow:hidden;background:#fff;border-right:1px solid #e8e8e8}
.col-left{width:220px;flex-shrink:0}
.col-mid{width:304px;flex-shrink:0}
.col-right{flex:1;border-right:none}
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
.tl-bubble{background:#f0f0f0;border-radius:4px;padding:.3rem .5rem;font-size:.83rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-width:580px}
.tl-bubble-staff{background:#e6f4ff;border-left:3px solid #1890ff}
.tl-bubble-self{background:#d9f7be;border:1px solid #b7eb8f}
.tl-bubble-other{background:#fff;border:1px solid #e8e8e8}
.tl-rcpt{font-size:.7rem;color:#ccc}
.load-older-btn{margin:0 auto .6rem;display:block;padding:.3rem .9rem;font-size:.75rem;border:1px solid #d9d9d9;border-radius:3px;background:#fff;color:#555;cursor:pointer}
.load-older-btn:hover{background:#f5f5f5}
.load-older-btn:disabled{color:#ccc;cursor:default}
.empty-state{padding:2.5rem 1rem;text-align:center;color:#ccc;font-size:.83rem}
.loading{padding:1rem;text-align:center;color:#bbb;font-size:.82rem}
.error-msg{margin:.5rem;padding:.6rem .75rem;background:#fff2f0;color:#cf1322;border:1px solid #ffccc7;border-radius:3px;font-size:.8rem}
.media-placeholder{background:#fafafa;border:1px dashed #d9d9d9;border-radius:4px;padding:.35rem .6rem;font-size:.8rem;color:#888;font-style:italic}
.media-preview{max-width:280px;max-height:280px;border-radius:4px;display:block}
</style>
</head>
<body>
<div class="top-bar">
  <h1>Conversation Review Console</h1>
  <span class="tz-note">Times shown in Beijing time (UTC+8)</span>
  <div class="top-bar-user">
    <span id="current-user"></span>
    <a href="/admin/messages">Messages ↗</a>
    <button class="btn-logout" onclick="doLogout()">Logout</button>
  </div>
</div>
<div class="layout">
  <div class="col col-left">
    <div class="mode-tabs">
      <button class="mode-tab active" id="tab-staff" onclick="setMode('staff')">Staff</button>
      <button class="mode-tab" id="tab-contact" onclick="setMode('contact')">Contact</button>
    </div>
    <div class="col-header" id="entity-header">Monitored Accounts</div>
    <div class="col-body" id="entity-body"><div class="loading">Loading…</div></div>
  </div>
  <div class="col col-mid">
    <div class="col-header" id="conv-header">Conversations</div>
    <div class="col-body" id="conv-body"><div class="empty-state">Select an account or contact</div></div>
  </div>
  <div class="col col-right">
    <div class="col-header" id="timeline-header">Message Timeline</div>
    <div class="col-body" id="timeline-body"><div class="empty-state">Select a conversation</div></div>
  </div>
</div>
<script>
var mode='staff',selEntityId=null,selConvId=null;
var timelineConvId=null,timelineMsgs=[],timelineHasOlder=false,timelineNextBefore=null,timelineLoadingOlder=false;
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
  mode=m; selEntityId=null; selConvId=null;
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.getElementById('tab-staff').classList.toggle('active',m==='staff');
  document.getElementById('tab-contact').classList.toggle('active',m==='contact');
  document.getElementById('entity-header').textContent=m==='staff'?'Monitored Accounts':'Contacts';
  document.getElementById('conv-header').textContent='Conversations';
  document.getElementById('timeline-header').textContent='Message Timeline';
  document.getElementById('conv-body').innerHTML='<div class="empty-state">Select an account or contact</div>';
  document.getElementById('timeline-body').innerHTML='<div class="empty-state">Select a conversation</div>';
  loadEntityList();
}
function loadEntityList(){
  var url=mode==='staff'?'/api/monitored-accounts':'/api/contacts';
  document.getElementById('entity-body').innerHTML='<div class="loading">Loading…</div>';
  fetch(url).then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(items){if(items)renderEntityList(items);})
    .catch(function(){document.getElementById('entity-body').innerHTML='<div class="error-msg">Failed to load entities</div>';});
}
function renderEntityList(items){
  var body=document.getElementById('entity-body');
  if(!items||!items.length){body.innerHTML='<div class="empty-state">None found</div>';return;}
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
      var seatLabel=item.seat_status==='active'?'Active':'History';
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
  selEntityId=el.dataset.id; selConvId=null;
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.querySelectorAll('.entity-item').forEach(function(e){e.classList.remove('active');});
  el.classList.add('active');
  document.getElementById('conv-header').textContent='Conversations — '+el.dataset.name;
  document.getElementById('timeline-header').textContent='Message Timeline';
  document.getElementById('timeline-body').innerHTML='<div class="empty-state">Select a conversation</div>';
  loadConversations(selEntityId);
}
function loadConversations(entityId){
  var url=mode==='staff'
    ?'/api/conversations?mode=staff&staff_id='+encodeURIComponent(entityId)
    :'/api/conversations?mode=contact&contact_id='+encodeURIComponent(entityId);
  document.getElementById('conv-body').innerHTML='<div class="loading">Loading…</div>';
  fetch(url).then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(convs){if(convs)renderConvList(convs);})
    .catch(function(){document.getElementById('conv-body').innerHTML='<div class="error-msg">Failed to load conversations</div>';});
}
function renderConvList(convs){
  var body=document.getElementById('conv-body');
  if(!convs||!convs.length){body.innerHTML='<div class="empty-state">No conversations found</div>';return;}
  var html='';
  convs.forEach(function(c){
    var tb=c.conversation_type==='group'
      ?'<span class="badge badge-group">group</span>'
      :'<span class="badge badge-direct">direct</span>';
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
    html+='<div class="conv-card" data-id="'+esc(c.conversation_id)+'" data-name="'+esc(c.display_name)+'" onclick="onConvClick(this)">'
      +'<div class="conv-top"><span class="conv-name" title="'+esc(rawId)+'">'+esc(c.display_name)+'</span><span class="conv-time">'+esc(t)+'</span></div>'
      +secondary
      +(snip?'<div class="conv-snippet">'+snip+'</div>':'')
      +'<div class="conv-meta">'+tb+' <span class="badge badge-count">'+esc(c.message_count)+' msgs</span>'+acct+'</div>'
      +'</div>';
  });
  body.innerHTML=html;
}
function onConvClick(el){
  selConvId=el.dataset.id;
  document.querySelectorAll('.conv-card').forEach(function(e){e.classList.remove('active');});
  el.classList.add('active');
  document.getElementById('timeline-header').textContent='Timeline — '+el.dataset.name;
  loadTimeline(selConvId);
}
function loadTimeline(convId){
  timelineConvId=convId; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.getElementById('timeline-body').innerHTML='<div class="loading">Loading…</div>';
  fetchTimelinePage(null, true);
}
function fetchTimelinePage(before, isInitial){
  var url='/api/conversations/'+encodeURIComponent(timelineConvId)+'/messages?limit=20';
  if(before)url+='&before='+encodeURIComponent(before);
  return fetch(url)
    .then(function(r){if(handleUnauth(r))return null;if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(function(data){
      if(!data)return;
      timelineMsgs=before?data.messages.concat(timelineMsgs):data.messages;
      timelineHasOlder=data.pagination.has_older;
      timelineNextBefore=data.pagination.next_before;
      renderTimeline(isInitial);
    })
    .catch(function(e){document.getElementById('timeline-body').innerHTML='<div class="error-msg">Failed to load: '+esc(e.message)+'</div>';});
}
function loadOlderMessages(){
  if(timelineLoadingOlder||!timelineHasOlder)return;
  timelineLoadingOlder=true;
  var body=document.getElementById('timeline-body');
  var prevScrollHeight=body.scrollHeight;
  var prevScrollTop=body.scrollTop;
  fetchTimelinePage(timelineNextBefore, false).then(function(){
    timelineLoadingOlder=false;
    var body2=document.getElementById('timeline-body');
    body2.scrollTop=prevScrollTop+(body2.scrollHeight-prevScrollHeight);
  });
}
var MEDIA_LABELS={image:'Image message',video:'Video message',voice:'Voice message',file:'File message'};
var MEDIA_STATUS_LABELS={not_downloaded:'not downloaded',unsupported:'unsupported',unknown:'status unknown',failed:'download failed'};
function renderMessageBody(m){
  var mediaType=m.media_type||'text';
  if(mediaType==='text'){
    return m.content_text?esc(m.content_text):'<div class="media-placeholder">Empty text message</div>';
  }
  if(mediaType==='image'&&m.media_status==='available'&&m.media_url){
    return '<a href="'+esc(m.media_url)+'" target="_blank" rel="noopener noreferrer">'
      +'<img class="media-preview" src="'+esc(m.media_url)+'" alt="Image message" loading="lazy"></a>';
  }
  if(mediaType==='unsupported'){
    return '<div class="media-placeholder">Unsupported message type</div>';
  }
  if(mediaType==='unknown'){
    return '<div class="media-placeholder">Unknown message type</div>';
  }
  var label=MEDIA_LABELS[mediaType]||'Media message';
  var statusLabel=MEDIA_STATUS_LABELS[m.media_status]||'unsupported';
  return '<div class="media-placeholder">'+esc(label)+' · '+esc(statusLabel)+'</div>';
}
function renderTimeline(scrollToBottom){
  var body=document.getElementById('timeline-body');
  if(!timelineMsgs||!timelineMsgs.length){body.innerHTML='<div class="empty-state">No messages</div>';return;}
  var html='';
  if(timelineHasOlder){
    html+='<button class="load-older-btn" onclick="loadOlderMessages()">Load older messages</button>';
  }
  html+='<div class="timeline">';
  timelineMsgs.forEach(function(m){
    var isSelf=mode==='staff'&&selEntityId&&m.sender===selEntityId;
    var isStaff=m.sender&&m.sender.indexOf('staff_')===0;
    var rowCls='tl-row '+(isSelf?'tl-row-self':'tl-row-other');
    var sc='tl-sender'+(isStaff?' tl-staff':'');
    var bc='tl-bubble '+(isSelf?'tl-bubble-self':(mode==='staff'?'tl-bubble-other':(isStaff?'tl-bubble-staff':'')));
    var text=renderMessageBody(m);
    var mt=(m.msgtype&&m.msgtype!=='text')?' <span class="badge badge-count" style="font-size:.67rem">'+esc(m.msgtype)+'</span>':'';
    var grp=m.roomid?' <span class="badge badge-group" style="font-size:.65rem">group</span>':'';
    var senderName=m.sender_display_name||m.sender||'?';
    var senderRaw=m.sender_raw_id||m.sender;
    var senderSecondary=(senderRaw&&senderRaw!==senderName)?' <span class="tl-sender-raw">('+esc(senderRaw)+')</span>':'';
    var rcptNames=(m.recipient_display_names&&m.recipient_display_names.length)?m.recipient_display_names:(m.recipients||[]);
    var rcpt=rcptNames.length?'<div class="tl-rcpt">→ '+esc(rcptNames.join(', '))+'</div>':'';
    html+='<div class="'+rowCls+'">'
      +'<div class="tl-meta"><span class="'+sc+'">'+esc(senderName)+'</span>'+senderSecondary
      +' <span class="tl-time">'+esc(fmtTime(m.msgtime))+'</span>'+mt+grp+'</div>'
      +'<div class="'+bc+'">'+text+'</div>'
      +rcpt+'</div>';
  });
  html+='</div>';
  body.innerHTML=html;
  if(scrollToBottom){body.scrollTop=body.scrollHeight;}
}
loadCurrentUser();
setMode('staff');
</script>
</body>
</html>
"""


@app.get("/admin/conversations", response_class=HTMLResponse)
def admin_conversations(request: Request, db: Session = Depends(get_db)):
    """Three-column conversation review console. Requires valid session."""
    if _resolve_session_tenant_id(request, db) is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=_REVIEW_CONSOLE_HTML)


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

    # Recipients safe to load by message PK: parent was already tenant-verified above.
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
        recipients=[RecipientOut.model_validate(r) for r in recipients],
    )
