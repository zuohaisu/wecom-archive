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
function updateSyncStatus(){
  var el=document.getElementById('sync-status');
  var button=document.getElementById('btn-sync-now');
  if(button)button.disabled=syncInProgress;
  if(!el)return;
  var text='',isError=false;
  if(syncStatusNotice==='failed'){
    text=I18N.t('sync.failed');
    isError=true;
  }else if(syncStatusNotice==='already_running'){
    text=I18N.t('sync.queued');
  }else if(syncStatusNotice==='rate_limited'){
    text=I18N.t('sync.rateLimited');
  }else if(syncStatus&&syncStatus.status==='syncing'){
    text=I18N.t('sync.inProgress');
  }else if(syncStatus&&syncStatus.status==='error'){
    text=I18N.t('sync.failed');
    isError=true;
  }else if(syncStatus&&syncStatus.startTime){
    text=I18N.t('sync.lastSync').replace('{time}',fmtTime(Date.parse(syncStatus.startTime)));
  }else{
    text=I18N.t('sync.noData');
  }
  el.textContent=text;
  el.className='sync-status'+(isError?' sync-status-error':'');
}
function _recordSyncStatus(data){
  if(!data)return false;
  var version=Number(data.seqVersion||0);
  var versionChanged=lastSeenSyncVersion!==null&&version!==lastSeenSyncVersion;
  lastSeenSyncVersion=version;
  syncStatus=data;
  syncInProgress=data.status==='syncing';
  if(syncStatusNotice==='already_running'&&data.status!=='syncing')syncStatusNotice=null;
  if(syncStatusNotice==='rate_limited'&&data.status!=='idle')syncStatusNotice=null;
  updateSyncStatus();
  if(syncInProgress){
    startSyncStatusPolling();
  }else{
    stopSyncStatusPolling();
  }
  return versionChanged;
}
function pollSyncStatus(){
  if(syncStatusRequestInFlight)return Promise.resolve(false);
  syncStatusRequestInFlight=true;
  return fetchSyncStatus().then(function(data){
    syncStatusRequestInFlight=false;
    return _recordSyncStatus(data);
  }).catch(function(){
    syncStatusRequestInFlight=false;
    syncStatusNotice='failed';
    updateSyncStatus();
    return false;
  });
}
function startSyncStatusPolling(){
  if(syncStatusPollTimer)return;
  syncStatusPollTimer=setInterval(function(){
    if(!document.hidden){
      pollSyncStatus().then(function(versionChanged){
        if(versionChanged)refreshForSyncVersion();
      });
    }
  },2000);
}
function stopSyncStatusPolling(){
  if(!syncStatusPollTimer)return;
  clearInterval(syncStatusPollTimer);
  syncStatusPollTimer=null;
}
function refreshData(){
  var tasks=[refreshEntityList().catch(function(){})];
  if(selEntityId)tasks.push(refreshConversationList().catch(function(){}));
  if(timelineConvId)tasks.push(refreshTimelineIfSelected().catch(function(){}));
  return Promise.all(tasks);
}
function finishRefresh(){
  refreshInFlight=false;
}
function refreshForSyncVersion(){
  if(refreshInFlight)return;
  refreshInFlight=true;
  refreshData().then(finishRefresh,finishRefresh);
}
function syncNow(){
  if(syncInProgress)return;
  syncInProgress=true;
  syncStatusNotice=null;
  updateSyncStatus();
  return fetch('/api/admin/sync-now',{method:'POST'}).then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(function(data){
    if(!data)return;
    syncStatusNotice=data.accepted?null:data.message;
    if(data.message==='already_running')syncInProgress=true;
    if(data.message==='rate_limited')syncInProgress=false;
    updateSyncStatus();
    if(syncInProgress)startSyncStatusPolling();
    return pollSyncStatus().then(function(versionChanged){
      if(versionChanged)refreshForSyncVersion();
    });
  }).catch(function(){
    syncInProgress=false;
    syncStatusNotice='failed';
    updateSyncStatus();
  });
}
function initializeSyncStatus(){
  pollSyncStatus().then(function(versionChanged){
    if(versionChanged)refreshForSyncVersion();
  });
}
