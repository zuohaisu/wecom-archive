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
