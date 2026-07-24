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
