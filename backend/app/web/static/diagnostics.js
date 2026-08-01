
function esc(s){
  return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function handleUnauth(r){
  if(r.status===401){window.location.href='/admin/login';return true;}
  return false;
}
function applyStaticI18n(){
  document.documentElement.lang=I18N.getLocale();
  document.title=I18N.t('diagnostics.pageTitle');
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent=I18N.t(el.getAttribute('data-i18n'));
  });
}

var ReachabilityStatusRegistry=(function(){
  var entries={
    reachable_direct:{labelKey:'reachability.status.reachable_direct',reachable:true},
    reachable_group:{labelKey:'reachability.status.reachable_group',reachable:true},
    unreachable_missing_recipient:{labelKey:'reachability.status.unreachable_missing_recipient',reachable:false},
    unreachable_missing_room:{labelKey:'reachability.status.unreachable_missing_room',reachable:false},
    unreachable_missing_sender:{labelKey:'reachability.status.unreachable_missing_sender',reachable:false},
    unreachable_membership:{labelKey:'reachability.status.unreachable_membership',reachable:false},
    unreachable_other:{labelKey:'reachability.status.unreachable_other',reachable:false}
  };
  var FALLBACK={labelKey:'reachability.status.unknown',reachable:false};
  function resolve(status){
    return (status&&Object.prototype.hasOwnProperty.call(entries,status))?entries[status]:FALLBACK;
  }
  function label(status){ return I18N.t(resolve(status).labelKey); }
  function isReachable(status){ return resolve(status).reachable===true; }
  return {entries:entries,fallback:FALLBACK,resolve:resolve,label:label,isReachable:isReachable};
})();

var lastReport=null;

function pct(n,d){
  if(!d)return'0.0%';
  return (n/d*100).toFixed(1)+'%';
}

function fmtInt(n){
  return (typeof n==='number')?n.toLocaleString():esc(n);
}

function cardHtml(labelKey,value){
  return '<div class="diag-card"><div class="diag-card-label">'+esc(I18N.t(labelKey))+'</div>'+
    '<div class="diag-card-value">'+esc(value)+'</div></div>';
}

function metaHtml(labelKey,value){
  return '<span><strong>'+esc(I18N.t(labelKey))+':</strong> '+esc(value)+'</span>';
}

function renderStatusTable(countsByStatus,unreachableTotal){
  countsByStatus=countsByStatus||{};
  var statuses=Object.keys(countsByStatus).filter(function(s){
    return countsByStatus[s]>0 && !ReachabilityStatusRegistry.isReachable(s);
  });
  if(!statuses.length){
    return '<div class="diag-empty">'+esc(I18N.t('diagnostics.noData'))+'</div>';
  }
  statuses.sort(function(a,b){ return (countsByStatus[b]||0)-(countsByStatus[a]||0); });
  var rows='';
  statuses.forEach(function(status){
    var count=countsByStatus[status]||0;
    rows+='<tr><td>'+esc(ReachabilityStatusRegistry.label(status))+'</td><td>'+fmtInt(count)+'</td><td>'+pct(count,unreachableTotal)+'</td></tr>';
  });
  return '<div class="table-wrap"><table class="table"><thead><tr><th>'+esc(I18N.t('diagnostics.reasonColumn'))+'</th><th>'+
    esc(I18N.t('diagnostics.countColumn'))+'</th><th>'+esc(I18N.t('diagnostics.percentColumn'))+'</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>';
}

function renderTypeTable(countsByType){
  countsByType=countsByType||{};
  var types=Object.keys(countsByType).filter(function(t){ return countsByType[t]>0; });
  if(!types.length){
    return '<div class="diag-empty">'+esc(I18N.t('diagnostics.noData'))+'</div>';
  }
  types.sort(function(a,b){ return (countsByType[b]||0)-(countsByType[a]||0); });
  var rows='';
  types.forEach(function(t){
    rows+='<tr><td>'+esc(t)+'</td><td>'+fmtInt(countsByType[t])+'</td></tr>';
  });
  return '<div class="table-wrap"><table class="table"><thead><tr><th>'+esc(I18N.t('diagnostics.typeColumn'))+'</th><th>'+
    esc(I18N.t('diagnostics.totalColumn'))+'</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}

function renderReport(data){
  var root=document.getElementById('diag-root');
  if(!data.matching_total){
    root.innerHTML='<div class="diag-empty">'+esc(I18N.t('diagnostics.noData'))+'</div>';
    return;
  }

  var html='';

  if(data.has_more){
    html+='<div class="diag-note">'+esc(I18N.t('diagnostics.moreMessagesExist'))+'</div>';
  }

  var reachRate=pct(data.reachable_count,(data.reachable_count||0)+(data.unreachable_count||0));

  html+='<div class="diag-cards">';
  html+=cardHtml('diagnostics.successfulDecrypted',fmtInt(data.matching_total));
  html+=cardHtml('diagnostics.reachableMessages',fmtInt(data.reachable_count));
  html+=cardHtml('diagnostics.unreachableMessages',fmtInt(data.unreachable_count));
  html+=cardHtml('diagnostics.reachabilityRate',reachRate);
  html+='</div>';

  html+='<div class="diag-section"><h2>'+esc(I18N.t('diagnostics.scanMetadataTitle'))+'</h2>';
  html+='<div class="diag-meta-list">';
  html+=metaHtml('diagnostics.scannedMessages',fmtInt(data.scanned_count));
  html+=metaHtml('diagnostics.matchingTotal',fmtInt(data.matching_total));
  html+=metaHtml('diagnostics.limitLabel',fmtInt(data.limit));
  html+=metaHtml('diagnostics.offsetLabel',fmtInt(data.offset));
  html+=metaHtml('diagnostics.hasMoreLabel',data.has_more?I18N.t('diagnostics.hasMoreYes'):I18N.t('diagnostics.hasMoreNo'));
  html+='</div></div>';

  html+='<div class="diag-section"><h2>'+esc(I18N.t('diagnostics.unreachableReasons'))+'</h2>';
  html+=renderStatusTable(data.counts_by_status,data.unreachable_count);
  html+='</div>';

  html+='<div class="diag-section"><h2>'+esc(I18N.t('diagnostics.byMessageType'))+'</h2>';
  html+=renderTypeTable(data.counts_by_message_type);
  html+='</div>';

  root.innerHTML=html;
}

function renderError(){
  var root=document.getElementById('diag-root');
  root.innerHTML='<div class="diag-error">'+esc(I18N.t('diagnostics.failedToLoad'))+
    ' <button class="diag-retry-btn" onclick="loadDiagnostics()">'+esc(I18N.t('diagnostics.retry'))+'</button></div>';
}

function loadDiagnostics(){
  var root=document.getElementById('diag-root');
  root.innerHTML='<div class="diag-loading">'+esc(I18N.t('diagnostics.loading'))+'</div>';
  fetch('/api/admin/reachability-audit').then(function(r){
    if(handleUnauth(r))return null;
    if(!r.ok)throw new Error('request_failed');
    return r.json();
  }).then(function(data){
    if(!data)return;
    lastReport=data;
    renderReport(data);
  }).catch(function(){
    renderError();
  });
}

applyStaticI18n();
document.getElementById('btn-diag-refresh').addEventListener('click',loadDiagnostics);
I18N.onChange(function(){
  applyStaticI18n();
  if(lastReport){renderReport(lastReport);}
});
loadDiagnostics();
