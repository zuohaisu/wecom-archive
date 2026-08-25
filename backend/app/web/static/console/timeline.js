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
      if(data.room_display_name){
        selConvName=data.room_display_name;
        document.getElementById('timeline-header').textContent=I18N.t('console.timelineHeader')+' — '+data.room_display_name;
      }
      timelineMsgs=before?data.messages.concat(timelineMsgs):data.messages;
      timelineHasOlder=data.pagination.has_older;
      timelineNextBefore=data.pagination.next_before;
      renderTimeline(isInitial);
      startHistoryObserver();
      // The stats/participant panel can require an aggregate across an
      // entire long conversation.  Start it only after the first message
      // page has rendered, so it cannot contend with the time-to-chat path.
      if(isInitial){
        setTimeout(function(){
          if(timelineConvId===requestConvId&&timelineRequestGen===gen&&typeof loadConversationDetail==='function')loadConversationDetail(requestConvId);
        },0);
      }
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
    if(typeof window!=='undefined'&&window.ProductAnalytics)window.ProductAnalytics.track('product.conversation.older_messages_loaded.v1');
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
      m.sender_display_name,m.sender_raw_id,m.sender_avatar_url,m.sender_avatar_status,rcptSig,
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
  // Archive Console v2 (Message Types visual refresh): only text and
  // available voice keep the classic chat-bubble chrome (padding/border/
  // background). Every other renderer (bare image/video/emotion, file/
  // link/structured/system/composite cards, and every graded media-
  // unavailable placeholder) already supplies its own box -- wrapping
  // those in .tl-bubble too produced a redundant double-boxed look.
  var isRevokeStandalone=!!(m.revoke_association_status&&m.revoke_association_status!=='linked');
  var isUnavailableMedia=!!(m.media_status&&m.media_status!=='available'&&m.media_type&&m.media_type!=='text');
  var wrapInBubble=!isRevokeStandalone&&(m.media_type==='text'||(m.media_type==='voice'&&!isUnavailableMedia));
  var bodyHtml=wrapInBubble?('<div class="'+bc+'">'+text+'</div>'):('<div class="tl-bare">'+text+'</div>');
  if(selectedMsgId===m.msgid)rowCls+=' audit-selected';
  var mt=(auditMode&&m.msgtype&&m.msgtype!=='text')?' <span class="badge badge-count" style="font-size:.67rem">'+esc(m.msgtype)+'</span>':'';
  var auditLine=auditMode?'<div class="tl-audit-line">'+esc(m.msgid)+' · '+esc(m.normalized_type||'-')+' · '+esc(m.msgtype||'-')+'</div>':'';
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
  var senderAvatar=(typeof ArchiveAvatar!=='undefined'&&ArchiveAvatar.html)
    ?ArchiveAvatar.html(m.sender_avatar_url,senderName,'tl-avatar')
    :'<span class="tl-avatar">'+esc((senderName||'?').charAt(0).toUpperCase())+'</span>';
  var rcptNames=(m.recipient_display_names&&m.recipient_display_names.length)?m.recipient_display_names:(m.recipients||[]);
  var rcpt='';
  if(m.roomid){
    if(rcptNames.length){
      rcpt='<div class="tl-rcpt">'+I18N.t('timeline.groupChat')+' · '+rcptNames.length+' '+(rcptNames.length===1?I18N.t('timeline.participant'):I18N.t('timeline.participants'))+'</div>';
    }
  }else if(rcptNames.length){
    rcpt='<div class="tl-rcpt">→ '+esc(rcptNames.join(', '))+'</div>';
  }
  // RND-363: a leading select checkbox appears only while delete mode is
  // active. It is a real <input type=checkbox> with an aria-label (never
  // message content) so screen readers and keyboard users can operate it.
  var deleteCheck='';
  if(typeof deleteMode!=='undefined'&&deleteMode){
    var checked=deleteSelection[m.msgid]?' checked':' ';
    deleteCheck='<label class="tl-delete-checkbox" aria-hidden="false"><input class="tl-delete-check" type="checkbox" aria-label="'+esc(I18N.t('delete.selectMessage'))+'"'+checked+'onclick="event.stopPropagation();toggleMessageForDelete(&quot;'+esc(m.msgid)+'&quot;)" onkeydown="if(event.key===\'Enter\'){event.preventDefault();event.stopPropagation();toggleMessageForDelete(&quot;'+esc(m.msgid)+'&quot;)}"></label>';
  }
  return '<div class="'+rowCls+'" data-msgid="'+esc(m.msgid)+'" data-msgsig="'+esc(timelineSignature([m]))+'" onclick="selectMessageForAudit(&quot;'+esc(m.msgid)+'&quot;)">'
    +'<div class="tl-meta">'+deleteCheck+senderAvatar+'<span class="'+sc+'">'+esc(senderName)+'</span>'+senderSecondary
    +' <span class="tl-time">'+esc(fmtTime(m.msgtime))+'</span>'+mt+grp+revokedBadge+'</div>'
    +bodyHtml
    +rcpt+auditLine+'</div>';
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

// ---------------------------------------------------------------------------
// Archive Console v2 — 消息详情 (audit) panel tab. Entirely frontend-only:
// every field rendered here already exists on the TimelineMessageOut row
// (see backend/app/schemas/timeline.py) -- no new API call. selectedMsgId/
// setPanelTab live in console-state.js/console-entry.js respectively.
// ---------------------------------------------------------------------------
// Shared by selectMessageForAudit() and applyLocale() (the latter needs to
// re-render the currently-selected message's audit fields in the new
// language after a locale switch).
function findTimelineMessage(msgid){
  for(var i=0;i<timelineMsgs.length;i++){
    if(timelineMsgs[i].msgid===msgid)return timelineMsgs[i];
  }
  return null;
}
function selectMessageForAudit(msgid){
  selectedMsgId=msgid;
  document.querySelectorAll('[data-msgid]').forEach(function(el){
    el.classList.toggle('audit-selected',el.getAttribute('data-msgid')===msgid);
  });
  var m=findTimelineMessage(msgid);
  if(!panelOpen)togglePanel();
  setPanelTab('audit');
  if(m)renderPanelAudit(m);
}
function renderPanelAuditEmpty(){
  var body=document.getElementById('panel-audit-body');
  if(body)body.innerHTML='<div class="empty-state">'+I18N.t('panel.noSelection')+'</div>';
}
function renderPanelAudit(m){
  var body=document.getElementById('panel-audit-body');
  if(!body)return;
  var preview=m.content_text
    ||(m.structured_content&&m.structured_content.fields&&(m.structured_content.fields.title||m.structured_content.fields.name))
    ||('['+(m.normalized_type||m.msgtype||'?')+']');
  var rows=[
    [I18N.t('panel.field.msgid'),m.msgid],
    [I18N.t('panel.field.rawMsgtype'),m.msgtype||'-'],
    [I18N.t('panel.field.normalized'),m.normalized_type||'-'],
    [I18N.t('panel.field.support'),m.support_status||'-'],
    [I18N.t('panel.field.renderer'),m.renderer_strategy||'-'],
    [I18N.t('panel.field.mediaStatus'),m.media_status||'—'],
    [I18N.t('panel.field.sender'),m.sender_raw_id||m.sender||'—'],
    [I18N.t('panel.field.conversation'),m.roomid||selConvId||'—'],
    [I18N.t('panel.field.msgtime'),fmtTime(m.msgtime)],
    [I18N.t('panel.field.revoked'),m.is_revoked?('true · '+fmtTime(m.revoked_at)):'false']
  ];
  var rowsHtml=rows.map(function(r){
    return '<div class="panel-audit-row"><span class="panel-audit-k">'+esc(r[0])+'</span><span class="panel-audit-v">'+esc(r[1])+'</span></div>';
  }).join('');
  body.innerHTML='<div class="panel-audit-preview">'+esc(preview)+'</div>'
    +rowsHtml
    +'<div class="panel-audit-actions"><button type="button" class="panel-copy-btn" onclick="copyMsgid()">'+esc(I18N.t('panel.copyMsgid'))+'</button> '
    +'<button type="button" class="panel-copy-btn" onclick="openSelectedMessageExport()">'+esc(I18N.t('exports.openMessage'))+'</button></div>';
}
function copyMsgid(){
  if(!selectedMsgId)return;
  if(typeof navigator!=='undefined'&&navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(selectedMsgId).catch(function(){});
  }
}
