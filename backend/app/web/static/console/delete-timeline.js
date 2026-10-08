// RND-363: message deletion selection mode for the conversation timeline.
//
// Deleting is Owner/Admin-only and fail-closed against a tenant compliance
// hold (RND-362). Selection state is scoped to the current tenant and
// current conversation and is cleared whenever the conversation, scope,
// or mode changes; pagination of older messages never reuses it.
// Deletion itself never reads or logs message bodies.

function loadDeletionStatus(){
  return fetch('/api/admin/messages/deletion-status',{credentials:'include'})
    .then(function(r){if(!r.ok)return null;return r.json();})
    .then(function(d){
      if(!d)return null;
      deleteStatus=d;
      updateDeleteModeButton();
      return d;
    })
    .catch(function(){return null;});
}

function deletionEligible(){
  return Boolean(deleteStatus&&deleteStatus.can_delete&&!deleteStatus.deletion_locked);
}

function deletionUnavailableReason(){
  if(!deleteStatus)return null;
  if(!deleteStatus.can_delete)return 'delete.denied';
  if(deleteStatus.deletion_locked)return 'delete.locked';
  return null;
}

function toggleDeleteMode(){
  if(!deletionEligible())return;
  deleteMode=!deleteMode;
  if(deleteMode&&typeof favoriteMode!=='undefined'&&favoriteMode){
    favoriteMode=false;
    if(typeof clearTimelineFavoriteSelection==='function')clearTimelineFavoriteSelection();
    if(typeof updateTimelineFavoriteModeRows==='function')updateTimelineFavoriteModeRows();
  }
  if(!deleteMode)clearDeleteSelection();
  updateDeleteModeButton();
  updateDeleteBar();
  renderTimeline(false);
}

function updateDeleteModeButton(){
  var btn=document.getElementById('btn-delete-mode');
  if(!btn)return;
  btn.disabled=!deletionEligible()||!selConvId;
  btn.classList.toggle('active',deleteMode&&deletionEligible());
  if(typeof updateTimelineFavoriteModeButton==='function')updateTimelineFavoriteModeButton();
}

function clearDeleteSelection(){deleteSelection={};}

function countDeleteSelection(){
  var n=0;
  Object.keys(deleteSelection).forEach(function(k){if(deleteSelection[k])n++;});
  return n;
}

function toggleMessageForDelete(msgid){
  if(!deleteMode||!msgid)return;
  if(deleteSelection[msgid]){delete deleteSelection[msgid];}
  else{deleteSelection[msgid]=true;}
  updateDeleteBar();
  var row=document.querySelector('[data-msgid="'+String(msgid).replace(/"/g,'\\"')+'"] .tl-delete-check');
  if(row)row.checked=Boolean(deleteSelection[msgid]);
}

function toggleSelectAllLoaded(){
  if(!deleteMode||!timelineMsgs||!timelineMsgs.length)return;
  var all=timelineMsgs.every(function(m){return deleteSelection[m.msgid];});
  timelineMsgs.forEach(function(m){
    if(all){delete deleteSelection[m.msgid];}
    else{deleteSelection[m.msgid]=true;}
  });
  updateDeleteBar();
  renderTimeline(false);
}

function updateDeleteBar(){
  var bar=document.getElementById('delete-bar');
  if(!bar)return;
  bar.hidden=!deleteMode;
  if(!deleteMode)return;
  var count=countDeleteSelection();
  var label=document.getElementById('delete-selected-count');
  if(label)label.textContent=I18N.t('delete.selected',{count:String(count)});
  var del=document.getElementById('btn-delete-confirm');
  if(del)del.disabled=count<1;
  var all=document.getElementById('btn-delete-select-all');
  if(all){
    var allSelected=timelineMsgs&&timelineMsgs.length&&timelineMsgs.every(function(m){return deleteSelection[m.msgid];});
    all.textContent=I18N.t(allSelected?'delete.selectNone':'delete.selectAllLoaded');
  }
  var result=document.getElementById('delete-result');
  if(result){result.hidden=true;result.textContent='';}
}

function showDeleteResult(message,withRecycleLink){
  var result=document.getElementById('delete-result');
  if(!result)return;
  result.hidden=false;
  result.textContent=message;
  var link=document.getElementById('delete-result-recycle');
  if(link)link.hidden=!withRecycleLink;
}

function confirmDeleteSelected(){
  if(!deleteMode)return;
  var ids=Object.keys(deleteSelection).filter(function(k){return deleteSelection[k];});
  if(!ids.length)return;
  if(ids.length>100)ids=ids.slice(0,100);
  var modal=document.getElementById('delete-confirm-modal');
  if(modal){
    document.getElementById('delete-confirm-count').textContent=I18N.t('delete.confirmCount',{count:String(ids.length)});
    modal.hidden=false;
    var ok=document.getElementById('btn-delete-confirm-ok');
    ok.onclick=function(){modal.hidden=true;runDeleteSelected(ids);};
    var cancel=document.getElementById('btn-delete-confirm-cancel');
    cancel.onclick=function(){modal.hidden=true;};
    var close=document.getElementById('btn-delete-confirm-close');
    if(close)close.onclick=function(){modal.hidden=true;};
    ok.focus();
    return;
  }
  if(window.confirm(I18N.t('delete.confirmCopy',{count:String(ids.length)})))runDeleteSelected(ids);
}

function runDeleteSelected(ids){
  var btn=document.getElementById('btn-delete-confirm');
  if(btn)btn.disabled=true;
  fetch('/api/admin/messages/delete',{
    method:'POST',
    credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message_ids:ids})
  })
    .then(function(r){
      return r.json().catch(function(){return {};}).then(function(body){
        if(!r.ok){
          var error=new Error(body.detail||'request_failed');
          error.status=r.status;
          throw error;
        }
        return body;
      });
    })
    .then(function(result){
      var gone={};
      (result.deleted_message_ids||[]).forEach(function(id){gone[id]=true;});
      // Keep pagination/order stable: filter in place, never re-sort.
      timelineMsgs=timelineMsgs.filter(function(m){return !gone[m.msgid];});
      clearDeleteSelection();
      updateDeleteBar();
      renderTimeline(false);
      var parts=[];
      if(result.deleted)parts.push(I18N.t('delete.result.deleted',{count:String(result.deleted)}));
      if(result.already_deleted)parts.push(I18N.t('delete.result.already',{count:String(result.already_deleted)}));
      if(result.not_found)parts.push(I18N.t('delete.result.notFound',{count:String(result.not_found)}));
      showDeleteResult(parts.join('；')||I18N.t('delete.result.none'),result.deleted>0);
    })
    .catch(function(error){
      var reason=deletionUnavailableReason();
      var message=error.status===423?(reason?I18N.t(reason):I18N.t('delete.locked'))
        :error.status===401?I18N.t('billing.error.auth')
          :I18N.t('delete.failed');
      showDeleteResult(message,false);
    })
    .finally(function(){
      if(btn)btn.disabled=countDeleteSelection()<1;
    });
}
