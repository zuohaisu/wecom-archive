(function(){
  'use strict';

  var LATEST_URL='/api/admin/reachability-checks/latest';
  var START_URL='/api/admin/reachability-checks';
  var TERMINAL={healthy:true,attention:true,no_data:true,incomplete:true,error:true};
  var POLL_DELAYS=[1000,1500,2500,4000,6000];
  var MAX_POLL_ATTEMPTS=12;
  var MAX_COUNT=1000000000;
  var pollTimer=null;
  var pollAttempts=0;
  var latestRequest=false;
  var startRequest=false;
  var unloaded=false;
  var currentView={kind:'loading'};

  function text(key, values){
    var value=I18N.t(key);
    Object.keys(values||{}).forEach(function(name){ value=value.replace('{'+name+'}',String(values[name])); });
    return value;
  }
  function el(tag, className, value){
    var element=document.createElement(tag);
    if(className)element.className=className;
    if(value!==undefined)element.textContent=value;
    return element;
  }
  function append(parent, tag, className, value){
    var child=el(tag,className,value);parent.appendChild(child);return child;
  }
  function clear(element){ element.replaceChildren(); }
  function validCount(value){ return typeof value==='number'&&isFinite(value)&&Math.floor(value)===value&&value>=0&&value<=MAX_COUNT; }
  function count(value){ return validCount(value)?value:0; }
  function safeDate(value){
    if(typeof value!=='string'||value.length>64)return null;
    var date=new Date(value);
    return isNaN(date.getTime())?null:date;
  }
  function formatDate(value){
    var date=safeDate(value);
    if(!date)return text('diagnostics.timeUnavailable');
    try{return new Intl.DateTimeFormat(I18N.getLocale(),{dateStyle:'medium',timeStyle:'short'}).format(date);}catch(_error){return date.toISOString().replace('T',' ').replace('.000Z',' UTC');}
  }
  function safeVersion(value){ return typeof value==='string'&&/^reachability-v[0-9]+(?:[._-][a-zA-Z0-9]+)*$/.test(value)?value:text('diagnostics.unavailable'); }
  function safeReasonCounts(value){
    var known={unreachable_missing_recipient:'diagnostics.reason.missingRecipient',unreachable_missing_room:'diagnostics.reason.missingRoom',unreachable_missing_sender:'diagnostics.reason.missingSender',unreachable_membership:'diagnostics.reason.membership',unreachable_other:'diagnostics.reason.other'};
    var rows=[];var unknown=0;
    if(!value||typeof value!=='object'||Array.isArray(value))return rows;
    Object.keys(value).forEach(function(code){
      if(!validCount(value[code]))return;
      if(known[code])rows.push({key:known[code],count:value[code]});else unknown+=value[code];
    });
    if(unknown)rows.push({key:'diagnostics.reason.unknown',count:unknown});
    return rows.sort(function(a,b){return b.count-a.count||a.key.localeCompare(b.key);});
  }
  function normalise(data){
    data=data&&typeof data==='object'?data:{};
    var state=typeof data.state==='string'&&Object.prototype.hasOwnProperty.call(TERMINAL,data.state)||data.state==='checking'?data.state:'error';
    var rawCounts=data.counts&&typeof data.counts==='object'?data.counts:{};
    var invalid=!validCount(rawCounts.matching)||!validCount(rawCounts.checked)||!validCount(rawCounts.reachable)||!validCount(rawCounts.unreachable);
    var scope=data.scope&&typeof data.scope==='object'?data.scope:{};
    var complete=data.complete===true;
    if(invalid||!safeDate(scope.from_at)||!safeDate(scope.to_at))state='error';
    if((state==='healthy'||state==='attention')&&!complete)state='incomplete';
    return {state:state,complete:complete,counts:{matching:count(rawCounts.matching),checked:count(rawCounts.checked),reachable:count(rawCounts.reachable),unreachable:count(rawCounts.unreachable)},scope:{from_at:scope.from_at,to_at:scope.to_at},last_checked_at:data.last_checked_at,algorithm_version:safeVersion(data.algorithm_version),reasons:safeReasonCounts(data.reason_counts),safe_error_code:typeof data.safe_error_code==='string'?data.safe_error_code:'' ,hasRun:typeof data.public_run_id==='string'};
  }
  function stateInfo(snapshot){
    var n=snapshot.counts.unreachable;
    if(snapshot.state==='healthy')return {icon:'✓',title:'diagnostics.state.healthy.title',copy:'diagnostics.state.healthy.copy',action:'diagnostics.checkNow'};
    if(snapshot.state==='attention')return {icon:'!',title:'diagnostics.state.attention.title',copy:'diagnostics.state.attention.copy',values:{n:n},action:'diagnostics.checkAgain'};
    if(snapshot.state==='checking')return {icon:'…',title:'diagnostics.state.checking.title',copy:'diagnostics.state.checking.copy',action:'diagnostics.checking',disabled:true};
    if(snapshot.state==='no_data')return {icon:'○',title:'diagnostics.state.noData.title',copy:'diagnostics.state.noData.copy',action:'diagnostics.checkNow'};
    if(snapshot.state==='incomplete')return {icon:'!',title:'diagnostics.state.incomplete.title',copy:snapshot.hasRun?'diagnostics.state.incomplete.interrupted':'diagnostics.state.incomplete.none',action:'diagnostics.checkNow'};
    return {icon:'×',title:'diagnostics.state.error.title',copy:safeErrorText(snapshot.safe_error_code),action:'diagnostics.checkAgain'};
  }
  function safeErrorText(code){
    var allowed={scan_failed:'diagnostics.error.scanFailed',process_start_failed:'diagnostics.error.startFailed',stale_checking:'diagnostics.error.interrupted',scope_mismatch:'diagnostics.error.incomplete',page_incomplete:'diagnostics.error.incomplete'};
    return allowed[code]||'diagnostics.error.generic';
  }
  function meta(parent, key, value){var item=append(parent,'div');append(item,'dt','',text(key));append(item,'dd','',value);}
  function technicalRow(list,key,value){var row=append(list,'div');append(row,'dt','',text(key));append(row,'dd','',value);}
  function addDetails(card,snapshot){
    var details=append(card,'details','diag-details');
    append(details,'summary','',text('diagnostics.technicalDetails'));
    var list=append(details,'dl','diag-technical');
    technicalRow(list,'diagnostics.complete',snapshot.complete?text('diagnostics.yes'):text('diagnostics.no'));
    technicalRow(list,'diagnostics.matchingMessages',String(snapshot.counts.matching));
    technicalRow(list,'diagnostics.checkedMessages',String(snapshot.counts.checked));
    technicalRow(list,'diagnostics.reachableMessages',String(snapshot.counts.reachable));
    technicalRow(list,'diagnostics.unreachableMessages',String(snapshot.counts.unreachable));
    technicalRow(list,'diagnostics.algorithmVersion',snapshot.algorithm_version);
    technicalRow(list,'diagnostics.scopeFrom',formatDate(snapshot.scope.from_at));
    technicalRow(list,'diagnostics.scopeTo',formatDate(snapshot.scope.to_at));
    if(snapshot.reasons.length){
      var reasons=append(list,'div','diag-reasons');append(reasons,'dt','',text('diagnostics.reasonSummary'));
      var reasonList=append(reasons,'ul','diag-reason-list');
      snapshot.reasons.forEach(function(reason){var item=append(reasonList,'li');append(item,'span','',text(reason.key));append(item,'strong','',String(reason.count));});
    }
  }
  function actionButton(info){
    var button=el('button','btn btn-primary',text(info.action));button.type='button';button.disabled=!!info.disabled||startRequest;
    if(!button.disabled)button.addEventListener('click',startCheck);
    return button;
  }
  function renderSnapshot(snapshot){
    var root=document.getElementById('diag-root');if(!root)return;
    clear(root);root.setAttribute('aria-busy','false');
    var info=stateInfo(snapshot);var card=append(root,'section','diag-health');card.setAttribute('data-state',snapshot.state);
    var head=append(card,'div','diag-health-head');append(head,'span','diag-state-icon',info.icon).setAttribute('aria-hidden','true');
    var copy=append(head,'div');append(copy,'h2','diag-health-title',text(info.title));append(copy,'p','diag-health-copy',text(info.copy,info.values));
    var metadata=append(card,'dl','diag-health-meta');
    meta(metadata,'diagnostics.lastCompletedCheck',snapshot.complete?formatDate(snapshot.last_checked_at):text('diagnostics.noCompletedCheck'));
    meta(metadata,'diagnostics.checkScope',formatDate(snapshot.scope.from_at)+' – '+formatDate(snapshot.scope.to_at));
    meta(metadata,'diagnostics.checkedMessages',String(snapshot.counts.checked));
    var actions=append(card,'div','diag-actions');actions.appendChild(actionButton(info));
    addDetails(card,snapshot);
  }
  function renderProblem(kind){
    var root=document.getElementById('diag-root');if(!root)return;
    clear(root);root.setAttribute('aria-busy','false');
    var key=kind==='auth'?'diagnostics.authFailed':kind==='forbidden'?'diagnostics.forbidden':kind==='timeout'?'diagnostics.pollTimeout':kind==='start'?'diagnostics.startRequestFailed':'diagnostics.failedToLoad';
    var card=append(root,'section','diag-health');card.setAttribute('data-state',kind==='auth'||kind==='forbidden'?'auth':'error');
    var head=append(card,'div','diag-health-head');append(head,'span','diag-state-icon','×').setAttribute('aria-hidden','true');
    var content=append(head,'div');append(content,'h2','diag-health-title',text('diagnostics.state.error.title'));append(content,'p','diag-health-copy',text(key));
    var actions=append(card,'div','diag-actions');var button=el('button','btn btn-primary',text(kind==='timeout'?'diagnostics.viewLatest':'diagnostics.retry'));button.type='button';button.addEventListener('click',loadLatest);actions.appendChild(button);
  }
  function renderLoading(){
    var root=document.getElementById('diag-root');if(!root)return;clear(root);root.setAttribute('aria-busy','true');append(root,'p','diag-loading',text('diagnostics.loading'));
  }
  function render(){
    applyStaticI18n();
    if(currentView.kind==='snapshot')renderSnapshot(currentView.snapshot);
    else if(currentView.kind==='problem')renderProblem(currentView.problem);
    else renderLoading();
  }
  function stopPolling(){if(pollTimer!==null){window.clearTimeout(pollTimer);pollTimer=null;}}
  function schedulePoll(){
    if(unloaded)return;
    if(pollAttempts>=MAX_POLL_ATTEMPTS){currentView={kind:'problem',problem:'timeout'};render();return;}
    var delay=POLL_DELAYS[Math.min(pollAttempts,POLL_DELAYS.length-1)];
    pollTimer=window.setTimeout(function(){pollTimer=null;pollAttempts+=1;loadLatest(true);},delay);
  }
  function receiveSnapshot(data,fromPoll){
    var snapshot=normalise(data);currentView={kind:'snapshot',snapshot:snapshot};render();
    if(snapshot.state==='checking'){if(fromPoll||pollTimer===null)schedulePoll();}else stopPolling();
  }
  function loadLatest(fromPoll){
    if(latestRequest)return;latestRequest=true;
    if(!fromPoll){stopPolling();renderLoading();}
    fetch(LATEST_URL,{credentials:'include'}).then(function(response){
      if(response.status===401){currentView={kind:'problem',problem:'auth'};render();return null;}
      if(response.status===403){currentView={kind:'problem',problem:'forbidden'};render();return null;}
      if(!response.ok)throw new Error('latest_failed');return response.json();
    }).then(function(data){if(data)receiveSnapshot(data,fromPoll);}).catch(function(){currentView={kind:'problem',problem:'network'};render();}).finally(function(){latestRequest=false;});
  }
  function startCheck(){
    if(startRequest)return;startRequest=true;stopPolling();
    if(currentView.kind==='snapshot'){currentView.snapshot.state='checking';currentView.snapshot.complete=false;render();}
    fetch(START_URL,{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(response){
      if(response.status===409){loadLatest(true);return null;}
      if(response.status===401){currentView={kind:'problem',problem:'auth'};render();return null;}
      if(response.status===403){currentView={kind:'problem',problem:'forbidden'};render();return null;}
      if(!response.ok)throw new Error('start_failed');return response.json();
    }).then(function(data){if(data){pollAttempts=0;receiveSnapshot(data,true);}}).catch(function(){currentView={kind:'problem',problem:'start'};render();}).finally(function(){startRequest=false;});
  }
  function applyStaticI18n(){
    document.documentElement.lang=I18N.getLocale();document.title=I18N.t('diagnostics.pageTitle');
    document.querySelectorAll('[data-i18n]').forEach(function(element){element.textContent=I18N.t(element.getAttribute('data-i18n'));});
  }

  I18N.onChange(render);
  window.addEventListener('pagehide',function(){unloaded=true;stopPolling();});
  renderLoading();
  loadLatest(false);
}());
