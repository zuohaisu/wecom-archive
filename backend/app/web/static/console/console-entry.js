function applyStaticI18n(){
  document.documentElement.lang=I18N.getLocale();
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent=I18N.t(el.getAttribute('data-i18n'));
  });
}
function renderLangMenu(){
  var menu=document.getElementById('lang-menu');
  if(!menu)return;
  var current=I18N.getLocale();
  var html='';
  I18N.availableLocales().forEach(function(loc){
    var cls='lang-option'+(loc.code===current?' active':'');
    html+='<div class="'+cls+'" onclick="selectLocale(&quot;'+loc.code+'&quot;)">'+esc(loc.nativeName)+'</div>';
  });
  menu.innerHTML=html;
}
function toggleLangMenu(){
  var menu=document.getElementById('lang-menu');
  if(!menu)return;
  if(menu.style.display==='block'){menu.style.display='none';return;}
  renderLangMenu();
  menu.style.display='block';
}
function selectLocale(code){
  I18N.setLocale(code);
  var menu=document.getElementById('lang-menu');
  if(menu)menu.style.display='none';
  applyLocale();
}
function applyLocale(){
  applyStaticI18n();
  renderLangMenu();
  rebuildMediaLabels();
  document.getElementById('entity-header').textContent=mode==='staff'?I18N.t('console.monitoredAccounts'):I18N.t('console.contactsHeader');
  document.getElementById('conv-header').textContent=selEntityName?(I18N.t('nav.conversations')+' — '+selEntityName):I18N.t('nav.conversations');
  document.getElementById('timeline-header').textContent=selConvName?(I18N.t('console.timelineHeader')+' — '+selConvName):I18N.t('console.timelineHeader');
  if(lastEntityItems)renderEntityList(lastEntityItems);
  if(selEntityId&&lastConvItems){
    renderConvList(lastConvItems);
  }else if(!selEntityId){
    document.getElementById('conv-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectAccountOrContact')+'</div>';
  }
  if(timelineConvId&&timelineMsgs.length){
    renderTimeline(false);
  }else if(!timelineConvId){
    document.getElementById('timeline-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  }
  updateRefreshStatus();
  // QA fix: everything below is dynamic content built via I18N.t(...) at
  // RENDER time (not a static data-i18n element applyStaticI18n() already
  // refreshed above) -- each one previously stayed in whatever language it
  // was in when it was last rendered until the user triggered a re-render
  // some other way (search input placeholder/enter-hint, the selected
  // scope label, an open locator bar, visible search filter chips, and the
  // detail panel's audit/info tab content).
  var searchInput=document.getElementById('search-input');
  if(searchInput)searchInput.placeholder=I18N.t('search.placeholder');
  if(selEntityId){updateScopeButton(selEntityName);}
  else{var scopeLbl=document.getElementById('scope-label');if(scopeLbl)scopeLbl.textContent=I18N.t('console.pickScope');}
  if(searchHits.length)updateLocatorText();
  var filtersEl=document.getElementById('search-filters');
  if(filtersEl&&filtersEl.style.display!=='none')renderSearchFilters();
  // QA fix (round 2): refresh BOTH panel bodies regardless of which tab is
  // currently the visible one -- a locale switch while looking at 会话信息
  // must not leave 消息详情 (or vice versa) stuck showing stale-language
  // content the next time the user flips tabs, and the "nothing selected"
  // empty states need refreshing just as much as populated content (they
  // are baked-in translated strings with no data-i18n hook of their own,
  // not something applyStaticI18n() above already covers).
  var selMsg=selectedMsgId?findTimelineMessage(selectedMsgId):null;
  if(selMsg)renderPanelAudit(selMsg);else renderPanelAuditEmpty();
  if(selConvId&&convDetailCache[selConvId]){
    renderPanelInfo(convDetailCache[selConvId]);
  }else if(!selConvId){
    var infoBody=document.getElementById('panel-info-body');
    if(infoBody)infoBody.innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  }
  // QA fix (round 3): showFocusBanner() only sets its text via I18N.t() at
  // CREATION time and is a no-op if the banner already exists (see its own
  // early-return) -- once shown, it lived on screen for the rest of the
  // session, so a locale switch while it's visible left it stuck in
  // whatever language it was created in.
  var focusBanner=document.getElementById('focus-banner');
  if(focusBanner)focusBanner.textContent=I18N.t('search.jumpBack');
}
document.addEventListener('click',function(e){
  var sw=document.getElementById('lang-switch');
  var menu=document.getElementById('lang-menu');
  if(sw&&menu&&!sw.contains(e.target))menu.style.display='none';
});
function setMode(m){
  mode=m; selEntityId=null; selConvId=null; selEntityName=null; selConvName=null;
  lastConvItems=null;
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
  document.getElementById('tab-staff').classList.toggle('active',m==='staff');
  document.getElementById('tab-contact').classList.toggle('active',m==='contact');
  document.getElementById('entity-header').textContent=m==='staff'?I18N.t('console.monitoredAccounts'):I18N.t('console.contactsHeader');
  document.getElementById('conv-header').textContent=I18N.t('nav.conversations');
  document.getElementById('timeline-header').textContent=I18N.t('console.timelineHeader');
  document.getElementById('conv-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectAccountOrContact')+'</div>';
  document.getElementById('timeline-body').innerHTML='<div class="empty-state">'+I18N.t('console.selectConversation')+'</div>';
  hideNewMessageIndicator();
  // QA fix (round 2): selEntityId/selEntityName above are cleared, but the
  // "监控范围：X" scope-label/avatar updateScopeButton() set on selection
  // is JS-managed (see the template comment on #scope-label) and was
  // never reset here -- switching staff<->contact left the OLD entity's
  // name showing at the top even though the conversation list underneath
  // had already gone back to "select an account or contact", which reads
  // as if that entity were still the active review scope.
  var scopeLbl=document.getElementById('scope-label');
  if(scopeLbl)scopeLbl.textContent=I18N.t('console.pickScope');
  var scopeAvatar=document.getElementById('scope-avatar');
  if(scopeAvatar)scopeAvatar.textContent='?';
  // QA fix: switching staff<->contact previously left the right panel
  // showing whatever the LAST selected entity/conversation/message had
  // populated (a stale 会话信息/消息详情 that no longer belongs to any
  // visible selection) -- a real reviewer-misattribution risk. No entity
  // or conversation is selected immediately after a mode switch, so the
  // panel must go back to its "nothing selected" state, not linger.
  resetPanelForNoConversation();
  // QA fix (search-then-select race): return the entity-list fetch
  // promise so a caller that needs to act on the FRESH list (not
  // whatever lastEntityItems held before this call) can chain off it --
  // see onSearchContactItemClick(), which used to check lastEntityItems
  // synchronously right after calling setMode(), before the fetch it
  // triggers had even resolved.
  return loadEntityList();
}
function highlightKeyword(text,keyword){
  if(!keyword)return text;
  var re=new RegExp('('+keyword.replace(/[.*+?^${}()|[\]\\]/g,'\$&')+')','gi');
  return text.replace(re,'<span class="sr-highlight">$1</span>');
}
// Archive Console v2: search results render inline in the conv-list column
// (replacing the old floating .search-results dropdown), with real filter
// chips backed by /api/search/messages's existing date_range/msgtype params
// (backend/app/routers/search.py) and per-hit locate (see onSearchHitClick/
// the locator bar functions below) instead of only opening the conversation.
function setSearchActive(active){
  var convBody=document.getElementById('conv-body');
  var typeTabs=document.getElementById('conv-type-tabs');
  var filters=document.getElementById('search-filters');
  var results=document.getElementById('search-results');
  if(convBody)convBody.style.display=active?'none':'';
  if(typeTabs)typeTabs.style.display=active?'none':'';
  if(filters)filters.style.display=active?'block':'none';
  if(results)results.style.display=active?'block':'none';
  if(active)renderSearchFilters();
}
function renderSearchFilters(){
  var el=document.getElementById('search-filters');
  if(!el)return;
  var dateOpts=[[null,'search.filter.dateAll'],['1d','search.filter.date1d'],['7d','search.filter.date7d'],['30d','search.filter.date30d'],['90d','search.filter.date90d']];
  var html='<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">';
  dateOpts.forEach(function(o){
    var active=searchDateRange===o[0];
    var arg=o[0]?("'"+o[0]+"'"):'null';
    html+='<button type="button" class="chip'+(active?' active':'')+'" onclick="setSearchDateRange('+arg+')">'+esc(I18N.t(o[1]))+'</button>';
  });
  html+='</div><div style="display:flex;gap:6px;flex-wrap:wrap">'
    +'<button type="button" class="chip'+(!searchAllTypes?' active':'')+'" onclick="setSearchAllTypes(false)">'+esc(I18N.t('search.filter.msgtype'))+': text</button>'
    +'<button type="button" class="chip'+(searchAllTypes?' active':'')+'" onclick="setSearchAllTypes(true)">'+esc(I18N.t('console.convTypeAll'))+'</button>'
    +'</div>';
  el.innerHTML=html;
}
function setSearchDateRange(v){searchDateRange=v;renderSearchFilters();forceSearch();}
function setSearchAllTypes(v){searchAllTypes=v;renderSearchFilters();forceSearch();}
function forceSearch(){searchLastQ=null;doSearch();}
function buildSearchMsgUrl(q){
  var url='/api/search/messages?q='+encodeURIComponent(q)+'&limit=10';
  if(searchDateRange)url+='&date_range='+encodeURIComponent(searchDateRange);
  if(searchAllTypes&&typeof RND216_MSGTYPE_OPTIONS!=='undefined'){
    RND216_MSGTYPE_OPTIONS.forEach(function(opt){
      (opt.rawValues||[]).forEach(function(rv){url+='&msgtype='+encodeURIComponent(rv);});
    });
  }
  return url;
}
function doSearch(){
  var input=document.getElementById('search-input');
  var results=document.getElementById('search-results');
  var q=input.value.trim();
  if(q===searchLastQ)return;
  searchLastQ=q;
  if(!q){setSearchActive(false);clearLocator();searchHits=[];return;}
  setSearchActive(true);
  results.innerHTML='<div class="sr-loading">'+I18N.t('search.loading')+'</div>';
  var contactUrl='/api/search/contacts?q='+encodeURIComponent(q)+'&limit=5';
  var msgUrl=buildSearchMsgUrl(q);
  var contactDone=false,msgDone=false,contactError=false,msgError=false;
  var contactData=null,msgData=null;
  function renderResults(){
    if(!contactDone||!msgDone)return;
    if(contactError&&msgError){
      results.innerHTML='<div class="sr-error">'+I18N.t('search.error')+'</div>';
      return;
    }
    if((!contactData||!contactData.length)&&(!msgData||!msgData.results||!msgData.results.length)){
      results.innerHTML='<div class="sr-empty">'+I18N.t('search.noResults')+'</div>';
      searchHits=[];
      return;
    }
    var html='';
    if(contactData&&contactData.length){
      html+='<div class="sr-section"><div class="sr-section-header">'+I18N.t('search.contacts')+' ('+contactData.length+')</div>';
      contactData.forEach(function(c){
        html+='<div class="sr-item" data-search-contact="'+esc(c.wecom_userid)+'"><div class="sr-item-main">'
          +'<span class="sr-item-name">'+esc(c.display_name)+'</span>'
          +'<span class="sr-item-raw">'+esc(c.wecom_userid)+'</span>'
          +'</div></div>';
      });
      html+='</div>';
    }
    if(contactData&&contactData.length&&msgData&&msgData.results&&msgData.results.length){
      html+='<div class="sr-divider"></div>';
    }
    // Ordered hit registry the locator bar steps through -- populated in
    // API result order (same order rendered), reset on every fresh search.
    searchHits=[];
    if(msgData&&msgData.results&&msgData.results.length){
      html+='<div class="sr-section"><div class="sr-section-header">'+I18N.t('search.messages')+' ('+msgData.results.length+')</div>';
      msgData.results.forEach(function(m){
        var hitIndex=searchHits.length;
        searchHits.push({msgid:m.msgid,convId:m.conversation_id,convType:m.conversation_type,entityId:m.entity_id||'',entityType:m.entity_type||''});
        var sender=esc(m.sender_display_name);
        var conv=esc(m.conversation_name);
        var snippet=highlightKeyword(esc(m.content_snippet),q);
        var t=m.msgtime?fmtTime(m.msgtime):'';
        html+='<div class="sr-item" data-search-hit="'+hitIndex+'">'
          +'<div class="sr-item-main"><span class="sr-item-name">'+sender+'</span><span class="sr-item-conv">'+conv+'</span><span class="sr-item-time">'+t+'</span></div>'
          +'<div class="sr-item-snippet">'+snippet+'</div>'
          +'</div>';
      });
      html+='</div>';
    }
    results.innerHTML=html;
    attachSearchItemEvents();
  }
  contactDone=false;msgDone=false;contactError=false;msgError=false;
  contactData=null;msgData=null;
  fetch(contactUrl).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){
    contactData=d;contactDone=true;renderResults();
  }).catch(function(){
    contactError=true;contactDone=true;renderResults();
  });
  fetch(msgUrl).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){
    msgData=d;msgDone=true;renderResults();
  }).catch(function(){
    msgError=true;msgDone=true;renderResults();
  });
}
function attachSearchItemEvents(){
  document.querySelectorAll('[data-search-contact]').forEach(function(el){
    el.removeEventListener('click',onSearchContactItemClick);
    el.addEventListener('click',onSearchContactItemClick);
  });
  document.querySelectorAll('[data-search-hit]').forEach(function(el){
    el.removeEventListener('click',onSearchHitClick);
    el.addEventListener('click',onSearchHitClick);
  });
}
// Finds an already-rendered entity-item by id and selects it. Returns
// whether it was found -- callers decide what "not found" means.
function selectEntityIfPresent(wecomUserId){
  var entityBody=document.getElementById('entity-body');
  if(!entityBody)return false;
  var found=false;
  entityBody.querySelectorAll('.entity-item').forEach(function(el){
    if(el.dataset.id===wecomUserId){onEntityClick(el);found=true;}
  });
  return found;
}
// QA fix: a contact-search result's wecom_userid could resolve to either
// a monitored account (staff) or an external contact -- /api/search/contacts
// doesn't classify which, so this must actually check both lists. The
// previous version called setMode('staff') (which kicks off an ASYNC
// entity-list fetch) and then immediately read lastEntityItems
// SYNCHRONOUSLY on the very next line -- that's still whatever the
// PREVIOUS mode last loaded (or null), never the fresh staff list, so the
// contact branch fired almost every time regardless of where the id
// actually was. setMode() now returns the entity-list fetch promise
// specifically so this can wait for the real, current list before
// deciding.
function onSearchContactItemClick(){
  var wecomUserId=this.dataset.searchContact;
  if(!wecomUserId)return;
  document.getElementById('search-input').value='';
  setSearchActive(false);
  searchLastQ='';
  setMode('staff').then(function(){
    if(selectEntityIfPresent(wecomUserId))return;
    return setMode('contact').then(function(){
      selectEntityIfPresent(wecomUserId);
    });
  });
}
// Archive Console v2: a message-search hit now locates and flashes the
// EXACT matched message (msgid was already returned by /api/search/messages
// but previously unused for navigation -- only the conversation was
// opened) and arms the locator bar for prev/next stepping through every
// hit loaded in this search, without leaving the console.
function onSearchHitClick(){
  var idx=parseInt(this.getAttribute('data-search-hit'),10);
  var hit=searchHits[idx];
  if(!hit)return;
  locatorIndex=idx+1;
  document.getElementById('search-input').value='';
  setSearchActive(false);
  searchLastQ='';
  showLocatorBar();
  // Bug fix: this is an in-page locate, not a cross-page arrival -- see
  // focusIsUrlArrival's declaration comment in console-state.js.
  focusIsUrlArrival=false;
  focusMessage(hit.msgid,hit.convId,hit.convType,hit.entityId,hit.entityType);
}

// ---------------------------------------------------------------------------
// Locator bar: steps locatorIndex through searchHits (the last-loaded,
// API-ordered /api/search/messages results), reusing the existing
// focusMessage/focusCheckRow navigate-and-flash flow for each step instead
// of a second implementation. "M" is the LOADED hit count, not a fabricated
// global total -- MessageSearchPagination is cursor-based (has_older/
// next_before) with no total-count field.
// ---------------------------------------------------------------------------
function showLocatorBar(){
  var bar=document.getElementById('locator-bar');
  if(bar)bar.style.display='flex';
  updateLocatorText();
}
function updateLocatorText(){
  var el=document.getElementById('locator-text');
  if(!el)return;
  el.textContent=I18N.t('locator.position').replace('{n}',locatorIndex).replace('{m}',searchHits.length);
}
function clearLocator(){
  locatorIndex=0;
  var bar=document.getElementById('locator-bar');
  if(bar)bar.style.display='none';
}
function locatorPrev(){
  if(locatorIndex<=1)return;
  locatorIndex--;
  var hit=searchHits[locatorIndex-1];
  if(!hit)return;
  updateLocatorText();
  focusIsUrlArrival=false; // in-page locate -- see console-state.js
  focusMessage(hit.msgid,hit.convId,hit.convType,hit.entityId,hit.entityType);
}
function locatorNext(){
  if(locatorIndex>=searchHits.length)return;
  locatorIndex++;
  var hit=searchHits[locatorIndex-1];
  if(!hit)return;
  updateLocatorText();
  focusIsUrlArrival=false; // in-page locate -- see console-state.js
  focusMessage(hit.msgid,hit.convId,hit.convType,hit.entityId,hit.entityType);
}

// ---------------------------------------------------------------------------
// Archive Console v2 — audit-mode toggle, detail-panel toggle/tabs, and the
// monitoring-scope popover (replaces the old always-visible entity column).
// ---------------------------------------------------------------------------
function toggleAuditMode(){
  auditMode=!auditMode;
  var btn=document.getElementById('btn-audit-mode');
  if(btn)btn.classList.toggle('active',auditMode);
  if(timelineConvId&&timelineMsgs.length)renderTimeline(false);
}
function togglePanel(){
  panelOpen=!panelOpen;
  var col=document.getElementById('panel-col');
  var btn=document.getElementById('btn-panel-toggle');
  if(col)col.style.display=panelOpen?'flex':'none';
  if(btn)btn.classList.toggle('active',panelOpen);
}
function setPanelTab(tab){
  panelTab=tab;
  var infoTab=document.getElementById('panel-tab-info');
  var auditTab=document.getElementById('panel-tab-audit');
  var infoBody=document.getElementById('panel-info-body');
  var auditBody=document.getElementById('panel-audit-body');
  if(infoTab)infoTab.classList.toggle('active',tab==='info');
  if(auditTab)auditTab.classList.toggle('active',tab==='audit');
  if(infoBody)infoBody.style.display=(tab==='info')?'':'none';
  if(auditBody)auditBody.style.display=(tab==='audit')?'':'none';
}
function toggleScopePopover(){
  scopePopoverOpen=!scopePopoverOpen;
  var pop=document.getElementById('scope-popover');
  if(pop)pop.style.display=scopePopoverOpen?'flex':'none';
}
document.addEventListener('click',function(e){
  var btn=document.getElementById('scope-btn');
  var pop=document.getElementById('scope-popover');
  if(btn&&pop&&scopePopoverOpen&&!btn.contains(e.target)&&!pop.contains(e.target)){
    closeScopePopover();
  }
});

// RND-229: jump back from the search results page and highlight the target message.
// (focusMsgId is declared once near the top of this script, before readFocusFromUrl()
//  runs during init, so its value is not reset after being set.)
function focusMessage(msgid, convId, convType, entityId, entityType){
  if(!convId||!msgid)return;
  focusMsgId=msgid;
  // QA fix: applyConvTypeFilter() only hides non-matching conv-card
  // elements from the DOM -- it never renders them at all. If the
  // currently active 全部/群聊/单聊 filter doesn't match the target
  // conversation's type, waitForConv below can never find its card and
  // silently times out (locator bar shows a hit count, timeline never
  // loads). Locating a message must always be able to reach it regardless
  // of the current filter.
  if(convTypeFilter!=='all')setConvTypeFilter('all');
  var targetMode=(entityType==='staff')?'staff':'contact';
  // RND-240: if the target entity/conversation is already the active
  // selection, skip the entire re-fetch chain below (setMode -> reload
  // entity list -> waitForEntity poll -> reload conversation list ->
  // waitForConv poll) -- that chain redundantly re-pulls data the console
  // already has on screen, and its polling fallbacks are what stretched a
  // same-conversation locate to ~8s. Only short-circuit when the filter is
  // already 'all' too, so this never bypasses the setConvTypeFilter call
  // above.
  if(mode===targetMode&&selEntityId===entityId&&convTypeFilter==='all'){
    var cb=document.getElementById('conv-body');
    var cards=cb?cb.querySelectorAll('.conv-card'):[];
    var hit=null;
    Array.prototype.forEach.call(cards,function(card){
      if(card.dataset.id===convId)hit=card;
    });
    if(hit){
      focusMsgId=msgid;
      if(timelineConvId===convId&&timelineMsgs.length){
        focusCheckRow();
      }else{
        onConvClick(hit);
      }
      return;
    }
  }
  setMode(targetMode);
  var focusAttempts=0;
  var waitForEntity=function(){
    if(++focusAttempts>40)return; // ~8s cap; give up gracefully if the entity never appears
    var entityBody=document.getElementById('entity-body');
    var els=entityBody?entityBody.querySelectorAll('.entity-item'):[];
    var found=false;
    Array.prototype.forEach.call(els,function(el){
      if(el.dataset.id===entityId){onEntityClick(el);found=true;}
    });
    if(found){
      var waitForConv=setInterval(function(){
        var convBody=document.getElementById('conv-body');
        var cards=convBody?convBody.querySelectorAll('.conv-card'):[];
        var cfound=false;
        Array.prototype.forEach.call(cards,function(card){
          if(card.dataset.id===convId){onConvClick(card);cfound=true;}
        });
        if(cfound){clearInterval(waitForConv);focusPending=true;}
        setTimeout(function(){clearInterval(waitForConv);},8000);
      },100);
    }else{
      setTimeout(waitForEntity,200);
    }
  };
  setTimeout(waitForEntity,200);
}
function focusCheckRow(){
  if(!focusMsgId)return;
  var row=document.querySelector('[data-msgid="'+focusMsgId+'"]');
  if(row){
    row.scrollIntoView({behavior:'smooth',block:'center'});
    row.classList.add('target-flash');
    row.addEventListener('animationend',function(){row.classList.add('target-active');},{once:true});
    // Bug fix: only show the "← 返回搜索结果" banner (history.back()) for a
    // genuine cross-page arrival from /admin/search -- see
    // focusIsUrlArrival's declaration in console-state.js. An in-page
    // locate already has the locator bar as its "return" UI; showing this
    // banner too was sending users to whatever page preceded this tab
    // (typically /admin/login) when they clicked it after an in-page
    // search hit, since no actual page navigation had occurred to undo.
    if(focusIsUrlArrival)showFocusBanner();
    return;
  }
  if(timelineHasOlder){
    var requestConvId=timelineConvId, gen=timelineRequestGen, before=timelineNextBefore;
    fetchOlderMessages(requestConvId,before).then(function(){
      if(timelineConvId!==requestConvId||timelineRequestGen!==gen)return;
      renderTimeline(false);
      setTimeout(focusCheckRow,250);
    }).catch(function(){
      if(timelineConvId!==requestConvId||timelineRequestGen!==gen)return;
    });
  }
}
function showFocusBanner(){
  if(document.getElementById('focus-banner'))return;
  var b=document.createElement('div');
  b.id='focus-banner';
  b.style.cssText='position:fixed;left:50%;top:8px;transform:translateX(-50%);z-index:300;background:#1890ff;color:#fff;padding:.35rem .8rem;border-radius:4px;font-size:.8rem;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.2)';
  b.textContent=I18N.t('search.jumpBack');
  b.onclick=function(){history.back();};
  document.body.appendChild(b);
}
function readFocusFromUrl(){
  var p=new URLSearchParams(location.search);
  var f=p.get('focus');
  if(!f)return;
  // A real cross-page arrival -- the previous history entry is the
  // standalone search-results page, so history.back() (the banner's
  // action) is correct here. See focusIsUrlArrival's declaration comment.
  focusIsUrlArrival=true;
  focusMessage(f,p.get('conv'),p.get('convType'),p.get('entityId'),p.get('entityType'));
}

function onSearchInput(){
  var input=document.getElementById('search-input');
  if(searchTimer)clearTimeout(searchTimer);
  if(!input.value.trim()){
    searchLastQ='';
    setSearchActive(false);
    clearLocator();
    searchHits=[];
    return;
  }
  searchTimer=setTimeout(doSearch,300);
}
// Archive Console v2: results now render inline in the conv-list column
// (replacing the old floating dropdown), so visibility is driven purely by
// whether there's an active query (setSearchActive), not input focus --
// unlike a floating dropdown, hiding inline results on blur would fight the
// user clicking a filter chip, a hit, or anything else in the column.
document.getElementById('search-input').addEventListener('input',onSearchInput);
document.getElementById('search-input').addEventListener('keydown',function(e){
  if(e.key==='Enter'){
    var v=this.value.trim();
    if(v){e.preventDefault();window.location.href='/admin/search?q='+encodeURIComponent(v);}
  }
});

// RND-217 C5: bootstrap runs LAST -- every other top-level side effect
// (the lang-menu outside-click handler and all four #search-input
// listeners above) is registered before we kick off the initial load, so
// the first render/auto-refresh never races an unregistered listener.
// QA fix (round 2): a persisted non-default locale (I18N.getLocale() reads
// it back at load) previously only got applyStaticI18n()'s data-i18n pass
// here -- the dynamic content applyLocale() ALSO refreshes (search
// placeholder, scope label, panel empty states, ...) stayed in the
// default locale's baked-in text until the user manually reopened the
// language menu and reselected the same locale. applyLocale() already
// calls applyStaticI18n() first, so this is a strict superset, safe to
// call this early (nothing is loaded yet -- every "if(lastEntityItems)"/
// "if(selEntityId)" branch inside it is a no-op at this point).
applyLocale();
loadCurrentUser();
setMode('staff');
lastRefreshAt=Date.now();
updateRefreshStatus();
startAutoRefresh();
readFocusFromUrl();
