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
.conv-card{padding:.55rem .75rem;border-bottom:1px solid #f0f0f0;cursor:pointer}
.conv-card:hover{background:#f5f8ff}
.conv-card.active{background:#e6f4ff;border-left:3px solid #1890ff}
.conv-top{display:flex;align-items:baseline;gap:.3rem;margin-bottom:.15rem}
.conv-name{font-weight:600;font-size:.83rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv-time{font-size:.7rem;color:#bbb;white-space:nowrap;flex-shrink:0}
.conv-snippet{font-size:.78rem;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:.2rem}
.conv-meta{display:flex;align-items:center;gap:.3rem;flex-wrap:wrap}
.badge{display:inline-block;font-size:.7rem;padding:.05rem .3rem;border-radius:2px;font-weight:500;line-height:1.4}
.badge-direct{background:#f6ffed;color:#389e0d;border:1px solid #b7eb8f}
.badge-group{background:#e6f7ff;color:#0958d9;border:1px solid #91caff}
.badge-count{background:#f5f5f5;color:#999;border:1px solid #e8e8e8}
.badge-account{font-size:.68rem;color:#888}
.timeline{padding:.6rem .75rem;display:flex;flex-direction:column;gap:.65rem}
.tl-msg{display:flex;flex-direction:column;gap:.12rem}
.tl-meta{display:flex;align-items:center;gap:.35rem;flex-wrap:wrap}
.tl-sender{font-size:.78rem;font-weight:600;color:#555}
.tl-staff{color:#0958d9}
.tl-time{font-size:.72rem;color:#bbb}
.tl-bubble{background:#f0f0f0;border-radius:4px;padding:.3rem .5rem;font-size:.83rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-width:580px}
.tl-bubble-staff{background:#e6f4ff;border-left:3px solid #1890ff}
.tl-rcpt{font-size:.7rem;color:#ccc}
.empty-state{padding:2.5rem 1rem;text-align:center;color:#ccc;font-size:.83rem}
.loading{padding:1rem;text-align:center;color:#bbb;font-size:.82rem}
.error-msg{margin:.5rem;padding:.6rem .75rem;background:#fff2f0;color:#cf1322;border:1px solid #ffccc7;border-radius:3px;font-size:.8rem}
</style>
</head>
<body>
<div class="top-bar">
  <h1>Conversation Review Console</h1>
  <span style="margin-left:auto"><a href="/admin/messages">Messages ↗</a></span>
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
function esc(s){
  return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtTime(ms){
  if(!ms)return'';
  var d=new Date(ms);
  return d.getUTCFullYear()+'-'+pad(d.getUTCMonth()+1)+'-'+pad(d.getUTCDate())+' '+pad(d.getUTCHours())+':'+pad(d.getUTCMinutes())+' UTC';
}
function pad(n){return String(n).padStart(2,'0');}
function setMode(m){
  mode=m; selEntityId=null; selConvId=null;
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
  fetch(url).then(function(r){return r.json();}).then(renderEntityList)
    .catch(function(){document.getElementById('entity-body').innerHTML='<div class="error-msg">Failed to load entities</div>';});
}
function renderEntityList(items){
  var body=document.getElementById('entity-body');
  if(!items||!items.length){body.innerHTML='<div class="empty-state">None found</div>';return;}
  var html='';
  items.forEach(function(item){
    var id=mode==='staff'?item.monitored_account_id:item.contact_id;
    var name=item.display_name||id;
    var av=esc(name.charAt(0).toUpperCase());
    var bg=mode==='staff'?'#1890ff':'#389e0d';
    html+='<div class="entity-item" data-id="'+esc(id)+'" data-name="'+esc(name)+'" onclick="onEntityClick(this)">'
      +'<div class="entity-avatar" style="background:'+bg+'">'+av+'</div>'
      +'<span class="entity-name">'+esc(name)+'</span></div>';
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
  fetch(url).then(function(r){return r.json();}).then(renderConvList)
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
    var acct=(mode==='contact'&&c.monitored_account_ids&&c.monitored_account_ids.length)
      ?'<span class="badge-account">'+esc(c.monitored_account_ids.join(', '))+'</span>':'';
    html+='<div class="conv-card" data-id="'+esc(c.conversation_id)+'" data-name="'+esc(c.display_name)+'" onclick="onConvClick(this)">'
      +'<div class="conv-top"><span class="conv-name">'+esc(c.display_name)+'</span><span class="conv-time">'+esc(t)+'</span></div>'
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
  var url='/api/conversations/'+encodeURIComponent(convId)+'/messages';
  document.getElementById('timeline-body').innerHTML='<div class="loading">Loading…</div>';
  fetch(url)
    .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(renderTimeline)
    .catch(function(e){document.getElementById('timeline-body').innerHTML='<div class="error-msg">Failed to load: '+esc(e.message)+'</div>';});
}
function renderTimeline(msgs){
  var body=document.getElementById('timeline-body');
  if(!msgs||!msgs.length){body.innerHTML='<div class="empty-state">No messages</div>';return;}
  var html='<div class="timeline">';
  msgs.forEach(function(m){
    var isStaff=m.sender&&m.sender.indexOf('staff_')===0;
    var sc='tl-sender'+(isStaff?' tl-staff':'');
    var bc='tl-bubble'+(isStaff?' tl-bubble-staff':'');
    var text=m.content_text?esc(m.content_text):'['+esc(m.msgtype||'message')+']';
    var mt=(m.msgtype&&m.msgtype!=='text')?' <span class="badge badge-count" style="font-size:.67rem">'+esc(m.msgtype)+'</span>':'';
    var grp=m.roomid?' <span class="badge badge-group" style="font-size:.65rem">group</span>':'';
    var rcpt=(m.recipients&&m.recipients.length)?'<div class="tl-rcpt">→ '+esc(m.recipients.join(', '))+'</div>':'';
    html+='<div class="tl-msg">'
      +'<div class="tl-meta"><span class="'+sc+'">'+esc(m.sender||'?')+'</span>'
      +' <span class="tl-time">'+esc(fmtTime(m.msgtime))+'</span>'+mt+grp+'</div>'
      +'<div class="'+bc+'">'+text+'</div>'
      +rcpt+'</div>';
  });
  html+='</div>';
  body.innerHTML=html;
  body.scrollTop=body.scrollHeight;
}
setMode('staff');
</script>
</body>
</html>
"""


@app.get("/admin/conversations", response_class=HTMLResponse)
def admin_conversations():
    """Three-column conversation review console (RND-97)."""
    return HTMLResponse(content=_REVIEW_CONSOLE_HTML)


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
        recipients=[RecipientOut.model_validate(r) for r in recipients],
    )
