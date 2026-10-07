// RND-368: message favorite interactions for the conversation timeline.
// All writes/status checks use the shared tenant-scoped favorites API; this
// module keeps only ephemeral view state and never persists message content.
function timelineFavoriteTranslate(key,values){
  var text=I18N.t(key);
  Object.keys(values||{}).forEach(function(name){
    text=text.replace(new RegExp('\\{'+name+'\\}','g'),String(values[name]));
  });
  return text;
}

function timelineFavoriteWritableRole(role){
  return ['owner','admin','compliance','legal'].indexOf(role)!==-1;
}

function timelineFavoriteTargets(messages){
  var seen={};
  return (messages||[]).filter(function(message){
    if(!message||typeof message.msgid!=='string'||!message.msgid||seen[message.msgid])return false;
    seen[message.msgid]=true;
    return true;
  });
}

function timelineFavoriteChunks(items){
  var result=[];
  for(var i=0;i<items.length;i+=100)result.push(items.slice(i,i+100));
  return result;
}

function timelineFavoritePayload(messages){
  return messages.map(function(message){
    return {object_type:'message',object_id:message.msgid,source_page:'messages'};
  });
}

function timelineFavoriteRequest(url,body){
  return fetch(url,{
    method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
  }).then(function(response){
    if(!response.ok){var error=new Error('favorite_request_failed');error.status=response.status;throw error;}
    return response.json();
  });
}

function timelineFavoriteCount(){
  return Object.keys(favoriteSelection).filter(function(id){return favoriteSelection[id];}).length;
}

function timelineFavoriteMessage(msgid){
  for(var i=0;i<timelineMsgs.length;i++)if(timelineMsgs[i].msgid===msgid)return timelineMsgs[i];
  return null;
}

function timelineFavoriteStatusFor(msgid){
  return favoriteStates[msgid]||null;
}

function timelineFavoriteStatusReady(msgid){
  var status=timelineFavoriteStatusFor(msgid);
  return Boolean(status&&status.result==='found'&&typeof status.isFavorited==='boolean');
}

function timelineFavoriteRowHtml(message){
  var msgid=message.msgid;
  var status=timelineFavoriteStatusFor(msgid);
  var known=timelineFavoriteStatusReady(msgid);
  var favorited=known&&status.isFavorited;
  var pending=!status||status.result==='pending';
  var marker=pending?'…':(status&&status.result==='not_found'?'!':(favorited?'★':'☆'));
  var markerLabel=pending?I18N.t('favorites.statusLoading')
    :(status&&status.result==='not_found'?I18N.t('favorites.unavailable')
      :(favorited?I18N.t('favorites.messageFavorited'):I18N.t('favorites.messageNotFavorited')));
  var actionLabel=I18N.t(favorited?'favorites.removeMessage':'favorites.addMessage');
  var selected=favoriteSelection[msgid]?' checked':'';
  var unavailable=status&&status.result==='not_found';
  var disabled=!favoriteUserCanWrite||!known||favoriteBusy||!!deleteMode||!!unavailable;
  var toggle='<button type="button" class="tl-favorite-toggle" data-favorite-toggle aria-pressed="'+String(!!favorited)+'" aria-label="'+esc(actionLabel)+'" title="'+esc(actionLabel)+'"'+(disabled?' disabled':'')+' onclick="event.stopPropagation();toggleSingleMessageFavorite(&quot;'+esc(msgid)+'&quot;)"><span aria-hidden="true">'+(favorited?'★':'☆')+'</span></button>';
  var checkbox='<label class="tl-favorite-select-label"><input type="checkbox" class="tl-favorite-select-check" data-favorite-select="'+esc(msgid)+'" aria-label="'+esc(I18N.t('favorites.selectMessage'))+'"'+selected+(favoriteBusy||unavailable?' disabled':'')+' onclick="event.stopPropagation();toggleMessageForFavorite(&quot;'+esc(msgid)+'&quot;)" onkeydown="if(event.key===\'Enter\'){event.preventDefault();event.stopPropagation();toggleMessageForFavorite(&quot;'+esc(msgid)+'&quot;)}"></label>';
  return checkbox+'<span class="tl-favorite-marker" data-favorite-marker aria-label="'+esc(markerLabel)+'" title="'+esc(markerLabel)+'">'+marker+'</span>'+toggle;
}

function updateTimelineFavoriteRole(role){
  favoriteUserCanWrite=timelineFavoriteWritableRole(role);
  updateTimelineFavoriteControls();
  updateTimelineFavoriteRows();
}

function clearTimelineFavoriteSelection(){
  favoriteSelection={};
  updateTimelineFavoriteControls();
  updateTimelineFavoriteRows();
}

function resetTimelineFavoritesForConversation(){
  clearTimelineFavoriteSelection();
  favoriteStates={};
  favoriteStateVersions={};
  favoriteStatusFailed=false;
  favoriteStatusRetrying=false;
  setTimelineFavoriteResult('',false,false);
  updateTimelineFavoriteModeButton();
  updateTimelineFavoriteControls();
}

function resetTimelineFavoriteScope(){
  favoriteMode=false;
  timelineFavoritesOnly=false;
  var filter=document.getElementById('timeline-favorites-only');
  if(filter)filter.checked=false;
  resetTimelineFavoritesForConversation();
  applyTimelineFavoriteModeClass();
}

function updateTimelineFavoriteModeButton(){
  var button=document.getElementById('btn-favorite-mode');
  if(button){
    button.disabled=!selConvId||!!deleteMode;
    if(button.classList)button.classList.toggle('active',favoriteMode);
    if(button.setAttribute)button.setAttribute('aria-pressed',String(favoriteMode));
    button.textContent=I18N.t(favoriteMode?'favorites.exitSelection':'favorites.enterSelection');
  }
  var filter=document.getElementById('timeline-favorites-only');
  if(filter){filter.checked=!!timelineFavoritesOnly;filter.disabled=!selConvId;}
}

function applyTimelineFavoriteModeClass(){
  var timeline=document.querySelector('.timeline');
  if(timeline)timeline.classList.toggle('favorite-selection-mode',favoriteMode);
  updateTimelineFavoriteRows();
}

function updateTimelineFavoriteControls(){
  var bar=document.getElementById('favorite-selection-bar');
  if(bar)bar.hidden=!favoriteMode;
  var count=timelineFavoriteCount();
  var label=document.getElementById('favorite-selected-count');
  if(label)label.textContent=timelineFavoriteTranslate('favorites.selectedCount',{count:String(count)});
  var ids=Object.keys(favoriteSelection).filter(function(id){return favoriteSelection[id];});
  var ready=ids.length>0&&ids.every(timelineFavoriteStatusReady);
  var all=timelineMsgs.length>0&&timelineMsgs.every(function(message){return !!favoriteSelection[message.msgid];});
  var selectAll=document.getElementById('btn-favorite-select-all');
  if(selectAll){selectAll.textContent=I18N.t(all?'favorites.selectNone':'favorites.selectAllLoaded');selectAll.disabled=!timelineMsgs.length||favoriteBusy;}
  var clear=document.getElementById('btn-favorite-clear-selection');
  if(clear)clear.disabled=count===0||favoriteBusy;
  var canAct=favoriteUserCanWrite&&ready&&!favoriteBusy;
  var favorite=document.getElementById('btn-favorite-selected');
  if(favorite)favorite.disabled=!canAct;
  var unfavorite=document.getElementById('btn-unfavorite-selected');
  if(unfavorite)unfavorite.disabled=!canAct;
  var retry=document.getElementById('timeline-favorite-status-retry');
  if(retry){retry.hidden=!favoriteStatusFailed;retry.disabled=favoriteBusy;}
  var hint=document.getElementById('favorite-selection-hint');
  if(hint){
    hint.textContent=!favoriteUserCanWrite?I18N.t('favorites.permissionDenied')
      :(favoriteStatusFailed?I18N.t('favorites.statusFailed')
        :(ids.length&&!ready?I18N.t('favorites.statusLoading'):''));
  }
  updateTimelineFavoriteModeButton();
}

function updateTimelineFavoriteRows(msgids){
  var ids=msgids||timelineMsgs.map(function(message){return message.msgid;});
  var timeline=document.querySelector('.timeline');
  var rows=timeline?timeline.querySelectorAll('.tl-row[data-msgid]'):[];
  ids.forEach(function(msgid){
    var row=null;
    for(var i=0;i<rows.length;i++){
      if(rows[i].getAttribute('data-msgid')===msgid){row=rows[i];break;}
    }
    if(!row)return;
    var status=timelineFavoriteStatusFor(msgid);
    var known=timelineFavoriteStatusReady(msgid);
    var favorited=known&&status.isFavorited;
    var pending=!status||status.result==='pending';
    var marker=pending?'…':(status&&status.result==='not_found'?'!':(favorited?'★':'☆'));
    var markerLabel=pending?I18N.t('favorites.statusLoading')
      :(status&&status.result==='not_found'?I18N.t('favorites.unavailable')
        :(favorited?I18N.t('favorites.messageFavorited'):I18N.t('favorites.messageNotFavorited')));
    var markerEl=row.querySelector('[data-favorite-marker]');
    if(markerEl){markerEl.textContent=marker;markerEl.setAttribute('aria-label',markerLabel);markerEl.title=markerLabel;}
    var toggle=row.querySelector('[data-favorite-toggle]');
    if(toggle){
      var actionLabel=I18N.t(favorited?'favorites.removeMessage':'favorites.addMessage');
      toggle.disabled=!favoriteUserCanWrite||!known||favoriteBusy||!!deleteMode||!!(status&&status.result==='not_found');
      toggle.setAttribute('aria-pressed',String(!!favorited));
      toggle.setAttribute('aria-label',actionLabel);
      toggle.title=actionLabel;
      toggle.textContent=favorited?'★':'☆';
    }
    var checkbox=row.querySelector('[data-favorite-select]');
    if(checkbox){
      checkbox.checked=!!favoriteSelection[msgid];
      checkbox.disabled=favoriteBusy||!!(status&&status.result==='not_found');
    }
    row.classList.toggle('favorite-selected',!!favoriteSelection[msgid]);
  });
}

function updateTimelineFavoriteModeRows(){
  var timeline=document.querySelector('.timeline');
  if(timeline)timeline.classList.toggle('favorite-selection-mode',favoriteMode);
  updateTimelineFavoriteRows();
}

function toggleTimelineFavoriteMode(){
  if(!selConvId||deleteMode)return;
  favoriteMode=!favoriteMode;
  if(!favoriteMode)clearTimelineFavoriteSelection();
  updateTimelineFavoriteModeButton();
  updateTimelineFavoriteControls();
  updateTimelineFavoriteModeRows();
  if(favoriteMode){
    var first=document.querySelector('.tl-favorite-select-check:not(:disabled)');
    if(first)first.focus();
  }else{
    var button=document.getElementById('btn-favorite-mode');
    if(button)button.focus();
  }
}

function toggleMessageForFavorite(msgid){
  if(!favoriteMode||favoriteBusy||!msgid)return;
  var status=timelineFavoriteStatusFor(msgid);
  if(status&&status.result==='not_found')return;
  if(favoriteSelection[msgid])delete favoriteSelection[msgid];
  else favoriteSelection[msgid]=true;
  updateTimelineFavoriteControls();
  updateTimelineFavoriteRows([msgid]);
}

function toggleFavoriteSelectionAll(){
  if(!favoriteMode||favoriteBusy||!timelineMsgs.length)return;
  var all=timelineMsgs.every(function(message){return !!favoriteSelection[message.msgid];});
  if(all){clearTimelineFavoriteSelection();return;}
  favoriteSelection={};
  timelineMsgs.forEach(function(message){
    var status=timelineFavoriteStatusFor(message.msgid);
    if(!status||status.result!=='not_found')favoriteSelection[message.msgid]=true;
  });
  updateTimelineFavoriteControls();
  updateTimelineFavoriteRows();
}

function bumpTimelineFavoriteVersion(msgid){
  favoriteStateVersions[msgid]=(favoriteStateVersions[msgid]||0)+1;
  return favoriteStateVersions[msgid];
}

function loadTimelineFavoriteStatuses(messages,convId,requestGen){
  if(favoriteBusy)return Promise.resolve();
  var targets=timelineFavoriteTargets(messages);
  if(!targets.length)return Promise.resolve();
  var versions={};
  targets.forEach(function(message){
    versions[message.msgid]=bumpTimelineFavoriteVersion(message.msgid);
    favoriteStates[message.msgid]={result:'pending',isFavorited:null};
  });
  favoriteStatusFailed=false;
  updateTimelineFavoriteControls();
  updateTimelineFavoriteRows(targets.map(function(message){return message.msgid;}));
  var batches=timelineFavoriteChunks(targets);
  return batches.reduce(function(promise,batch){
    return promise.then(function(){
      return timelineFavoriteRequest('/api/favorites/status',{items:timelineFavoritePayload(batch)})
        .then(function(data){
          if(!data||!Array.isArray(data.items))throw new Error('invalid_favorite_status');
          if(timelineConvId!==convId||timelineRequestGen!==requestGen)return;
          var removed=[];
          data.items.forEach(function(item){
            if(item.object_type!=='message'||typeof item.object_id!=='string')return;
            if(!Object.prototype.hasOwnProperty.call(versions,item.object_id)||favoriteStateVersions[item.object_id]!==versions[item.object_id])return;
            if(item.result==='not_found'){
              favoriteStates[item.object_id]={result:'not_found',isFavorited:null};
              delete favoriteSelection[item.object_id];
              removed.push(item.object_id);
            }else if(item.result==='found'&&typeof item.is_favorited==='boolean'){
              favoriteStates[item.object_id]={result:'found',isFavorited:item.is_favorited};
            }
          });
          if(removed.length)removeTimelineFavoriteRows(removed,true);
          updateTimelineFavoriteControls();
          updateTimelineFavoriteRows(data.items.map(function(item){return item.object_id;}));
        });
    });
  },Promise.resolve()).then(function(){
    if(timelineConvId===convId&&timelineRequestGen===requestGen&&favoriteStatusRetrying){
      favoriteStatusRetrying=false;
      setTimelineFavoriteResult('',false,false);
    }
  }).catch(function(){
    if(timelineConvId===convId&&timelineRequestGen===requestGen){
      favoriteStatusFailed=true;
      favoriteStatusRetrying=false;
      setTimelineFavoriteResult(I18N.t('favorites.statusFailed'),true,false);
      updateTimelineFavoriteControls();
    }
  });
}

function retryTimelineFavoriteStatuses(){
  if(!timelineMsgs.length){favoriteStatusFailed=false;favoriteStatusRetrying=false;setTimelineFavoriteResult('',false,false);return;}
  favoriteStatusFailed=false;
  favoriteStatusRetrying=true;
  setTimelineFavoriteResult(I18N.t('favorites.statusLoading'),false,false);
  loadTimelineFavoriteStatuses(timelineMsgs.slice(),timelineConvId,timelineRequestGen);
}

function setTimelineFavoritesOnly(checked){
  timelineFavoritesOnly=!!checked;
  clearTimelineFavoriteSelection();
  favoriteStates={};
  favoriteStateVersions={};
  favoriteStatusFailed=false;
  favoriteStatusRetrying=false;
  setTimelineFavoriteResult('',false,false);
  updateTimelineFavoriteModeButton();
  if(selConvId)loadTimeline(selConvId,timelineConvType);
}

function setTimelineFavoriteResult(message,error,showFavoritesLink){
  var result=document.getElementById('timeline-favorite-result');
  if(result){result.textContent=message||'';result.classList.toggle('favorite-result-error',!!error);result.hidden=!message;}
  var feedback=result&&result.parentNode;
  if(feedback)feedback.classList.toggle('has-result',!!message);
  var link=document.getElementById('timeline-favorite-link');
  if(link)link.hidden=!showFavoritesLink;
}

function timelineFavoriteSummaryMessage(summary,partial){
  return timelineFavoriteTranslate(partial?'favorites.partialResult':'favorites.result',{
    applied:String(summary.applied||0),
    unchanged:String(summary.unchanged||0),
    not_found:String(summary.not_found||0)
  });
}

function removeTimelineFavoriteRows(msgids,deleted){
  if(!msgids||!msgids.length)return;
  var remove={};msgids.forEach(function(id){remove[id]=true;});
  var before=timelineMsgs.length;
  var wasNearBottom=typeof isNearBottom==='function'?isNearBottom():false;
  timelineMsgs=timelineMsgs.filter(function(message){return !remove[message.msgid];});
  if(timelineMsgs.length===before)return;
  if(deleted&&selectedMsgId&&remove[selectedMsgId]){
    selectedMsgId=null;
    if(typeof renderPanelAuditEmpty==='function')renderPanelAuditEmpty();
  }
  var body=document.getElementById('timeline-body');
  var scrollTop=body?body.scrollTop:0;
  if(typeof applyTimelineRefresh==='function')applyTimelineRefresh(scrollTop,wasNearBottom,false);
  else renderTimeline(false);
}

function applyTimelineFavoriteItems(data,action,targets,versions,summary){
  if(!data||!Array.isArray(data.items))throw new Error('invalid_favorite_result');
  summary.requested+=Number(data.requested)||0;
  summary.unique+=Number(data.unique)||0;
  summary.applied+=Number(data.applied)||0;
  summary.unchanged+=Number(data.unchanged)||0;
  summary.not_found+=Number(data.not_found)||0;
  summary.items=summary.items.concat(data.items);
  var remove=[];
  data.items.forEach(function(item){
    if(item.object_type!=='message'||typeof item.object_id!=='string')return;
    var version=versions[item.object_id];
    if(!Object.prototype.hasOwnProperty.call(versions,item.object_id)||favoriteStateVersions[item.object_id]!==version)return;
    if(item.result==='not_found'){
      favoriteStates[item.object_id]={result:'not_found',isFavorited:null};
      delete favoriteSelection[item.object_id];
      remove.push(item.object_id);
    }else if(['favorited','already_favorited','unfavorited','already_unfavorited'].indexOf(item.result)!==-1){
      favoriteStates[item.object_id]={
        result:'found',
        isFavorited:item.result==='favorited'||item.result==='already_favorited'
      };
      bumpTimelineFavoriteVersion(item.object_id);
      if(timelineFavoritesOnly&&action==='unfavorite'&&!favoriteStates[item.object_id].isFavorited)remove.push(item.object_id);
    }
  });
  if(remove.length)removeTimelineFavoriteRows(remove,true);
  updateTimelineFavoriteRows(targets.map(function(message){return message.msgid;}));
  updateTimelineFavoriteControls();
}

function runTimelineFavoriteAction(action,ids){
  if(favoriteBusy||!favoriteUserCanWrite||deleteMode)return Promise.resolve(false);
  var byId={};
  timelineMsgs.forEach(function(message){byId[message.msgid]=message;});
  var targets=(ids||[]).map(function(id){return byId[id];}).filter(function(message){
    return message&&timelineFavoriteStatusReady(message.msgid);
  });
  if(!targets.length)return Promise.resolve(false);
  var summary={requested:0,unique:0,applied:0,unchanged:0,not_found:0,items:[]};
  var requestConvId=timelineConvId, requestGen=timelineRequestGen;
  var versions={};
  targets.forEach(function(message){versions[message.msgid]=bumpTimelineFavoriteVersion(message.msgid);});
  favoriteBusy=true;
  setTimelineFavoriteResult('',false,false);
  updateTimelineFavoriteControls();
  updateTimelineFavoriteRows(targets.map(function(message){return message.msgid;}));
  var batches=timelineFavoriteChunks(targets);
  var completed=Promise.resolve();
  batches.forEach(function(batch){
    completed=completed.then(function(){
      return timelineFavoriteRequest('/api/favorites/batch',{
        action:action,items:timelineFavoritePayload(batch)
      }).then(function(data){
        if(timelineConvId===requestConvId&&timelineRequestGen===requestGen){
          applyTimelineFavoriteItems(data,action,targets,versions,summary);
        }else{
          if(!data||!Array.isArray(data.items))throw new Error('invalid_favorite_result');
          summary.requested+=Number(data.requested)||0;
          summary.unique+=Number(data.unique)||0;
          summary.applied+=Number(data.applied)||0;
          summary.unchanged+=Number(data.unchanged)||0;
          summary.not_found+=Number(data.not_found)||0;
          summary.items=summary.items.concat(data.items);
        }
      });
    });
  });
  return completed.then(function(){
    favoriteBusy=false;
    if(timelineConvId===requestConvId&&timelineRequestGen===requestGen){
      favoriteSelection={};
      favoriteStatusFailed=false;
      setTimelineFavoriteResult(timelineFavoriteSummaryMessage(summary,false),false,true);
      updateTimelineFavoriteRows(targets.map(function(message){return message.msgid;}));
      var missing=timelineMsgs.filter(function(message){return !timelineFavoriteStatusReady(message.msgid);});
      if(missing.length)loadTimelineFavoriteStatuses(missing,requestConvId,requestGen);
    }
    updateTimelineFavoriteControls();
    return summary;
  }).catch(function(){
    favoriteBusy=false;
    if(timelineConvId!==requestConvId||timelineRequestGen!==requestGen){
      updateTimelineFavoriteControls();
      return false;
    }
    var currentTargets=targets.filter(function(message){return !!timelineFavoriteMessage(message.msgid);});
    currentTargets.forEach(function(message){favoriteStates[message.msgid]={result:'pending',isFavorited:null};});
    setTimelineFavoriteResult(
      summary.requested?timelineFavoriteSummaryMessage(summary,true):I18N.t('favorites.actionFailed'),
      true,summary.requested>0
    );
    updateTimelineFavoriteControls();
    updateTimelineFavoriteRows(currentTargets.map(function(message){return message.msgid;}));
    var missing=timelineMsgs.filter(function(message){return !timelineFavoriteStatusReady(message.msgid);});
    if(missing.length)loadTimelineFavoriteStatuses(missing,requestConvId,requestGen);
    return false;
  });
}

function runTimelineFavoriteSelection(action){
  var ids=Object.keys(favoriteSelection).filter(function(id){return favoriteSelection[id];});
  return runTimelineFavoriteAction(action,ids);
}

function toggleSingleMessageFavorite(msgid){
  if(favoriteBusy||!favoriteUserCanWrite||deleteMode||!timelineFavoriteStatusReady(msgid))return;
  var status=timelineFavoriteStatusFor(msgid);
  return runTimelineFavoriteAction(status.isFavorited?'unfavorite':'favorite',[msgid]);
}

function initTimelineFavorites(){
  var button=document.getElementById('btn-favorite-mode');
  if(button&&button.addEventListener)button.addEventListener('click',toggleTimelineFavoriteMode);
  var selectAll=document.getElementById('btn-favorite-select-all');
  if(selectAll&&selectAll.addEventListener)selectAll.addEventListener('click',toggleFavoriteSelectionAll);
  var clear=document.getElementById('btn-favorite-clear-selection');
  if(clear&&clear.addEventListener)clear.addEventListener('click',clearTimelineFavoriteSelection);
  var exit=document.getElementById('btn-favorite-exit');
  if(exit&&exit.addEventListener)exit.addEventListener('click',toggleTimelineFavoriteMode);
  var favorite=document.getElementById('btn-favorite-selected');
  if(favorite&&favorite.addEventListener)favorite.addEventListener('click',function(){runTimelineFavoriteSelection('favorite');});
  var unfavorite=document.getElementById('btn-unfavorite-selected');
  if(unfavorite&&unfavorite.addEventListener)unfavorite.addEventListener('click',function(){runTimelineFavoriteSelection('unfavorite');});
  var retry=document.getElementById('timeline-favorite-status-retry');
  if(retry&&retry.addEventListener)retry.addEventListener('click',retryTimelineFavoriteStatuses);
  var filter=document.getElementById('timeline-favorites-only');
  if(filter&&filter.addEventListener)filter.addEventListener('change',function(){setTimelineFavoritesOnly(filter.checked);});
  if(document.addEventListener)document.addEventListener('keydown',function(event){
    if(event.key!=='Escape'||!favoriteMode)return;
    favoriteMode=false;
    clearTimelineFavoriteSelection();
    updateTimelineFavoriteModeRows();
    var modeButton=document.getElementById('btn-favorite-mode');
    if(modeButton&&modeButton.focus)modeButton.focus();
  });
  updateTimelineFavoriteModeButton();
  updateTimelineFavoriteControls();
}
