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
// RND-323: staff selection preference is isolated by both tenant and user.
function _lastEntityStorageKey(){
  if(!(currentTenantId&&currentUserId))return null;
  return 'rnd.lastEntity.'+currentTenantId+'.'+currentUserId;
}
function persistLastEntity(id){
  var key=_lastEntityStorageKey();
  if(!id||!key)return;
  try{localStorage.setItem(key,id);}catch(e){}
}
function readLastEntity(){
  var key=_lastEntityStorageKey();
  if(!key)return null;
  try{return localStorage.getItem(key);}catch(e){return null;}
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
  if(mode==='staff'&&!selEntityId)maybeAutoSelectEntity(items);
}
// RND-323: reuse the regular selection path so auto-selection has identical
// conversation and timeline loading side effects to a manual click.
function maybeAutoSelectEntity(items){
  if(mode!=='staff'||selEntityId||(typeof searchSelectionInProgress!=='undefined'&&searchSelectionInProgress)||!items||!items.length)return;
  if(items.length===1){
    selectEntityIfPresent(items[0].staff_id);
    return;
  }
  var last=readLastEntity();
  if(last&&items.some(function(it){return it.staff_id===last;}))selectEntityIfPresent(last);
}
function onEntityClick(el){
  selEntityId=el.dataset.id; selEntityName=el.dataset.name; selConvId=null; selConvName=null;
  if(mode==='staff'&&typeof persistLastEntity==='function')persistLastEntity(selEntityId);
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.querySelectorAll('.entity-item').forEach(function(e){e.classList.remove('active');});
  el.classList.add('active');
  document.getElementById('conv-header').textContent=I18N.t('nav.conversations')+' — '+el.dataset.name;
  document.getElementById('timeline-header').textContent=I18N.t('console.timelineHeader');
  document.getElementById('timeline-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  updateScopeButton(el.dataset.name);
  closeScopePopover();
  resetPanelForNoConversation();
  loadConversations(selEntityId);
}
// Archive Console v2: the "监控范围" popover trigger button reflects the
// currently selected entity instead of a permanent always-visible column.
function updateScopeButton(name){
  var label=document.getElementById('scope-label');
  var avatar=document.getElementById('scope-avatar');
  // QA fix: the separator used to be a hardcoded full-width Chinese colon
  // ('：') concatenated in JS regardless of locale -- every other
  // "label + value" string in this codebase bakes its own locale-correct
  // separator into the translated string itself (e.g. audioArchive.endedAt
  // ends in "：" for zh but would need ": " for en), never appends one in
  // JS. These two keys follow that convention.
  if(label)label.textContent=(mode==='staff'?I18N.t('console.scopeLabelStaffPrefix'):I18N.t('console.scopeLabelContactPrefix'))+name;
  if(avatar)avatar.textContent=(name||'?').charAt(0).toUpperCase();
}
function closeScopePopover(){
  scopePopoverOpen=false;
  var pop=document.getElementById('scope-popover');
  if(pop)pop.style.display='none';
}
// Archive Console v2: client-side 全部/群聊/单聊 filter over the
// already-fetched conversation list -- no new API call, ConversationOut
// already carries conversation_type.
function applyConvTypeFilter(convs){
  if(convTypeFilter==='all')return convs||[];
  return (convs||[]).filter(function(c){return c.conversation_type===convTypeFilter;});
}
function setConvTypeFilter(type){
  convTypeFilter=type;
  document.querySelectorAll('#conv-type-tabs .ctab').forEach(function(el){
    el.classList.toggle('active',el.dataset.type===type);
  });
  if(lastConvItems)renderConvList(lastConvItems);
}
function renderConvList(convs){
  lastConvItems=convs;
  lastConvSig=convListSignature(convs);
  var body=document.getElementById('conv-body');
  var visible=applyConvTypeFilter(convs);
  if(!convs||!convs.length){body.innerHTML='<div class="empty-state">'+I18N.t('console.noConversations')+'</div>';return;}
  if(!visible.length){body.innerHTML='<div class="empty-state">'+I18N.t('console.noConversations')+'</div>';return;}
  var html='';
  visible.forEach(function(c){
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
  clearPanelForNewConversation();
  loadTimeline(selConvId, el.dataset.type);
}
// Archive Console v2: switching conversations invalidates any previously
// selected message (it belongs to the OLD conversation's timeline) and the
// stale 会话信息 stats/participants for the previous conversation. A real
// fetch (loadConversationDetail) always follows this immediately, so
// "loading" is the correct, truthful state here.
function clearPanelForNewConversation(){
  selectedMsgId=null;
  renderPanelAuditEmpty();
  renderPanelInfoLoading();
}
// QA fix: used when an entity/scope is selected (or a mode switch happens)
// but no conversation has been chosen yet -- nothing is actually loading
// at this point, so showing the "加载中" spinner (as
// clearPanelForNewConversation above does) left it stuck forever until a
// conversation was eventually clicked. This shows the honest
// "select a conversation" empty state instead, and also clears any
// selected-message audit info left over from a previous entity/mode.
function resetPanelForNoConversation(){
  selectedMsgId=null;
  renderPanelAuditEmpty();
  var body=document.getElementById('panel-info-body');
  if(body)body.innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
}

// ---------------------------------------------------------------------------
// Archive Console v2 — 会话信息 panel tab. Backed by
// GET /api/conversations/{id}/detail (api-client.js's loadConversationDetail),
// additive to the existing listing/timeline endpoints. Participants are
// explicitly labeled as inferred from archived messages, never presented as
// a live WeCom room roster (this system has no such sync) -- see
// panel.participantsNote.
// ---------------------------------------------------------------------------
function renderPanelInfoLoading(){
  var body=document.getElementById('panel-info-body');
  if(body)body.innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
}
function renderPanelInfoError(){
  var body=document.getElementById('panel-info-body');
  if(body)body.innerHTML='<div class="error-msg">'+I18N.t('panel.detailFailed')+'</div>';
}
function renderPanelInfo(detail){
  var body=document.getElementById('panel-info-body');
  if(!body)return;
  // media_available_percent is deliberately not shown: computing it
  // correctly needs a join against MediaFile.download_status per message,
  // which this additive endpoint intentionally keeps out of scope rather
  // than adding a second, more expensive query path -- see
  // get_conversation_detail's docstring in routers/conversations.py.
  var stats=[
    [I18N.t('panel.stat.messageCount'),detail.message_count],
    [I18N.t('panel.stat.decrypted'),Math.round((detail.decrypted_percent||0)*10)/10+'%'],
    [I18N.t('panel.stat.participants'),(detail.participants||[]).length]
  ];
  var statsHtml=stats.map(function(s){
    return '<div class="panel-stat"><div class="panel-stat-k">'+esc(s[0])+'</div><div class="panel-stat-v">'+esc(s[1])+'</div></div>';
  }).join('');
  var participants=detail.participants||[];
  var av=function(name,i){
    var colors=['#1677ff','#0f9d58','#e5844d','#8b5cf6','#0891b2','#d4436b'];
    return 'width:22px;height:22px;border-radius:4px;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;font-size:9px;background:'+colors[i%colors.length];
  };
  var participantsHtml=participants.map(function(p,i){
    var name=p.display_name||p.raw_id||p.id;
    var tagCls=p.role==='staff'?'panel-participant-tag-staff':'panel-participant-tag-contact';
    var tagLabel=p.role==='staff'?I18N.t('panel.role.staff'):I18N.t('panel.role.contact');
    return '<div class="panel-participant"><div style="'+av(name,i)+'">'+esc((name||'?').charAt(0).toUpperCase())+'</div>'
      +'<span class="panel-participant-name">'+esc(name)+'</span>'
      +'<span class="panel-participant-tag '+tagCls+'">'+esc(tagLabel)+'</span></div>';
  }).join('');
  body.innerHTML='<div class="panel-section"><div class="panel-section-title">'+esc(I18N.t('panel.stat.messageCount'))+'</div>'
    +'<div class="panel-stats">'+statsHtml+'</div></div>'
    +'<div class="panel-section"><div class="panel-section-title">'+esc(I18N.t('panel.participantsTitle'))+'</div>'
    +participantsHtml
    +'<div class="panel-participants-note">'+esc(I18N.t('panel.participantsNote'))+'</div></div>';
}
