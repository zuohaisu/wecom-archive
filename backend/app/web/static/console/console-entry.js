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
  loadEntityList();
}
function highlightKeyword(text,keyword){
  if(!keyword)return text;
  var re=new RegExp('('+keyword.replace(/[.*+?^${}()|[\]\\]/g,'\$&')+')','gi');
  return text.replace(re,'<span class="sr-highlight">$1</span>');
}
function doSearch(){
  var input=document.getElementById('search-input');
  var results=document.getElementById('search-results');
  var q=input.value.trim();
  if(q===searchLastQ)return;
  searchLastQ=q;
  if(!q){results.style.display='none';return;}
  results.innerHTML='<div class="sr-loading">'+I18N.t('search.loading')+'</div>';
  results.style.display='block';
  setTimeout(function(){results.style.maxHeight='';},50);
  var contactUrl='/api/search/contacts?q='+encodeURIComponent(q)+'&limit=5';
  var msgUrl='/api/search/messages?q='+encodeURIComponent(q)+'&limit=10';
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
    if(msgData&&msgData.results&&msgData.results.length){
      html+='<div class="sr-section"><div class="sr-section-header">'+I18N.t('search.messages')+' ('+msgData.results.length+')</div>';
      msgData.results.forEach(function(m){
        var sender=esc(m.sender_display_name);
        var conv=esc(m.conversation_name);
        var snippet=highlightKeyword(esc(m.content_snippet),q);
        var t=m.msgtime?fmtTime(m.msgtime):'';
        html+='<div class="sr-item"'
          +' data-search-msg="'+esc(m.conversation_id)+'"'
          +' data-search-msg-type="'+esc(m.conversation_type)+'"'
          +' data-search-msg-sid="'+esc(m.entity_id||'')+'"'
          +' data-search-msg-stype="'+esc(m.entity_type||'')+'">'
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
  document.querySelectorAll('[data-search-msg]').forEach(function(el){
    el.removeEventListener('click',onSearchMsgItemClick);
    el.addEventListener('click',onSearchMsgItemClick);
  });
}
function onSearchContactItemClick(){
  var wecomUserId=this.dataset.searchContact;
  if(!wecomUserId)return;
  document.getElementById('search-input').value='';
  document.getElementById('search-results').style.display='none';
  searchLastQ='';
  setMode('staff');
  var foundInStaff=false;
  if(lastEntityItems){
    lastEntityItems.forEach(function(it){
      if(it.staff_id===wecomUserId||it.monitored_account_id===wecomUserId)foundInStaff=true;
    });
  }
  if(foundInStaff){
    var entityBody=document.getElementById('entity-body');
    var els=entityBody.querySelectorAll('.entity-item');
    els.forEach(function(el){
      if(el.dataset.id===wecomUserId)onEntityClick(el);
    });
  }else{
    setMode('contact');
    var checkInterval=setInterval(function(){
      var entityBody=document.getElementById('entity-body');
      var els=entityBody.querySelectorAll('.entity-item');
      var found=false;
      els.forEach(function(el){
        if(el.dataset.id===wecomUserId){onEntityClick(el);found=true;}
      });
      if(found)clearInterval(checkInterval);
      setTimeout(function(){clearInterval(checkInterval);},5000);
    },100);
  }
}
function onSearchMsgItemClick(){
  var convId=this.dataset.searchMsg;
  var convType=this.dataset.searchMsgType;
  var entityId=this.dataset.searchMsgSid;
  var entityType=this.dataset.searchMsgStype;
  if(!convId)return;
  document.getElementById('search-input').value='';
  document.getElementById('search-results').style.display='none';
  searchLastQ='';
  // Navigate using API-provided entity context
  if(entityId&&entityType){
    // Navigate to the entity first
    var targetMode=entityType==='staff'?'staff':'contact';
    setMode(targetMode);
    var smAttempts=0;
    var waitForEntity=function(){
      if(++smAttempts>40)return; // ~8s cap; avoid infinite retry if entity missing
      var entityBody=document.getElementById('entity-body');
      var els=entityBody.querySelectorAll('.entity-item');
      var found=false;
      els.forEach(function(el){
        if(el.dataset.id===entityId){
          onEntityClick(el);
          found=true;
        }
      });
      if(found){
        // Wait for conv list to load, then select target conversation
        var waitForConv=setInterval(function(){
          var convBody=document.getElementById('conv-body');
          var cards=convBody.querySelectorAll('.conv-card');
          var cfound=false;
          cards.forEach(function(card){
            if(card.dataset.id===convId){
              onConvClick(card);
              cfound=true;
            }
          });
          if(cfound)clearInterval(waitForConv);
          setTimeout(function(){clearInterval(waitForConv);},5000);
        },100);
      }else{
        setTimeout(waitForEntity,200);
      }
    };
    setTimeout(waitForEntity,200);
    return;
  }
  // Fallback: try to find in current conv list
  if(lastConvItems){
    for(var i=0;i<lastConvItems.length;i++){
      if(lastConvItems[i].conversation_id===convId){
        var convBody=document.getElementById('conv-body');
        var cards=convBody.querySelectorAll('.conv-card');
        cards.forEach(function(card){
          if(card.dataset.id===convId)onConvClick(card);
        });
        return;
      }
    }
  }
}

// RND-229: jump back from the search results page and highlight the target message.
// (focusMsgId is declared once near the top of this script, before readFocusFromUrl()
//  runs during init, so its value is not reset after being set.)
function focusMessage(msgid, convId, convType, entityId, entityType){
  if(!convId||!msgid)return;
  focusMsgId=msgid;
  var targetMode=(entityType==='staff')?'staff':'contact';
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
    showFocusBanner();
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
  b.textContent='← 返回搜索结果';
  b.onclick=function(){history.back();};
  document.body.appendChild(b);
}
function readFocusFromUrl(){
  var p=new URLSearchParams(location.search);
  var f=p.get('focus');
  if(!f)return;
  focusMessage(f,p.get('conv'),p.get('convType'),p.get('entityId'),p.get('entityType'));
}

function onSearchInput(){
  var input=document.getElementById('search-input');
  var results=document.getElementById('search-results');
  if(searchTimer)clearTimeout(searchTimer);
  if(!input.value.trim()){
    searchLastQ='';
    results.style.display='none';
    return;
  }
  searchTimer=setTimeout(doSearch,300);
}
function onSearchBlur(){
  setTimeout(function(){document.getElementById('search-results').style.display='none';},200);
}
function onSearchFocus(){
  var results=document.getElementById('search-results');
  if(searchLastQ){results.style.display='block';}
}
document.getElementById('search-input').addEventListener('input',onSearchInput);
document.getElementById('search-input').addEventListener('blur',onSearchBlur);
document.getElementById('search-input').addEventListener('focus',onSearchFocus);
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
applyStaticI18n();
loadCurrentUser();
setMode('staff');
lastRefreshAt=Date.now();
updateRefreshStatus();
startAutoRefresh();
readFocusFromUrl();
