var currentTenantId=null,currentUserId=null,authMePromise=null;
function loadCurrentUser(){
  authMePromise=fetch('/api/auth/me').then(function(r){return r.json();}).then(function(d){
    if(!d.authenticated){window.location.href='/admin/login';return;}
    currentTenantId=d.tenant_id||null;
    currentUserId=d.id||null;
    // RND-297: server preferences take precedence; I18N/theme localStorage
    // remains the fallback until an authenticated preference is available.
    if(d.locale&&typeof I18N!=='undefined')I18N.setLocale(d.locale);
    if(d.theme)document.documentElement.setAttribute('data-theme',d.theme);
    if(typeof applyLocale==='function')applyLocale();
    var el=document.getElementById('current-user');
    if(el)el.textContent=d.display_name||d.wecom_user_id||'';
  }).catch(function(){});
  return authMePromise;
}
function doLogout(){
  fetch('/api/auth/logout',{method:'POST'}).then(function(){
    window.location.href='/admin/login';
  }).catch(function(){window.location.href='/admin/login';});
}
function fetchSyncStatus(){
  return fetch('/api/admin/sync-status').then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  });
}
// QA fix (search-then-select race): returns the fetch promise so callers
// that need to act AFTER the entity list has actually rendered (e.g.
// setMode()/onSearchContactItemClick()) can chain off it instead of
// reading lastEntityItems synchronously right after calling this --
// lastEntityItems is stale (whatever the PREVIOUS mode last loaded, or
// null) until this promise resolves.
function loadEntityList(){
  var requestMode=mode;
  // The console does not render conversation_count in its member picker.
  // Avoid making page entry wait for an archive-wide count aggregation that
  // the user cannot see.
  var url=requestMode==='staff'?'/api/monitored-accounts?include_conversation_count=false':'/api/contacts';
  document.getElementById('entity-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
  // Do not serialize the member-list request behind /api/auth/me.  The
  // server independently authenticates this request; auth/me is only
  // needed later to read the tenant-and-user-scoped remembered selection.
  var authReady=(typeof authMePromise!=='undefined'&&authMePromise)?authMePromise:null;
  return fetch(url).then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(items){
    if(!items||mode!==requestMode)return;
    renderEntityList(items);
    // If the list wins the race, render it now and restore a persisted
    // multi-seat choice after the identity key becomes available.  A sole
    // staff seat is already selected immediately by renderEntityList().
    if(requestMode==='staff'&&authReady){
      authReady.then(function(){
        if(mode===requestMode&&!selEntityId&&lastEntityItems===items)maybeAutoSelectEntity(items);
      });
    }
  })
    .catch(function(){document.getElementById('entity-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadEntities')+'</div>';});
}
function loadConversations(entityId){
  var url=mode==='staff'
    ?'/api/conversations?mode=staff&staff_id='+encodeURIComponent(entityId)+'&include_participant_metadata=false'
    :'/api/conversations?mode=contact&contact_id='+encodeURIComponent(entityId);
  document.getElementById('conv-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
  fetch(url).then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(convs){if(convs)renderConvList(convs);})
    .catch(function(){document.getElementById('conv-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadConversations')+'</div>';});
}
// Archive Console v2: 会话信息 panel tab data. Additive endpoint (RND-none —
// this UI redesign has no ticket number) -- see routers/conversations.py's
// get_conversation_detail. Failures/staleness are guarded by requestConvId
// so a slow response from a conversation the user has since navigated away
// from never overwrites the panel for the CURRENT conversation.
function loadConversationDetail(convId){
  var requestConvId=convId;
  renderPanelInfoLoading();
  fetch('/api/conversations/'+encodeURIComponent(convId)+'/detail')
    .then(function(r){if(handleUnauth(r))return null;if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(function(detail){
      if(!detail||selConvId!==requestConvId)return;
      convDetailCache[requestConvId]=detail;
      renderPanelInfo(detail);
    })
    .catch(function(){
      if(selConvId!==requestConvId)return;
      renderPanelInfoError();
    });
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
