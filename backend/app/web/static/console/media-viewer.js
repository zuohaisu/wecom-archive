
// ---------------------------------------------------------------------------
// Unified Viewer -- one shared overlay for image/emotion/video preview and
// chatrecord nested-message browsing, built once and reused for every
// message (never one popup per message type). Never touches the timeline
// DOM/scroll position behind it, and is completely independent of the 30s
// auto-refresh poller / renderTimeline() re-renders. role="dialog"/
// aria-modal (RND-206 QA fix #11) with focus moved in on open and restored
// to the triggering element (or a safe fallback) on close.
// ---------------------------------------------------------------------------
var viewerItems=[];
var viewerIndex=-1;
var viewerKeyHandlerBound=false;
// Bumped on every open/close so an in-flight descriptor fetch belonging to
// a viewer session that has since closed (or been reopened with different
// items) can never apply its result (RND-206 QA fix #5).
var viewerGen=0;
var viewerFocusTrigger=null;

// Registry of items the Viewer can page through for the CURRENT
// renderTimeline() pass -- reset at the top of renderTimeline() (see
// timeline.js), populated as each image/emotion/video element is rendered
// (see message-renderers.js) so prev/next navigates every viewable item
// across the whole visible timeline, not just siblings within one message.
// Kept here alongside the rest of the Viewer's state; message-renderers.js
// loads immediately before this file, but both only touch these globals at
// runtime (inside renderTimeline()/the composite renderers), never at load.
var timelineViewerItems=[];
function registerViewerItem(item){
  timelineViewerItems.push(item);
  return timelineViewerItems.length-1;
}

function ensureViewerRoot(){
  var root=document.getElementById('rnd206-viewer');
  if(root)return root;
  root=document.createElement('div');
  root.id='rnd206-viewer';
  root.className='v-overlay';
  root.style.display='none';
  root.setAttribute('role','dialog');
  root.setAttribute('aria-modal','true');
  root.setAttribute('aria-label',I18N.t('viewer.dialogLabel'));
  root.innerHTML=
    '<div class="v-backdrop" onclick="closeViewer()"></div>'
    +'<button type="button" class="v-close" onclick="closeViewer()" aria-label="'+esc(I18N.t('viewer.close'))+'">&times;</button>'
    +'<button type="button" class="v-nav v-prev" onclick="viewerShow(viewerIndex-1)" style="display:none" aria-label="'+esc(I18N.t('viewer.prev'))+'">&#8249;</button>'
    +'<button type="button" class="v-nav v-next" onclick="viewerShow(viewerIndex+1)" style="display:none" aria-label="'+esc(I18N.t('viewer.next'))+'">&#8250;</button>'
    +'<div class="v-body" id="rnd206-viewer-body"></div>';
  document.body.appendChild(root);
  if(!viewerKeyHandlerBound){
    document.addEventListener('keydown',function(e){
      var root2=document.getElementById('rnd206-viewer');
      if(!root2||root2.style.display==='none')return;
      if(e.key==='Escape')closeViewer();
      else if(e.key==='ArrowLeft')viewerShow(viewerIndex-1);
      else if(e.key==='ArrowRight')viewerShow(viewerIndex+1);
    });
    viewerKeyHandlerBound=true;
  }
  return root;
}
// RND-206 QA fix #11/#15: re-applies I18N labels to the persistent viewer
// chrome (built once by ensureViewerRoot and never rebuilt otherwise) so a
// runtime locale switch updates them without a page reload. If the viewer
// is currently open, also re-renders the current item so any visible
// loading/error/action copy picks up the new locale immediately.
function refreshViewerLabels(){
  var root=typeof document!=='undefined'?document.getElementById('rnd206-viewer'):null;
  if(!root)return;
  root.setAttribute('aria-label',I18N.t('viewer.dialogLabel'));
  var closeBtn=root.querySelector('.v-close');
  if(closeBtn)closeBtn.setAttribute('aria-label',I18N.t('viewer.close'));
  var prevBtn=root.querySelector('.v-prev');
  if(prevBtn)prevBtn.setAttribute('aria-label',I18N.t('viewer.prev'));
  var nextBtn=root.querySelector('.v-next');
  if(nextBtn)nextBtn.setAttribute('aria-label',I18N.t('viewer.next'));
  if(root.style.display!=='none'&&viewerIndex>=0)viewerShow(viewerIndex);
}
function openViewer(items,startIndex){
  viewerItems=items||[];
  viewerGen++;
  viewerFocusTrigger=typeof document!=='undefined'?document.activeElement:null;
  var root=ensureViewerRoot();
  root.style.display='flex';
  if(typeof document!=='undefined')document.body.style.overflow='hidden';
  viewerShow(startIndex||0);
  var closeBtn=root.querySelector('.v-close');
  if(closeBtn&&typeof closeBtn.focus==='function')closeBtn.focus();
}
function restoreViewerFocus(){
  var trigger=viewerFocusTrigger;
  viewerFocusTrigger=null;
  if(trigger&&typeof trigger.focus==='function'&&typeof document!=='undefined'&&document.body
     &&typeof document.body.contains==='function'&&document.body.contains(trigger)){
    trigger.focus();
    return;
  }
  if(typeof document==='undefined')return;
  var fallback=document.getElementById('timeline-body');
  if(fallback&&typeof fallback.focus==='function')fallback.focus();
}
function closeViewer(){
  viewerGen++;
  var root=document.getElementById('rnd206-viewer');
  if(root)root.style.display='none';
  if(typeof document!=='undefined')document.body.style.overflow='';
  viewerItems=[];
  viewerIndex=-1;
  restoreViewerFocus();
}
function openChatrecordViewer(node,depth){
  openViewer([{kind:'chatrecord',node:node,depth:depth||0}],0);
}
function viewerShow(idx){
  if(!viewerItems.length||idx<0||idx>=viewerItems.length)return;
  viewerIndex=idx;
  var gen=viewerGen;
  var root=ensureViewerRoot();
  var body=document.getElementById('rnd206-viewer-body');
  var item=viewerItems[idx];
  var multi=viewerItems.length>1&&item.kind!=='chatrecord';
  root.querySelector('.v-prev').style.display=(multi&&idx>0)?'block':'none';
  root.querySelector('.v-next').style.display=(multi&&idx<viewerItems.length-1)?'block':'none';
  if(item.kind==='chatrecord'){
    body.innerHTML='<div class="v-chatrecord"><div class="v-chatrecord-title">'
      +esc((item.node.fields&&item.node.fields.title)||I18N.t('chatrecord.title'))+'</div>'
      +renderCompositeChildren(item.node,item.depth||0)+'</div>';
    // RND-206 QA fix #3: viewer-mounted content (nested media inside a
    // chatrecord's expanded view) was never hydrated -- this is the exact
    // "viewer content is inserted without running the required hydration
    // flow" defect. hydrateRichMedia is the SAME engine used for the
    // timeline itself, applied here too.
    hydrateRichMedia(body);
    return;
  }
  body.innerHTML='<div class="v-loading" role="status">'+esc(I18N.t('viewer.loading'))+'</div>';
  fetchDescriptorWithRecovery(item.accessUrl).then(function(desc){
    if(gen!==viewerGen||viewerIndex!==idx)return;
    if(item.kind==='video'){
      body.innerHTML='<video class="v-media" src="'+esc(desc.url)+'" controls playsinline></video>';
    }else{
      body.innerHTML='<img class="v-media" src="'+esc(desc.url)+'" alt="'+esc(item.label||'')+'">';
    }
  }).catch(function(e){
    if(gen!==viewerGen||viewerIndex!==idx)return;
    var errKind=classifyMediaError(e);
    var key=errKind==='auth'?'viewer.unauthorized':errKind==='forbidden'?'media.error.forbidden'
      :errKind==='missing'?'viewer.missingMedia':errKind==='network'?'media.error.network':'viewer.error';
    body.innerHTML='<div class="v-error" role="alert">'+esc(I18N.t(key))+'</div>';
  });
}
