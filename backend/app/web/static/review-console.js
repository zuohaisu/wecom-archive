
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
      // RND-229 AC5/AC6 fix: trigger the initial locate only after the
      // first-screen timeline has rendered, so a slow first-screen response
      // no longer races the fixed 350ms timer used previously. The history
      // pagination path (focusCheckRow's timelineHasOlder branch) handles
      // targets that live in older pages.
      if(isInitial && focusPending && focusMsgId){
        focusPending=false;
        setTimeout(focusCheckRow,60);
      }
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
  var entries=RND216_MTR_ENTRIES;
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
  text=text.replace(/\[([^\]\n]*)\]\(([^)\n]*)\)/g,function(whole,label,url){
    var token=' LINK'+links.length+' ';
    links.push({label:label||url,url:isSafeUrl(url)?url:null});
    return token;
  });
  text=esc(text);
  text=text.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  var out=[];var inList=false;
  text.split(/\n/).forEach(function(line){
    var bullet=line.match(/^\s*[-*]\s+(.*)$/);
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
// RND-210 (+ QA FAIL remediation): business-card (名片) renderer. Surfaces
// the company name + contact identifier extracted by parse_card_message.
// When the backend resolved a tenant-scoped Contact display name
// (f.contact_name), it is shown as the primary contact label so the
// timeline reads "张三" instead of the raw WeCom userid "contact_zhangsan";
// the raw userid is shown as a secondary line for traceability. The WeCom
// protocol does NOT provide a display name or avatar itself, so when no
// Contact match exists we fall back to the raw userid and never fabricate
// an avatar/name (card.businessCard.noAvatar).
function renderCardMessage(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var corpName=f.corpname||'';
  var contactName=f.contact_name||f.userid||'';
  var contactId=f.userid||'';
  var html='<div class="structured-card structured-card-businesscard">'
    +'<div class="structured-card-type-label">'+esc(I18N.t(m.display_label_key))+'</div>';
  if(corpName)html+='<div class="structured-card-title">'+esc(I18N.t('card.businessCard.corpName'))+esc(corpName)+'</div>';
  if(contactName)html+='<div class="structured-card-desc">'+esc(I18N.t('card.businessCard.contactName'))+esc(contactName)+'</div>';
  // Show the raw userid only when it differs from the resolved name (i.e.
  // a name was actually found) — otherwise it would merely repeat the label.
  if(contactId&&contactName!==contactId)html+='<div class="structured-card-meta structured-card-userid">'+esc(I18N.t('card.businessCard.contactId'))+esc(contactId)+'</div>';
  // Protocol never provides an avatar/snapshot — state that rather than
  // inventing one (RND-210 privacy boundary).
  html+='<div class="structured-card-meta">'+esc(I18N.t('card.businessCard.noAvatar'))+'</div>';
  html+='</div>';
  return html;
}
// RND-210 (+ QA FAIL remediation): audio-archive (meeting_voice_call /
// audio_archive) renderer. Previously audio_archive was a PLACEHOLDER type
// excluded from the frontend registry, so it collapsed into the generic
// "unknown message type". As a STRUCTURED_CARD it now shows the audio-
// archive type label, the call end time (when present), and an explicit
// "not playable" status — never an unknown placeholder. Full playback is
// RND-202 scope, so no <audio> element is rendered here.
function renderAudioArchiveMessage(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var et=f.endtime;
  // WeCom endtime is epoch-seconds; fmtTime expects epoch-ms — convert only
  // when the value looks like seconds (defensive for either unit).
  if(et&&et<1e12)et=et*1000;
  var html='<div class="structured-card structured-card-audioarchive">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.audioArchive'))+'</div>';
  if(et)html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.endedAt'))+esc(fmtTime(et))+'</div>';
  html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.playbackUnavailable'))+'</div>';
  html+='</div>';
  return html;
}
// RND-210 (+ QA FAIL remediation): audio-shared-doc (voip_doc_share /
// audio_doc) renderer. Shows the shared document title (when the parser
// captured one) and an explicit "not playable" status. The previous
// renderStructuredFallback copy omitted the title, so surface it here.
function renderAudioDocMessage(m){
  var f=m.structured_content&&m.structured_content.fields;
  if(!f)return renderStructuredFallback(m);
  var title=f.title||f.docid||I18N.t('messageType.audioDoc');
  var html='<div class="structured-card structured-card-audiodoc">'
    +'<div class="structured-card-type-label">'+esc(I18N.t('messageType.audioDoc'))+'</div>'
    +'<div class="structured-card-title">'+esc(title)+'</div>';
  if(f.url&&isSafeUrl(f.url))html+='<a class="structured-card-link-action" href="'+esc(f.url)+'" target="_blank" rel="noopener noreferrer">'+esc(I18N.t('link.openLink'))+'</a>';
  html+='<div class="structured-card-meta">'+esc(I18N.t('audioArchive.playbackUnavailable'))+'</div>';
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
  card:renderCardMessage,
  docmsg:renderStructuredFallback,
  // RND-210 (+ QA FAIL remediation): audio_archive now renders a dedicated
  // card (type label + end time + explicit "not playable") instead of the
  // generic unknown placeholder. audio_doc shows the shared-doc title.
  audio_archive:renderAudioArchiveMessage,
  audio_doc:renderAudioDocMessage,
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
var focusMsgId=null; // RND-229: set by readFocusFromUrl() during init, read by focusCheckRow()
// RND-229 AC5/AC6 fix: the initial focus attempt must run only after the
// first-screen timeline has actually rendered (a fixed 350ms wait raced
// slow first-screen responses and silently dropped the locate). Set true
// once the target conversation is selected; consumed by fetchTimelinePage
// on its initial render-completion.
var focusPending=false;
applyStaticI18n();
loadCurrentUser();
setMode('staff');
lastRefreshAt=Date.now();
updateRefreshStatus();
startAutoRefresh();
readFocusFromUrl();

// RND-159: Search
var searchTimer=null,searchLastQ='';
function highlightKeyword(text,keyword){
  if(!keyword)return text;
  var re=new RegExp('('+keyword.replace(/[.*+?^${}()|[\]\\]/g,'\$&')+')','gi');
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
    var smAttempts=0;
    var waitForEntity=function(){
      if(++smAttempts>40)return; // ~8s cap; avoid infinite retry if entity missing
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

// RND-229: jump back from the search results page and highlight the target message.
// (focusMsgId is declared once near the top of this script, before readFocusFromUrl()
//  runs during init, so its value is not reset after being set.)
function focusMessage(msgid, convId, convType, entityId, entityType){
  if(!convId||!msgid)return;
  focusMsgId=msgid;
  var targetMode=(entityType==='staff')?'staff':'contact';
  setMode(targetMode);
  var focusAttempts=0;
  var waitForEntity=function(){
    if(++focusAttempts>40)return; // ~8s cap; give up gracefully if the entity never appears
    var entityBody=document.getElementById('entity-body');
    var els=entityBody?entityBody.querySelectorAll('.entity-item'):[];
    var found=false;
    Array.prototype.forEach.call(els,function(el){
      if(el.dataset.id===entityId){onEntityClick(el);found=true;}
    });
    if(found){
      var waitForConv=setInterval(function(){
        var convBody=document.getElementById('conv-body');
        var cards=convBody?convBody.querySelectorAll('.conv-card'):[];
        var cfound=false;
        Array.prototype.forEach.call(cards,function(card){
          if(card.dataset.id===convId){onConvClick(card);cfound=true;}
        });
        if(cfound){clearInterval(waitForConv);focusPending=true;}
        setTimeout(function(){clearInterval(waitForConv);},8000);
      },100);
    }else{
      setTimeout(waitForEntity,200);
    }
  };
  setTimeout(waitForEntity,200);
}
function focusCheckRow(){
  if(!focusMsgId)return;
  var row=document.querySelector('[data-msgid="'+focusMsgId+'"]');
  if(row){
    row.scrollIntoView({behavior:'smooth',block:'center'});
    row.classList.add('target-flash');
    row.addEventListener('animationend',function(){row.classList.add('target-active');},{once:true});
    showFocusBanner();
    return;
  }
  if(timelineHasOlder){
    var requestConvId=timelineConvId, gen=timelineRequestGen, before=timelineNextBefore;
    fetchOlderMessages(requestConvId,before).then(function(){
      if(timelineConvId!==requestConvId||timelineRequestGen!==gen)return;
      renderTimeline(false);
      setTimeout(focusCheckRow,250);
    }).catch(function(){
      if(timelineConvId!==requestConvId||timelineRequestGen!==gen)return;
    });
  }
}
function showFocusBanner(){
  if(document.getElementById('focus-banner'))return;
  var b=document.createElement('div');
  b.id='focus-banner';
  b.style.cssText='position:fixed;left:50%;top:8px;transform:translateX(-50%);z-index:300;background:#1890ff;color:#fff;padding:.35rem .8rem;border-radius:4px;font-size:.8rem;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.2)';
  b.textContent='← 返回搜索结果';
  b.onclick=function(){history.back();};
  document.body.appendChild(b);
}
function readFocusFromUrl(){
  var p=new URLSearchParams(location.search);
  var f=p.get('focus');
  if(!f)return;
  focusMessage(f,p.get('conv'),p.get('convType'),p.get('entityId'),p.get('entityType'));
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
document.getElementById('search-input').addEventListener('keydown',function(e){
  if(e.key==='Enter'){
    var v=this.value.trim();
    if(v){e.preventDefault();window.location.href='/admin/search?q='+encodeURIComponent(v);}
  }
});
