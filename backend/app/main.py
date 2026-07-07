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
from app.i18n_assets import I18N_SCRIPT_TAG
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
<html lang="zh-CN">
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
.media-preview{max-width:280px;max-height:280px;border-radius:4px;display:block}
.new-msg-indicator{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);background:#1890ff;color:#fff;border:none;border-radius:999px;padding:.35rem 1rem;font-size:.78rem;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.new-msg-indicator:hover{background:#0958d9}
.lang-switch{position:relative}
.btn-lang{background:transparent;border:1px solid #3a4a5a;color:#8ca0b3;padding:.2rem .65rem;border-radius:3px;cursor:pointer;font-size:.78rem}
.btn-lang:hover{border-color:#8ca0b3;color:#fff}
.lang-menu{position:absolute;top:135%;right:0;background:#fff;border:1px solid #e8e8e8;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.18);min-width:7rem;overflow:hidden;z-index:50}
.lang-option{padding:.4rem .7rem;font-size:.8rem;color:#333;cursor:pointer;white-space:nowrap}
.lang-option:hover{background:#f5f5f5}
.lang-option.active{color:#1890ff;font-weight:600;background:#e6f4ff}
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
  <div class="top-bar-user">
    <span id="current-user"></span>
    <a href="/admin/messages"><span data-i18n="nav.messages">消息记录</span> ↗</a>
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
var timelineConvId=null,timelineMsgs=[],timelineHasOlder=false,timelineNextBefore=null,timelineLoadingOlder=false;
var timelineHistoryError=null,timelineTopObserver=null;
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
function renderEntityList(items){
  lastEntityItems=items;
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
    html+='<div class="conv-card" data-id="'+esc(c.conversation_id)+'" data-name="'+esc(c.display_name)+'" onclick="onConvClick(this)">'
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
  loadTimeline(selConvId);
}
function loadTimeline(convId){
  timelineConvId=convId; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  timelineLoadingOlder=false; timelineHistoryError=null;
  stopHistoryObserver();
  hideNewMessageIndicator();
  document.getElementById('timeline-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
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
      startHistoryObserver();
    })
    .catch(function(e){document.getElementById('timeline-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadPrefix')+esc(e.message)+'</div>';});
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
  var url='/api/conversations/'+encodeURIComponent(convId)+'/messages?limit=20&before='+encodeURIComponent(before);
  return fetch(url).then(function(r){
    if(handleUnauth(r)){var e=new Error('unauthorized');e.handled=true;throw e;}
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(data){
    if(timelineConvId!==convId)return;
    timelineMsgs=data.messages.concat(timelineMsgs);
    timelineHasOlder=data.pagination.has_older;
    timelineNextBefore=data.pagination.next_before;
  });
}
function loadOlderAutomatically(){
  if(timelineLoadingOlder||!timelineHasOlder||timelineHistoryError)return;
  var requestConvId=timelineConvId;
  var body=document.getElementById('timeline-body');
  var beforeHeight=body?body.scrollHeight:0;
  timelineLoadingOlder=true;
  showLoadingOlder();
  preserveScrollPosition(body,beforeHeight);
  var beforeHeight2=body?body.scrollHeight:0;
  fetchOlderMessages(requestConvId,timelineNextBefore).then(function(){
    if(timelineConvId!==requestConvId)return;
    timelineLoadingOlder=false;
    renderTimeline(false);
    startHistoryObserver();
    preserveScrollPosition(body,beforeHeight2);
  }).catch(function(e){
    if(timelineConvId!==requestConvId)return;
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
  var entries={
    text:{category:'text'},
    image:{category:'media',mediaType:'image',previewSupported:true},
    video:{category:'placeholder',mediaType:'video',previewSupported:false,placeholderKey:'placeholder.video'},
    voice:{category:'placeholder',mediaType:'voice',previewSupported:false,placeholderKey:'placeholder.voice'},
    file:{category:'placeholder',mediaType:'file',previewSupported:false,placeholderKey:'placeholder.file'},
    location:{category:'placeholder',mediaType:'location',previewSupported:false,placeholderKey:'placeholder.location'},
    link:{category:'placeholder',mediaType:'link',previewSupported:false,placeholderKey:'placeholder.link'},
    card:{category:'placeholder',mediaType:'card',previewSupported:false,placeholderKey:'placeholder.card'},
    emotion:{category:'placeholder',mediaType:'emotion',previewSupported:false,placeholderKey:'placeholder.emotion'},
    miniprogram:{category:'placeholder',mediaType:'miniprogram',previewSupported:false,placeholderKey:'placeholder.miniprogram'},
    todo:{category:'placeholder',mediaType:'todo',previewSupported:false,placeholderKey:'placeholder.todo'}
  };
  var FALLBACK={category:'placeholder',placeholderKey:'placeholder.unsupported'};
  function resolve(msgtype){
    return (msgtype&&Object.prototype.hasOwnProperty.call(entries,msgtype))?entries[msgtype]:null;
  }
  function resolvePlaceholder(msgtype){
    var entry=resolve(msgtype);
    return (entry&&entry.category==='placeholder')?entry:null;
  }
  return {entries:entries,resolve:resolve,resolvePlaceholder:resolvePlaceholder,fallback:FALLBACK};
})();
function renderMessageBody(m){
  var mediaType=m.media_type||'text';
  if(mediaType==='text'){
    return m.content_text?esc(m.content_text):'<div class="media-placeholder">'+I18N.t('timeline.emptyText')+'</div>';
  }
  if(mediaType==='image'&&m.media_status==='available'&&m.media_url){
    return '<a href="'+esc(m.media_url)+'" target="_blank" rel="noopener noreferrer">'
      +'<img class="media-preview" src="'+esc(m.media_url)+'" alt="'+esc(I18N.t('media.image'))+'" loading="lazy"></a>';
  }
  if(mediaType==='image'){
    var imgLabel=MEDIA_LABELS.image||I18N.t('media.generic');
    var imgStatusLabel=MEDIA_STATUS_LABELS[m.media_status]||I18N.t('media.status.unsupported');
    return '<div class="media-placeholder">'+esc(imgLabel)+' · '+esc(imgStatusLabel)+'</div>';
  }
  if(mediaType==='unknown'){
    return '<div class="media-placeholder">'+I18N.t('media.unknownType')+'</div>';
  }
  var typeEntry=MessageTypeRegistry.resolvePlaceholder(m.msgtype)||MessageTypeRegistry.fallback;
  return '<div class="media-placeholder">'+I18N.t(typeEntry.placeholderKey)+'</div>';
}
function renderTimeline(scrollToBottom){
  var body=document.getElementById('timeline-body');
  if(!timelineMsgs||!timelineMsgs.length){body.innerHTML='<div class="empty-state">'+I18N.t('console.noMessages')+'</div>';return;}
  var pendingHistoryError=(typeof timelineHistoryError!=='undefined')&&timelineHistoryError;
  var html='<div id="timeline-history-status">'
    +(pendingHistoryError?historyRetryHtml():(timelineHasOlder?'':'<div class="history-status history-end">'+I18N.t('history.noMore')+'</div>'))
    +'</div>';
  html+='<div id="timeline-top-sentinel"></div>';
  html+='<div class="timeline">';
  timelineMsgs.forEach(function(m){
    var isSelf=mode==='staff'&&selEntityId&&m.sender===selEntityId;
    var isStaff=m.sender&&m.sender.indexOf('staff_')===0;
    var rowCls='tl-row '+(isSelf?'tl-row-self':'tl-row-other');
    var sc='tl-sender'+(isStaff?' tl-staff':'');
    var bc='tl-bubble '+(isSelf?'tl-bubble-self':(mode==='staff'?'tl-bubble-other':(isStaff?'tl-bubble-staff':'')));
    var text=renderMessageBody(m);
    var mt=(m.msgtype&&m.msgtype!=='text')?' <span class="badge badge-count" style="font-size:.67rem">'+esc(m.msgtype)+'</span>':'';
    var grp=m.roomid?' <span class="badge badge-group" style="font-size:.65rem">'+esc(I18N.t('timeline.groupBadge'))+'</span>':'';
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
  var url=mode==='staff'?'/api/monitored-accounts':'/api/contacts';
  return fetch(url).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(items){
    if(items)renderEntityList(items);
  });
}
function refreshConversationList(){
  if(!selEntityId)return Promise.resolve();
  var url=mode==='staff'
    ?'/api/conversations?mode=staff&staff_id='+encodeURIComponent(selEntityId)
    :'/api/conversations?mode=contact&contact_id='+encodeURIComponent(selEntityId);
  return fetch(url).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(convs){
    if(convs)renderConvList(convs);
  });
}
function refreshTimelineIfSelected(){
  if(!timelineConvId||timelineLoadingOlder)return Promise.resolve();
  var convId=timelineConvId;
  var body=document.getElementById('timeline-body');
  var wasNearBottom=isNearBottom();
  var prevScrollTop=body?body.scrollTop:0;
  var url='/api/conversations/'+encodeURIComponent(convId)+'/messages?limit=20';
  return fetch(url).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(data){
    if(!data||timelineConvId!==convId||timelineLoadingOlder)return;
    var existingIds={};
    timelineMsgs.forEach(function(m){existingIds[m.msgid]=true;});
    var hasNew=data.messages.some(function(m){return!existingIds[m.msgid];});
    timelineMsgs=mergeMessagesByMsgid(timelineMsgs,data.messages);
    renderTimeline(false);
    startHistoryObserver();
    var body2=document.getElementById('timeline-body');
    if(!body2)return;
    if(wasNearBottom){
      body2.scrollTop=body2.scrollHeight;
      hideNewMessageIndicator();
    }else{
      body2.scrollTop=prevScrollTop;
      if(hasNew)showNewMessageIndicator();
    }
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
