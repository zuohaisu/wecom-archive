
var mode='staff',selEntityId=null,selConvId=null,selEntityName=null,selConvName=null;
var lastEntityItems=null,lastConvItems=null;
// RND-204: signatures of the exact entity/conversation lists last painted,
// used only to skip a full list re-render during a background auto-refresh
// when nothing changed -- this is what stops the every-cycle whole-list
// rebuild (and its jitter / active-selection churn) called out in the
// incremental-refresh redesign. The guard lives ONLY in the refresh path
// (refreshEntityList/refreshConversationList); the initial-load and
// locale-switch paths always render.
var lastEntitySig=null,lastConvSig=null;
var timelineConvId=null,timelineMsgs=[],timelineHasOlder=false,timelineNextBefore=null,timelineLoadingOlder=false;
// RND-158 Phase 2 (API-contract round): entity context captured at the
// moment loadTimeline() is called for the CURRENTLY selected conversation
// -- not read live from mode/selEntityId at fetch time, since the user can
// switch entities while a timeline request is in flight. Same
// capture-at-call-time principle timelineRequestGen already establishes.
var timelineConvType=null,timelineMode=null,timelineEntityId=null;
var timelineHistoryError=null,timelineTopObserver=null;
// RND-206 QA fix: a monotonically increasing generation token bumped every
// time the active conversation changes (loadTimeline). Every in-flight
// timeline request captures its own requestConvId+gen at send time and
// re-checks both before applying its response -- a response that arrives
// after the user has switched (or switched back to) a conversation is
// silently dropped rather than clobbering newer state. See fetchTimelinePage/
// refreshTimelineIfSelected/fetchOlderMessages.
var timelineRequestGen=0;
// Signature of the exact array renderTimeline() last painted, used only to
// detect a semantically-unchanged auto-refresh (RND-206 QA fix #6) so an
// unchanged refresh can skip the DOM rebuild entirely and preserve live
// <video>/<audio> playback state instead of tearing it down and rebuilding.
var lastRenderedTimelineSignature=null;
var refreshInFlight=false;
// A sync version change merits reloading list/timeline data. There is no
// periodic browser refresh; status polling runs only while a requested sync
// is in progress.
var syncStatus=null,syncInProgress=false,lastSeenSyncVersion=null;
var syncStatusNotice=null,syncStatusPollTimer=null,syncStatusRequestInFlight=false;
function esc(s){
  return s==null?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtTime(ms){
  // Display-only: renders Beijing time (UTC+8, no DST) from a UTC epoch-ms value.
  // Raw ms is never mutated; ordering/pagination always use the original value.
  // Timezone is communicated once via the page-level tz-note, not per value —
  // this returns a bare "YYYY-MM-DD HH:mm:ss" with no timezone suffix.
  if(!ms)return'';
  var d=new Date(ms+8*3600*1000);
  return d.getUTCFullYear()+'-'+pad(d.getUTCMonth()+1)+'-'+pad(d.getUTCDate())+' '+pad(d.getUTCHours())+':'+pad(d.getUTCMinutes())+':'+pad(d.getUTCSeconds());
}
function pad(n){return String(n).padStart(2,'0');}
function handleUnauth(r){
  if(r.status===401){window.location.href='/admin/login';return true;}
  return false;
}
var focusMsgId=null; // RND-229: set by readFocusFromUrl() during init, read by focusCheckRow()
// RND-229 AC5/AC6 fix: the initial focus attempt must run only after the
// first-screen timeline has actually rendered (a fixed 350ms wait raced
// slow first-screen responses and silently dropped the locate). Set true
// once the target conversation is selected; consumed by fetchTimelinePage
// on its initial render-completion.
var focusPending=false;
// Bug fix: showFocusBanner()'s "← 返回搜索结果" wires history.back() --
// correct ONLY when the current focusMessage() call arrived via a cross-
// page redirect from the standalone /admin/search results page
// (readFocusFromUrl(), where the previous history entry really is that
// search page). The in-page inline-search/locator-bar flows
// (onSearchHitClick/locatorPrev/locatorNext) call focusMessage() too, but
// never navigate anywhere -- for them, history.back() pops whatever page
// was open BEFORE this console tab was ever loaded (typically
// /admin/login), which is how "click a search hit, then click the banner"
// was sending users back to the login screen. Each focusMessage() caller
// sets this explicitly right before calling; focusCheckRow() only shows
// the banner when it's true. The locator bar itself (prev/next/收起) is
// the correct "return" UI for the in-page case, so no replacement banner
// is needed there.
var focusIsUrlArrival=false;

// RND-159: Search
var searchTimer=null,searchLastQ='';

// Archive Console v2 (design import): new UI state.
// auditMode: gates the per-row msgtype badge + audit line in timelineRowHtml.
//   Default false -- previously the msgtype badge was always shown for
//   non-text messages; this is an intentional visible behavior change.
var auditMode=false;
// panelOpen/panelTab: the new right-hand info/audit panel.
var panelOpen=true, panelTab='info';
// selectedMsgId: the timeline row currently selected for the audit tab.
var selectedMsgId=null;
// scopePopoverOpen: the "监控范围" popover replacing the old always-visible
// entity column; entity-header/entity-body/mode-tabs render inside it.
var scopePopoverOpen=false;
// convTypeFilter: client-side filter over the already-fetched conversation
// list ('all'|'group'|'direct') -- no new API call, ConversationOut already
// carries conversation_type.
var convTypeFilter='all';
// searchHits: ordered array of the last /api/search/messages result rows
// (msgid/conversation_id/conversation_type/entity_id/entity_type), used by
// the locator bar's prev/next stepping. locatorIndex is 1-based, 0 = none.
var searchHits=[],locatorIndex=0;
// conversation detail cache for the panel's 会话信息 tab, keyed by
// conversation_id, populated by loadConversationDetail() in api-client.js.
var convDetailCache={};
// Inline search filters, real params passed to /api/search/messages (see
// backend/app/routers/search.py) -- not decorative. searchDateRange is one
// of null|'1d'|'7d'|'30d'|'90d'; searchAllTypes toggles between the
// backend's default (text-only, msgtype omitted) and every registered
// msgtype (via RND216_MSGTYPE_OPTIONS' rawValues, reused from the
// standalone search page's own catalog).
var searchDateRange=null,searchAllTypes=false;
// RND-363: message-deletion selection state, scoped to the current tenant
// and current conversation. deleteSelection is a {msgid:true} map over the
// currently LOADED timeline rows only; it is cleared on every conversation/
// scope/mode switch so an old selection can never be applied to a new view.
// deleteStatus is the cached GET /api/admin/messages/deletion-status
// response ({can_delete, deletion_locked}); the delete surface is hidden
// unless the authenticated role may delete AND the tenant hold is off.
var deleteMode=false;
var deleteSelection={};
var deleteStatus=null;
