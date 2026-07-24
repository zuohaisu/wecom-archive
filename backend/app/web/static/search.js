
var MSGTYPE_OPTIONS=RND216_MSGTYPE_OPTIONS;
var KEYWORD='';
var ALL=[];
var FILTERS={dateRange:null,user:[],staff:[],msgtype:[]};
var OPEN_POPOVER=null;
var PARTICIPANT_OPTIONS={contact:{},staff:{}};
var PARTICIPANT_LABELS={contact:{},staff:{}};
var DATE_RANGE_KEYS=[['','search.filter.dateAll'],['1d','search.filter.date1d'],['7d','search.filter.date7d'],['30d','search.filter.date30d'],['90d','search.filter.date90d']];
var MSGTYPE_RAW_BY_NORMALIZED={};
MSGTYPE_OPTIONS.forEach(function(opt){MSGTYPE_RAW_BY_NORMALIZED[opt.normalizedType]=opt.rawValues;});
function msgtypeOptionLabel(normalizedType){
  for(var i=0;i<MSGTYPE_OPTIONS.length;i++){
    if(MSGTYPE_OPTIONS[i].normalizedType===normalizedType)return I18N.t(MSGTYPE_OPTIONS[i].labelKey);
  }
  return normalizedType;
}
function applyStaticI18n(){
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent=I18N.t(el.getAttribute('data-i18n'));
  });
}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function getParam(n){return new URLSearchParams(location.search).get(n);}
function fmtTime(ms){if(!ms)return '';var d=new Date(ms);var p=function(n){return String(n).padStart(2,'0');};return (d.getMonth()+1)+'月'+d.getDate()+'日 '+p(d.getHours())+':'+p(d.getMinutes());}
function msgtypeLabel(t){var m={text:'文本',image:'图片',voice:'语音',video:'视频',file:'文件',link:'链接/卡片',system:'系统消息',emotion:'表情',chatrecord:'聊天记录',redpacket:'红包',miniprogram:'小程序'};return m[t]||t||'文本';}
function escapeRegExp(s){var bs=String.fromCharCode(92);var special='.*+?^${}()|[]';var out='';for(var i=0;i<s.length;i++){var c=s[i];if(special.indexOf(c)>=0)out+=bs+c;else out+=c;}return out;}
function highlight(text){if(!KEYWORD)return esc(text);try{var re=new RegExp('('+escapeRegExp(KEYWORD)+')','gi');return esc(text).replace(re,'<mark>$1</mark>');}catch(e){return esc(text);}}
function loadCurrentUser(){
  fetch('/api/auth/me').then(function(r){return r.json();}).then(function(d){
    if(!d.authenticated){window.location.href='/admin/login';return;}
    var el=document.getElementById('current-user');
    if(el)el.textContent=d.display_name||d.wecom_user_id||'';
  }).catch(function(){});
}
function doLogout(){
  fetch('/api/auth/logout',{method:'POST'}).then(function(){window.location.href='/admin/login';}).catch(function(){window.location.href='/admin/login';});
}
function doTopSearch(e){e.preventDefault();var v=document.getElementById('topSearch').value.trim();if(v)window.location.href='/admin/search?q='+encodeURIComponent(v);return false;}
function showError(){document.getElementById('resultsList').innerHTML='';document.getElementById('stateEmpty').style.display='none';document.getElementById('stateError').style.display='block';}
function showEmpty(hint){document.getElementById('resultsList').innerHTML='';document.getElementById('stateError').style.display='none';document.getElementById('stateEmpty').style.display='block';if(hint)document.getElementById('emptyHint').textContent=hint;}
function setLoading(){document.getElementById('stateEmpty').style.display='none';document.getElementById('stateError').style.display='none';var sk='';for(var i=0;i<5;i++){sk+='<div class="skeleton"><div class="sk-line mid"></div><div class="sk-line"></div><div class="sk-line short"></div></div>';}document.getElementById('resultsList').innerHTML=sk;}
var MAX_RESULTS=1000;

// ---- RND-230: filter bar (date / user / staff / msgtype) ----------------

function participantLabelFor(r,id){
  return (id===r.sender && r.sender_display_name)?r.sender_display_name:id;
}
function setParticipant(map,id,label){
  // Never let a raw-id fallback (no display name available from THIS row)
  // clobber a real display name already found for the same id from a
  // different row — whichever row is scanned last must not erase a
  // better label a previous row already supplied.
  if(!id)return;
  if(!(id in map)||map[id]===id)map[id]=label;
}
function collectParticipants(kind){
  // contact_ids/staff_ids are the message's full tenant-scoped participant
  // sets (sender + every recipient, split by is_staff — see
  // _derive_conversation_membership), not just the single `entity_id`
  // picked for navigation. Using them directly (rather than inferring
  // from entity_id/entity_type, which only ever names ONE side) is what
  // makes a group message with several contacts, or a staff-authored
  // message whose contacts never appear as `sender`, still populate the
  // right filter options.
  var field=kind==='staff'?'staff_ids':'contact_ids';
  var map={};
  ALL.forEach(function(r){
    (r[field]||[]).forEach(function(id){
      setParticipant(map,id,participantLabelFor(r,id));
    });
  });
  return map;
}
function refreshParticipantCache(){
  // PARTICIPANT_OPTIONS reflects only the current result set (popover
  // choices narrow with filters, per design). PARTICIPANT_LABELS
  // accumulates across the session so a chip for an already-selected
  // participant keeps its display name even if a later filter narrows
  // that participant out of the visible results.
  PARTICIPANT_OPTIONS.contact=collectParticipants('contact');
  PARTICIPANT_OPTIONS.staff=collectParticipants('staff');
  Object.keys(PARTICIPANT_OPTIONS.contact).forEach(function(id){setParticipant(PARTICIPANT_LABELS.contact,id,PARTICIPANT_OPTIONS.contact[id]);});
  Object.keys(PARTICIPANT_OPTIONS.staff).forEach(function(id){setParticipant(PARTICIPANT_LABELS.staff,id,PARTICIPANT_OPTIONS.staff[id]);});
}
function participantLabel(kind,id){
  var map=PARTICIPANT_LABELS[kind]||{};
  return map[id]||id;
}
function activeFilterCount(){
  return (FILTERS.dateRange?1:0)+FILTERS.user.length+FILTERS.staff.length+FILTERS.msgtype.length;
}
function hasActiveFilters(){
  return activeFilterCount()>0;
}
function setBadge(kind,count){
  var btn=document.getElementById('fbtn-'+kind);
  var badge=document.getElementById('fbadge-'+kind);
  if(!btn||!badge)return;
  if(count>0){btn.classList.add('active');badge.textContent=String(count);}
  else{btn.classList.remove('active');badge.textContent='';}
}
function renderChips(){
  var chips=[];
  if(FILTERS.dateRange){
    var dateOptLabel='';
    for(var i=0;i<DATE_RANGE_KEYS.length;i++){if(DATE_RANGE_KEYS[i][0]===FILTERS.dateRange)dateOptLabel=I18N.t(DATE_RANGE_KEYS[i][1]);}
    chips.push({kind:'date',value:'',label:I18N.t('search.filter.date')+': '+(dateOptLabel||FILTERS.dateRange)});
  }
  FILTERS.user.forEach(function(id){chips.push({kind:'user',value:id,label:I18N.t('search.filter.user')+': '+participantLabel('contact',id)});});
  FILTERS.staff.forEach(function(id){chips.push({kind:'staff',value:id,label:I18N.t('search.filter.staff')+': '+participantLabel('staff',id)});});
  FILTERS.msgtype.forEach(function(t){chips.push({kind:'msgtype',value:t,label:I18N.t('search.filter.msgtype')+': '+msgtypeOptionLabel(t)});});
  var box=document.getElementById('filterChips');
  if(!chips.length){box.style.display='none';box.innerHTML='';return;}
  box.style.display='flex';
  var html=chips.map(function(c){
    return '<span class="chip">'+esc(c.label)+'<button type="button" class="chip-x" data-kind="'+esc(c.kind)+'" data-value="'+esc(c.value)+'" aria-label="'+esc(I18N.t('search.filter.clear'))+'">×</button></span>';
  }).join('')+'<button type="button" class="filter-clear-all" id="btnClearAllFilters">'+esc(I18N.t('search.filter.clearAll'))+'</button>';
  box.innerHTML=html;
  Array.prototype.forEach.call(box.querySelectorAll('.chip-x'),function(btn){
    btn.addEventListener('click',function(){removeFilter(btn.getAttribute('data-kind'),btn.getAttribute('data-value')||'');});
  });
  var clearBtn=document.getElementById('btnClearAllFilters');
  if(clearBtn)clearBtn.addEventListener('click',clearAllFilters);
}
function updateFilterUI(){
  setBadge('date',FILTERS.dateRange?1:0);
  setBadge('user',FILTERS.user.length);
  setBadge('staff',FILTERS.staff.length);
  setBadge('msgtype',FILTERS.msgtype.length);
  renderChips();
}
function closeAllPopovers(){
  ['date','user','staff','msgtype'].forEach(function(k){
    var p=document.getElementById('fpop-'+k);
    if(p)p.classList.remove('open');
  });
  OPEN_POPOVER=null;
  document.removeEventListener('click',onDocClickClosePopover,true);
}
function onDocClickClosePopover(e){
  if(OPEN_POPOVER && e.target && (!e.target.closest || !e.target.closest('.filter-group'))){
    closeAllPopovers();
  }
}
function toggleFilterPopover(kind){
  if(OPEN_POPOVER===kind){closeAllPopovers();return;}
  closeAllPopovers();
  if(kind==='date'){renderDatePopover();}
  else if(kind==='msgtype'){renderMsgtypePopover();}
  else{renderMultiPopover(kind);}
  var p=document.getElementById('fpop-'+kind);
  if(p)p.classList.add('open');
  OPEN_POPOVER=kind;
  setTimeout(function(){document.addEventListener('click',onDocClickClosePopover,true);},0);
}
function renderDatePopover(){
  var html='';
  DATE_RANGE_KEYS.forEach(function(opt){
    var checked=(FILTERS.dateRange||'')===opt[0]?' checked':'';
    html+='<label class="filter-opt"><input type="radio" name="fdate" class="fp-date-radio" data-value="'+esc(opt[0])+'"'+checked+'> '+esc(I18N.t(opt[1]))+'</label>';
  });
  var el=document.getElementById('fpop-date');
  el.innerHTML=html;
  Array.prototype.forEach.call(el.querySelectorAll('.fp-date-radio'),function(r){
    r.addEventListener('change',function(){if(r.checked){setDateFilter(r.getAttribute('data-value'));}});
  });
}
function setDateFilter(v){
  FILTERS.dateRange=v||null;
  closeAllPopovers();
  applyFilters();
}
function renderMultiPopover(kind){
  var entityType=kind==='user'?'contact':'staff';
  var participants=PARTICIPANT_OPTIONS[entityType]||{};
  var ids=Object.keys(participants).sort(function(a,b){
    return (participants[a]||a).localeCompare(participants[b]||b);
  });
  var selected=FILTERS[kind];
  var html='<input type="text" class="fp-search" placeholder="搜索…">';
  html+='<div class="fp-options">';
  if(!ids.length){
    html+='<div class="filter-opt-empty">当前结果中暂无可选项</div>';
  }else{
    ids.forEach(function(id){
      var checked=selected.indexOf(id)>=0?' checked':'';
      var label=participants[id]||id;
      html+='<label class="filter-opt" data-label="'+esc(label.toLowerCase())+'"><input type="checkbox" class="fp-check" data-kind="'+esc(kind)+'" data-value="'+esc(id)+'"'+checked+'> '+esc(label)+'</label>';
    });
  }
  html+='</div>';
  var el=document.getElementById('fpop-'+kind);
  el.innerHTML=html;
  Array.prototype.forEach.call(el.querySelectorAll('.fp-check'),function(cb){
    cb.addEventListener('change',function(){
      toggleMultiFilter(cb.getAttribute('data-kind'),cb.getAttribute('data-value'),cb.checked);
    });
  });
  var searchBox=el.querySelector('.fp-search');
  if(searchBox){
    searchBox.addEventListener('input',function(){filterPopoverOptions(searchBox,el);});
  }
}
function filterPopoverOptions(input,container){
  var q=input.value.trim().toLowerCase();
  Array.prototype.forEach.call(container.querySelectorAll('.filter-opt[data-label]'),function(el){
    el.style.display=el.getAttribute('data-label').indexOf(q)>=0?'':'none';
  });
}
function renderMsgtypePopover(){
  var selected=FILTERS.msgtype;
  var html='';
  MSGTYPE_OPTIONS.forEach(function(opt){
    var checked=selected.indexOf(opt.normalizedType)>=0?' checked':'';
    html+='<label class="filter-opt"><input type="checkbox" class="fp-check" data-kind="msgtype" data-value="'+esc(opt.normalizedType)+'"'+checked+'> '+esc(I18N.t(opt.labelKey))+'</label>';
  });
  var el=document.getElementById('fpop-msgtype');
  el.innerHTML=html;
  Array.prototype.forEach.call(el.querySelectorAll('.fp-check'),function(cb){
    cb.addEventListener('change',function(){
      toggleMultiFilter(cb.getAttribute('data-kind'),cb.getAttribute('data-value'),cb.checked);
    });
  });
}
function toggleMultiFilter(kind,value,checked){
  var arr=FILTERS[kind];
  if(!arr)return;
  var idx=arr.indexOf(value);
  if(checked&&idx===-1)arr.push(value);
  if(!checked&&idx>=0)arr.splice(idx,1);
  applyFilters();
}
function removeFilter(kind,value){
  if(kind==='date'){FILTERS.dateRange=null;}
  else if(FILTERS[kind]){
    var idx=FILTERS[kind].indexOf(value);
    if(idx>=0)FILTERS[kind].splice(idx,1);
  }
  applyFilters();
}
function clearAllFilters(){
  FILTERS={dateRange:null,user:[],staff:[],msgtype:[]};
  applyFilters();
}
function parseListParam(name){
  return new URLSearchParams(location.search).getAll(name);
}
function syncUrlFromState(){
  var params=new URLSearchParams();
  if(KEYWORD)params.set('q',KEYWORD);
  if(FILTERS.dateRange)params.set('date_range',FILTERS.dateRange);
  FILTERS.user.forEach(function(id){params.append('user',id);});
  FILTERS.staff.forEach(function(id){params.append('staff',id);});
  FILTERS.msgtype.forEach(function(t){params.append('msgtype',t);});
  var qs=params.toString();
  history.replaceState(null,'',location.pathname+(qs?('?'+qs):''));
}
function runSearchOrShowHint(){
  // Guarded entry point for both init() and every filter mutation: q and
  // filters can both end up empty (e.g. clearing the last active filter
  // with no keyword typed), and doSearch() would then hit the backend's
  // 400 "no q, no filter" guard and land on the error state instead of
  // recovering to the normal empty-search hint.
  if(KEYWORD||hasActiveFilters()){
    doSearch();
  }else{
    ALL=[];
    refreshParticipantCache();
    document.getElementById('resultCount').textContent='';
    showEmpty('请输入关键词开始搜索');
  }
}
function applyFilters(){
  updateFilterUI();
  syncUrlFromState();
  runSearchOrShowHint();
}
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'&&OPEN_POPOVER){closeAllPopovers();}
});

// ---------------------------------------------------------------------------

function buildSearchParams(){
  var params=['limit=50'];
  if(KEYWORD)params.push('q='+encodeURIComponent(KEYWORD));
  if(FILTERS.dateRange)params.push('date_range='+encodeURIComponent(FILTERS.dateRange));
  FILTERS.user.forEach(function(id){params.push('user='+encodeURIComponent(id));});
  FILTERS.staff.forEach(function(id){params.push('staff='+encodeURIComponent(id));});
  FILTERS.msgtype.forEach(function(t){
    var raws=MSGTYPE_RAW_BY_NORMALIZED[t]||[t];
    raws.forEach(function(rv){params.push('msgtype='+encodeURIComponent(rv));});
  });
  return params;
}
function doSearch(){
  setLoading();
  var params=buildSearchParams();
  var all=[];
  function fetchPage(before){
    var url='/api/search/messages?'+params.join('&')+(before?('&before='+encodeURIComponent(before)):'');
    return fetch(url).then(function(r){
      if(r.status===401){window.location.href='/admin/login';return null;}
      if(!r.ok)throw new Error('HTTP '+r.status);
      return r.json();
    }).then(function(d){
      if(!d)return null;
      all=all.concat(d.results||[]);
      if(d.pagination&&d.pagination.next_before&&all.length<MAX_RESULTS){return fetchPage(d.pagination.next_before);}
      return all;
    });
  }
  fetchPage(null).then(function(results){
    if(results===null)return;
    ALL=results;
    refreshParticipantCache();
    updateFilterUI();
    render();
  }).catch(function(){showError();});
}
function render(){var list=document.getElementById('resultsList');var empty=document.getElementById('stateEmpty');var err=document.getElementById('stateError');empty.style.display='none';err.style.display='none';var rows=ALL.slice();var sort=document.getElementById('sortSel').value;rows=rows.slice().sort(function(a,b){return sort==='time_asc'?(a.msgtime||0)-(b.msgtime||0):(b.msgtime||0)-(a.msgtime||0);});document.getElementById('resultCount').textContent='共 '+rows.length+' 条消息匹配';if(rows.length===0){list.innerHTML='';empty.style.display='block';document.getElementById('emptyHint').textContent=hasActiveFilters()?'试试调整或清除筛选条件':'换个关键词试试';return;}list.innerHTML=rows.map(function(r){var badge=r.conversation_type==='group'?'<span class="rc-badge group">群聊</span>':'<span class="rc-badge direct">单聊</span>';var senderClass=r.entity_type==='staff'?'rc-sender staff':'rc-sender';var params=['focus='+encodeURIComponent(r.msgid),'conv='+encodeURIComponent(r.conversation_id),'convType='+encodeURIComponent(r.conversation_type),'entityId='+encodeURIComponent(r.entity_id||''),'entityType='+encodeURIComponent(r.entity_type||'')].join('&');return '<div class="result-card" data-href="/admin/conversations?'+params+'"><div class="rc-context">'+badge+'<span class="rc-conv" title="'+esc(r.conversation_name)+'">'+esc(r.conversation_name)+'</span><span class="'+senderClass+'">'+esc(r.sender_display_name)+'</span><span class="rc-time">'+esc(fmtTime(r.msgtime))+'</span></div><div class="rc-snippet">'+highlight(r.content_snippet||'')+'</div><div class="rc-foot"><span class="rc-route">员工 <b>'+esc(r.entity_id||'-')+'</b> <span class="arrow">·</span> 客户 <b>'+esc(r.sender||'-')+'</b> <span class="arrow">·</span> '+esc(msgtypeLabel(r.msgtype))+'</span><span class="rc-jump">查看上下文 ↗</span></div></div>';}).join('');Array.prototype.forEach.call(list.querySelectorAll('.result-card'),function(el){el.addEventListener('click',function(){window.location.href=el.getAttribute('data-href');});});}
function init(){
  applyStaticI18n();
  loadCurrentUser();
  KEYWORD=getParam('q')||'';
  FILTERS.dateRange=getParam('date_range')||null;
  FILTERS.user=parseListParam('user');
  FILTERS.staff=parseListParam('staff');
  FILTERS.msgtype=parseListParam('msgtype');
  var ql=document.getElementById('qLabel');
  if(KEYWORD){ql.innerHTML='“'+esc(KEYWORD)+'”';}else{ql.textContent='筛选结果';}
  updateFilterUI();
  runSearchOrShowHint();
}
init();
