"""
RND-204 — background incremental refresh regression coverage.

Background: after RND-153 the review console polls the selected conversation
every 30s. The defect this suite guards against was that every poll rebuilt
the whole conversation area (timeline innerHTML + entity/conversation lists),
which re-requested already-displayed images, jittered the timeline, and
disturbed scroll position even when the backend data had not changed.

The redesign (RND-204) makes the poll a background *incremental* update:
  * no data change  -> the timeline DOM is not touched at all,
  * a new message    -> only the new row is appended (existing rows/media
                        nodes keep their identity and load state),
  * a media status
    change            -> only the affected message's row is rebuilt (its
                        media re-requested); unrelated images are untouched,
  * entity/conv lists -> re-rendered only when their content signature
                        actually changed.

These tests execute the real embedded JS under Node against a small but
faithful DOM shim (with a tiny HTML parser) so the incremental reconcile
path in applyTimelineRefresh actually runs — asserting on real node
identity/media-request behaviour, not source-string matching.

Run (from backend/):
    pytest tests/test_rnd_204_incremental_refresh.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.routers.web import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON
from tests._rnd216_web_shims import review_console_js_source

_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


def _rnd206_block() -> str:
    return _extract(
        r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
        "RND-206 rich-media/composite block",
    )


def _state_vars_block() -> str:
    return _extract(
        r"var mode=.*?\nvar lastRenderedTimelineSignature=null;",
        "timeline/viewer state vars",
    ) + (
        # Archive Console v2 (design import): declared even later in
        # console-state.js (outside this extractor's range), read
        # unconditionally by timelineRowHtml()/renderConvList().
        "\nvar auditMode = false;\nvar selectedMsgId = null;\nvar convTypeFilter = 'all';"
    )


def _bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('en');",
        _state_vars_block(),
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"function handleUnauth\(r\)\{.*?\n\}", "handleUnauth()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        # Archive Console v2 (design import): graded media_status placeholder.
        _extract(
            r"var MEDIA_STATUS_DOT=\{.*?\nfunction renderGradedMediaPlaceholder\(typeLabel,status\)\{.*?\n\}",
            "renderGradedMediaPlaceholder",
        ),
        f"var RND216_MTR_ENTRIES = {_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON};",
        _extract(r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"),
        _extract(r"function isSafeUrl\(u\)\{.*?\n\}", "isSafeUrl()"),
        _extract(r"function hostnameOf\(u\)\{.*?\n\}", "hostnameOf()"),
        _extract(r"function fmtCoord\(n\)\{.*?\}", "fmtCoord()"),
        _extract(r"function renderStructuredFallback\(m\)\{.*?\n\}", "renderStructuredFallback()"),
        _extract(r"function renderLinkCard\(m\)\{.*?\n\}", "renderLinkCard()"),
        _extract(r"function renderLocationCard\(m\)\{.*?\n\}", "renderLocationCard()"),
        _extract(r"function renderSanitizedMarkdown\(raw\)\{.*?\n\}", "renderSanitizedMarkdown()"),
        _extract(r"function renderMarkdownCard\(m\)\{.*?\n\}", "renderMarkdownCard()"),
        _extract(r"function renderNewsCard\(m\)\{.*?\n\}", "renderNewsCard()"),
        _extract(r"function renderMiniprogramCard\(m\)\{.*?\n\}", "renderMiniprogramCard()"),
        _extract(r"function renderVoteCard\(m\)\{.*?\n\}", "renderVoteCard()"),
        _extract(r"function renderTodoCard\(m\)\{.*?\n\}", "renderTodoCard()"),
        _extract(r"function renderCollectCard\(m\)\{.*?\n\}", "renderCollectCard()"),
        _extract(r"function renderMeetingCard\(m\)\{.*?\n\}", "renderMeetingCard()"),
        _extract(r"function renderScheduleCard\(m\)\{.*?\n\}", "renderScheduleCard()"),
        _extract(r"function renderRedpacketCard\(m\)\{.*?\n\}", "renderRedpacketCard()"),
        _extract(r"function renderSwitchCorpCard\(m\)\{.*?\n\}", "renderSwitchCorpCard()"),
        _extract(r"function renderSystemCard\(m\)\{.*?\n\}", "renderSystemCard()"),
        # RND-210 — business-card renderer referenced by STRUCTURED_CARD_RENDERERS
        _extract(r"function renderCardMessage\(m\)\{.*?\n\}", "renderCardMessage()"),
        # RND-210: STRUCTURED_CARD_RENDERERS now also references these two
        # audio renderers — stub them (these tests don't exercise audio).
        "function renderAudioArchiveMessage(m){return '';} function renderAudioDocMessage(m){return '';}",
        _extract(r"var STRUCTURED_CARD_RENDERERS=\{.*?\n\};", "STRUCTURED_CARD_RENDERERS"),
        _extract(r"function renderStructuredCard\(m\)\{.*?\n\}", "renderStructuredCard()"),
        _rnd206_block(),
        _extract(r"function renderRevokePlaceholder\(m\)\{.*?\n\}", "renderRevokePlaceholder()"),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        _extract(r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"),
        _extract(r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"),
        _extract(r"function timelineRowHtml\(m\)\{.*?\n\}", "timelineRowHtml()"),
        _extract(r"function renderTimeline\(scrollToBottom\)\{.*?\n\}", "renderTimeline()"),
        _extract(r"function isNearTop\(\)\{.*?\n\}", "isNearTop()"),
        _extract(r"function historyStatusEl\(\)\{.*?\}", "historyStatusEl()"),
        _extract(r"function showEndOfHistory\(\)\{.*?\n\}", "showEndOfHistory()"),
        _extract(r"function historyRetryHtml\(\)\{.*?\n\}", "historyRetryHtml()"),
        _extract(r"function startHistoryObserver\(\)\{.*?\n\}", "startHistoryObserver()"),
        _extract(r"function stopHistoryObserver\(\)\{.*?\n\}", "stopHistoryObserver()"),
        _extract(r"function isNearBottom\(\)\{.*?\n\}", "isNearBottom()"),
        _extract(r"function showNewMessageIndicator\(\)\{.*?\n\}", "showNewMessageIndicator()"),
        _extract(r"function hideNewMessageIndicator\(\)\{.*?\n\}", "hideNewMessageIndicator()"),
        _extract(r"function mergeMessagesByMsgid\(existing,incoming\)\{.*?\n\}", "mergeMessagesByMsgid()"),
        _extract(r"function timelineEntityQueryParams\(\)\{.*?\n\}", "timelineEntityQueryParams()"),
        _extract(r"function buildTimelineRowNode\(m\)\{.*?\n\}", "buildTimelineRowNode()"),
        _extract(r"function syncHistoryStatus\(\)\{.*?\n\}", "syncHistoryStatus()"),
        _extract(r"function applyRefreshScroll\(prevScrollTop,wasNearBottom,hasNew\)\{.*?\n\}", "applyRefreshScroll()"),
        _extract(r"function applyTimelineRefresh\(prevScrollTop,wasNearBottom,hasNew\)\{.*?\n\}", "applyTimelineRefresh()"),
        _extract(r"function refreshTimelineIfSelected\(\)\{.*?\n\}", "refreshTimelineIfSelected()"),
        _extract(r"function entityListSignature\(items\)\{.*?\n\}", "entityListSignature()"),
        _extract(r"function convListSignature\(convs\)\{.*?\n\}", "convListSignature()"),
        _extract(r"function renderEntityList\(items\)\{.*?\n\}", "renderEntityList()"),
        # Archive Console v2 (design import): renderConvList() now filters
        # through applyConvTypeFilter() (client-side 全部/群聊/单聊 tabs).
        _extract(r"function applyConvTypeFilter\(convs\)\{.*?\n\}", "applyConvTypeFilter()"),
        _extract(r"function renderConvList\(convs\)\{.*?\n\}", "renderConvList()"),
        _extract(r"function refreshEntityList\(\)\{.*?\n\}", "refreshEntityList()"),
        _extract(r"function refreshConversationList\(\)\{.*?\n\}", "refreshConversationList()"),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Minimal DOM shim + tiny HTML parser (enough for the incremental reconcile
# path: className/attribute selectors, node identity, insert/replace/remove,
# and innerHTML that actually parses into a child tree).
# ---------------------------------------------------------------------------
_DOM_JS = r"""
function decodeEntities(s){
  return String(s).replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
}
function El(tag){
  this.tagName=String(tag||'div').toUpperCase();
  this.children=[];
  this.attributes={};
  this.className='';
  this.style={};
  this.nodeType=1;
  this.textContent='';
  this.parentNode=null;
  this._html='';
}
Object.defineProperty(El.prototype,'firstChild',{get:function(){return this.children.length?this.children[0]:null;}});
Object.defineProperty(El.prototype,'nextSibling',{get:function(){
  if(!this.parentNode)return null;
  var ch=this.parentNode.children;var i=ch.indexOf(this);
  return (i>=0&&i+1<ch.length)?ch[i+1]:null;
}});
Object.defineProperty(El.prototype,'innerHTML',{
  get:function(){return this._html;},
  set:function(v){
    this._html=String(v);
    this.children=[];
    var kids=parseHTML(String(v));
    for(var i=0;i<kids.length;i++){kids[i].parentNode=this;this.children.push(kids[i]);}
  }
});
El.prototype.setAttribute=function(k,v){this.attributes[k]=String(v);if(k==='class')this.className=String(v);};
El.prototype.getAttribute=function(k){return Object.prototype.hasOwnProperty.call(this.attributes,k)?this.attributes[k]:null;};
El.prototype.hasAttribute=function(k){return Object.prototype.hasOwnProperty.call(this.attributes,k);};
El.prototype.appendChild=function(c){c.parentNode=this;this.children.push(c);return c;};
El.prototype.insertBefore=function(c,ref){
  c.parentNode=this;
  if(!ref){this.children.push(c);return c;}
  var i=this.children.indexOf(ref);
  if(i<0){this.children.push(c);}else{this.children.splice(i,0,c);}
  return c;
};
El.prototype.replaceChild=function(nw,old){
  var i=this.children.indexOf(old);
  if(i>=0){this.children[i]=nw;nw.parentNode=this;old.parentNode=null;}
  return old;
};
El.prototype.removeChild=function(c){
  var i=this.children.indexOf(c);
  if(i>=0){this.children.splice(i,1);c.parentNode=null;}
  return c;
};
function _matches(el,sel){
  if(el.nodeType!==1)return false;
  if(sel.charAt(0)==='.'){return (' '+el.className+' ').indexOf(' '+sel.slice(1)+' ')>=0;}
  if(sel.charAt(0)==='['){return el.hasAttribute(sel.slice(1,sel.length-1));}
  return el.tagName===sel.toUpperCase();
}
function _collect(el,sel,out){
  for(var i=0;i<el.children.length;i++){
    var c=el.children[i];
    if(_matches(c,sel))out.push(c);
    _collect(c,sel,out);
  }
}
El.prototype.querySelectorAll=function(sel){var out=[];_collect(this,sel,out);return out;};
El.prototype.querySelector=function(sel){var out=[];_collect(this,sel,out);return out.length?out[0]:null;};
var _VOID={img:1,br:1,hr:1,input:1,meta:1,link:1};
function parseHTML(html){
  var pos=0;
  function parseNodes(){
    var nodes=[];
    while(pos<html.length){
      if(html.charAt(pos)==='<'){
        if(html.charAt(pos+1)==='/'){break;}
        nodes.push(parseElement());
      }else{
        var next=html.indexOf('<',pos);if(next<0)next=html.length;
        var text=html.slice(pos,next);pos=next;
        if(text.length){var tn=new El('#text');tn.nodeType=3;tn.textContent=decodeEntities(text);nodes.push(tn);}
      }
    }
    return nodes;
  }
  function parseElement(){
    pos++; // skip '<'
    var tm=/^[a-zA-Z0-9]+/.exec(html.slice(pos));
    var tag=tm[0];pos+=tag.length;
    var el=new El(tag);
    var selfClosed=false;
    while(pos<html.length){
      var ch=html.charAt(pos);
      if(ch==='>'){pos++;break;}
      if(ch==='/'&&html.charAt(pos+1)==='>'){pos+=2;selfClosed=true;break;}
      if(/\s/.test(ch)){pos++;continue;}
      var am=/^([a-zA-Z0-9_:\-]+)(\s*=\s*"([^"]*)")?/.exec(html.slice(pos));
      if(!am){pos++;continue;}
      var name=am[1];var val=am[3]!==undefined?decodeEntities(am[3]):'';
      el.setAttribute(name,val);
      pos+=am[0].length;
    }
    if(!selfClosed&&!_VOID[tag.toLowerCase()]){
      var kids=parseNodes();
      for(var i=0;i<kids.length;i++)el.appendChild(kids[i]);
      var close=html.indexOf('>',pos);pos=(close<0?html.length:close+1);
    }
    return el;
  }
  return parseNodes();
}
var _byId={};
function _searchId(el,id){
  for(var i=0;i<el.children.length;i++){
    var c=el.children[i];
    if(c.getAttribute&&c.getAttribute('id')===id)return c;
    var r=_searchId(c,id);if(r)return r;
  }
  return null;
}
var document={
  getElementById:function(id){
    if(_byId[id])return _byId[id];
    for(var k in _byId){var r=_searchId(_byId[k],id);if(r)return r;}
    return null;
  },
  createElement:function(tag){return new El(tag);}
};
window={location:{href:''}};
function flush(){return new Promise(function(res){setImmediate(res);});}
"""


def _run(script_body: str) -> dict:
    assert NODE, "node executable not found"
    harness = f"""
{_bundle()}
{_DOM_JS}

// Media hydration is exercised elsewhere (test_rnd_206_qa_fixes.py). Here we
// only need to *count* which access URLs get (re-)requested by a refresh, so
// stub loadRichMedia to record instead of performing a real descriptor fetch.
var mediaLoadCalls=[];
loadRichMedia=function(el){{ mediaLoadCalls.push(el.getAttribute('data-rnd206-access-url')); }};

(async function() {{
{script_body}
}})().catch(function(e) {{
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
}});
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def _text_msg(msgid: str, msgtime: int, text: str) -> dict:
    return {
        "msgid": msgid,
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": ["contact_zhangsan"],
        "recipient_display_names": ["Zhang San"],
        "msgtime": msgtime,
        "msgtype": "text",
        "content_text": text,
        "roomid": None,
        "media_type": "text",
        "media_status": None,
        "media_access_url": None,
        "normalized_type": "text",
        "renderer_strategy": "text",
        "is_revoked": False,
        "revoked_at": None,
        "revoke_association_status": None,
        "structured_content": None,
    }


def _image_msg(msgid: str, msgtime: int, *, status: str, access_url) -> dict:
    return {
        "msgid": msgid,
        "sender": "staff_alice",
        "sender_display_name": "Alice",
        "sender_raw_id": "staff_alice",
        "recipients": ["contact_zhangsan"],
        "recipient_display_names": ["Zhang San"],
        "msgtime": msgtime,
        "msgtype": "image",
        "content_text": None,
        "roomid": None,
        "media_type": "image",
        "media_status": status,
        "media_access_url": access_url,
        "normalized_type": "image",
        "renderer_strategy": "media_preview",
        "is_revoked": False,
        "revoked_at": None,
        "revoke_association_status": None,
        "structured_content": None,
    }


# A fetch stub that returns a fixed message set (verbatim) for the refresh.
def _fetch_returning(messages: list[dict]) -> str:
    return (
        "fetch=function(url){ return Promise.resolve({ok:true,status:200,"
        "json:function(){return Promise.resolve({messages:" + json.dumps(messages)
        + ",pagination:{has_older:false,next_before:null}});}}); };"
    )


_TIMELINE_ROOT = """
mode='staff';
selEntityId=null;
selConvId=null;
timelineConvId='conv-1';
timelineRequestGen=1;
timelineLoadingOlder=false;
timelineHasOlder=false;
timelineNextBefore=null;
timelineHistoryError=null;
timelineTopObserver=null;
var timelineBody=new El('div');
timelineBody.setAttribute('id','timeline-body');
timelineBody.scrollHeight=100; timelineBody.scrollTop=0; timelineBody.clientHeight=100;
var indicator=new El('button'); indicator.style={display:'none'};
_byId['timeline-body']=timelineBody;
_byId['new-msg-indicator']=indicator;
"""


def _tl_rows_js() -> str:
    # Helper JS: return the current timeline row nodes (in order).
    return (
        "function tlRows(){var tl=timelineBody.querySelector('.timeline');"
        "return tl?tl.querySelectorAll('[data-msgid]'):[];}"
    )


# ---------------------------------------------------------------------------
# 1. No data change -> the timeline DOM is not touched at all.
# ---------------------------------------------------------------------------


def test_unchanged_refresh_leaves_timeline_dom_untouched() -> None:
    msg = _text_msg("m1", 100, "hello")
    out = _run(
        _TIMELINE_ROOT
        + _tl_rows_js()
        + f"""
timelineMsgs=[{json.dumps(msg)}];
renderTimeline(false);
var tlBefore=timelineBody.querySelector('.timeline');
tlBefore.__tag='TL0';
var rowsBefore=tlRows();
rowsBefore[0].__mark='m1';
mediaLoadCalls=[];
{_fetch_returning([msg])}
await refreshTimelineIfSelected();
var tlAfter=timelineBody.querySelector('.timeline');
var rowsAfter=tlRows();
process.stdout.write(JSON.stringify({{
  sameTimelineNode: tlAfter.__tag==='TL0',
  sameRowNode: rowsAfter.length===1 && rowsAfter[0].__mark==='m1',
  mediaLoads: mediaLoadCalls.length
}}));
"""
    )
    assert out["sameTimelineNode"] is True  # timeline container never rebuilt
    assert out["sameRowNode"] is True  # the existing row keeps its identity
    assert out["mediaLoads"] == 0  # nothing re-requested


# ---------------------------------------------------------------------------
# 2. New message -> only the new row is appended; existing rows untouched.
# ---------------------------------------------------------------------------


def test_new_message_appended_without_rebuilding_existing_rows() -> None:
    m1 = _text_msg("m1", 100, "hello")
    m2 = _text_msg("m2", 200, "world")
    out = _run(
        _TIMELINE_ROOT
        + _tl_rows_js()
        + f"""
timelineMsgs=[{json.dumps(m1)}];
renderTimeline(false);
var tlBefore=timelineBody.querySelector('.timeline');
tlBefore.__tag='TL0';
tlRows()[0].__mark='m1';
mediaLoadCalls=[];
{_fetch_returning([m1, m2])}
await refreshTimelineIfSelected();
var tlAfter=timelineBody.querySelector('.timeline');
var rows=tlRows();
var ids=[]; for(var i=0;i<rows.length;i++) ids.push(rows[i].getAttribute('data-msgid'));
process.stdout.write(JSON.stringify({{
  sameTimelineNode: tlAfter.__tag==='TL0',
  ids: ids,
  existingRowPreserved: rows[0].__mark==='m1',
  newRowIsFresh: rows[1].__mark===undefined,
  mediaLoads: mediaLoadCalls.length
}}));
"""
    )
    assert out["sameTimelineNode"] is True  # incremental, not a full rebuild
    assert out["ids"] == ["m1", "m2"]  # new message appended in order
    assert out["existingRowPreserved"] is True  # old row node identity kept
    assert out["newRowIsFresh"] is True  # only the new row was created
    assert out["mediaLoads"] == 0  # no media involved -> nothing requested


def test_new_message_while_viewing_history_shows_indicator_without_forced_scroll() -> None:
    m1 = _text_msg("m1", 100, "hello")
    m2 = _text_msg("m2", 200, "world")
    out = _run(
        _TIMELINE_ROOT
        + _tl_rows_js()
        + f"""
timelineMsgs=[{json.dumps(m1)}];
renderTimeline(false);
// Simulate the user scrolled up reading history: NOT near the bottom.
timelineBody.scrollHeight=1000; timelineBody.scrollTop=50; timelineBody.clientHeight=100;
mediaLoadCalls=[];
{_fetch_returning([m1, m2])}
await refreshTimelineIfSelected();
process.stdout.write(JSON.stringify({{
  scrollTop: timelineBody.scrollTop,
  indicatorDisplay: indicator.style.display
}}));
"""
    )
    assert out["scrollTop"] == 50  # not force-scrolled to bottom
    assert out["indicatorDisplay"] == "block"  # "new messages" pill surfaced


# ---------------------------------------------------------------------------
# 3. Media status change (pending -> available) rebuilds only that message's
#    row and requests only its media; unrelated images are not reloaded.
# ---------------------------------------------------------------------------


def test_media_status_change_updates_only_that_row() -> None:
    img_pending = _image_msg("img1", 100, status="not_downloaded", access_url=None)
    img_available = _image_msg("img1", 100, status="available", access_url="/api/x/img1/access")
    # A second, already-available image that must NOT be re-requested.
    other = _image_msg("img2", 90, status="available", access_url="/api/x/img2/access")
    out = _run(
        _TIMELINE_ROOT
        + _tl_rows_js()
        + f"""
timelineMsgs=[{json.dumps(other)}, {json.dumps(img_pending)}];
renderTimeline(false);
var tlBefore=timelineBody.querySelector('.timeline');
tlBefore.__tag='TL0';
var rb=tlRows();
rb[0].__mark='img2'; rb[1].__mark='img1';
// Reset AFTER the initial full render so we only see refresh-driven loads.
mediaLoadCalls=[];
{_fetch_returning([other, img_available])}
await refreshTimelineIfSelected();
var tlAfter=timelineBody.querySelector('.timeline');
var rows=tlRows();
var byId={{}}; for(var i=0;i<rows.length;i++) byId[rows[i].getAttribute('data-msgid')]=rows[i];
process.stdout.write(JSON.stringify({{
  sameTimelineNode: tlAfter.__tag==='TL0',
  changedRowRebuilt: byId['img1'].__mark===undefined,
  otherRowPreserved: byId['img2'].__mark==='img2',
  mediaLoads: mediaLoadCalls
}}));
"""
    )
    assert out["sameTimelineNode"] is True  # still incremental
    assert out["changedRowRebuilt"] is True  # img1's row was rebuilt
    assert out["otherRowPreserved"] is True  # img2's row/img node untouched
    # Only the newly-available image is (re-)requested; the other is not.
    assert out["mediaLoads"] == ["/api/x/img1/access"]


# ---------------------------------------------------------------------------
# 4. Entity/conversation list guards — background refresh re-renders a list
#    only when its content signature actually changed.
# ---------------------------------------------------------------------------


def test_conversation_list_refresh_skips_render_when_unchanged() -> None:
    conv = {
        "conversation_id": "c1",
        "conversation_type": "direct",
        "display_name": "Zhang San",
        "raw_id": "contact_zhangsan",
        "last_message_text": "hi",
        "last_message_time": 1751702400000,
        "message_count": 3,
        "monitored_account_ids": ["staff_alice"],
        "monitored_account_display_names": ["Alice"],
    }
    conv_changed = dict(conv, last_message_text="new tail", message_count=4)
    out = _run(
        f"""
mode='staff'; selEntityId='staff_alice'; selConvId=null;
_byId['conv-body']=new El('div');
renderConvList([{json.dumps(conv)}]);      // baseline paint -> sets lastConvSig
var count=0; var orig=renderConvList;
renderConvList=function(c){{ count++; return orig(c); }};
function listFetch(arr){{ return function(url){{ return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve(arr);}}}}); }}; }}
fetch=listFetch([{json.dumps(conv)}]);         await refreshConversationList(); var afterUnchanged=count;
fetch=listFetch([{json.dumps(conv_changed)}]); await refreshConversationList(); var afterChanged=count;
process.stdout.write(JSON.stringify({{afterUnchanged: afterUnchanged, afterChanged: afterChanged}}));
"""
    )
    assert out["afterUnchanged"] == 0  # unchanged list -> no re-render
    assert out["afterChanged"] == 1  # changed list -> exactly one re-render


def test_entity_list_refresh_skips_render_when_unchanged() -> None:
    item = {
        "staff_id": "staff_alice",
        "raw_id": "staff_alice",
        "display_name": "Alice",
        "seat_status": "active",
    }
    item_changed = dict(item, display_name="Alice (Sales)")
    out = _run(
        f"""
mode='staff'; selEntityId=null;
_byId['entity-body']=new El('div');
renderEntityList([{json.dumps(item)}]);     // baseline paint -> sets lastEntitySig
var count=0; var orig=renderEntityList;
renderEntityList=function(x){{ count++; return orig(x); }};
function listFetch(arr){{ return function(url){{ return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve(arr);}}}}); }}; }}
fetch=listFetch([{json.dumps(item)}]);         await refreshEntityList(); var afterUnchanged=count;
fetch=listFetch([{json.dumps(item_changed)}]); await refreshEntityList(); var afterChanged=count;
process.stdout.write(JSON.stringify({{afterUnchanged: afterUnchanged, afterChanged: afterChanged}}));
"""
    )
    assert out["afterUnchanged"] == 0  # unchanged entities -> no re-render
    assert out["afterChanged"] == 1  # changed entities -> exactly one re-render


# ---------------------------------------------------------------------------
# 5. Stale-response guard on the list refreshers. refreshInFlight only blocks
#    overlapping refresh *cycles*; it does NOT stop a slow response from the
#    current cycle applying after the user has switched entity/mode. These
#    reproduce the acceptance-report blocker: a slow /api/conversations (or
#    entity-list) response must not repaint the list for a selection the user
#    has already navigated away from.
#
#    The scenario is driven deterministically: refreshX() is invoked (which
#    captures the selection at call time), then the selection is mutated
#    synchronously BEFORE the response's .then microtask runs, so the guard
#    sees the mismatch exactly as it would when a real network response lands
#    after a click. A returned-changed payload guarantees that, absent the
#    guard, a render WOULD have happened -- so a 0 render count proves the
#    guard (not the content signature) is what suppressed it.
# ---------------------------------------------------------------------------


def test_conversation_list_slow_response_does_not_overwrite_switched_entity() -> None:
    conv_a = {
        "conversation_id": "c-a",
        "conversation_type": "direct",
        "display_name": "Alice's chat",
        "raw_id": "contact_a",
        "last_message_text": "old",
        "last_message_time": 100,
        "message_count": 1,
        "monitored_account_ids": ["staff-a"],
        "monitored_account_display_names": ["Alice"],
    }
    # A genuinely changed payload for staff-a: without the stale guard this
    # differs from lastConvSig and WOULD trigger renderConvList.
    conv_a_changed = dict(conv_a, last_message_text="new tail", message_count=2)
    out = _run(
        f"""
mode='staff'; selEntityId='staff-a'; selConvId=null;
_byId['conv-body']=new El('div');
renderConvList([{json.dumps(conv_a)}]);        // baseline paint for staff-a -> lastConvSig
var count=0; var orig=renderConvList;
renderConvList=function(c){{ count++; return orig(c); }};
// A slow response carrying staff-a's (now-stale) conversations.
fetch=function(url){{ return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve([{json.dumps(conv_a_changed)}]);}}}}); }};
var p=refreshConversationList();               // captures reqEntityId='staff-a'
selEntityId='staff-b';                          // user switches BEFORE response lands
await p;
var afterSwitch=count;
// Control: with the same selection still active, a changed payload DOES render.
selEntityId='staff-a';
await refreshConversationList();
process.stdout.write(JSON.stringify({{afterSwitch: afterSwitch, afterSameSelection: count}}));
"""
    )
    assert out["afterSwitch"] == 0  # stale response suppressed -> list not overwritten
    assert out["afterSameSelection"] == 1  # same selection -> change still applied


def test_entity_list_slow_response_does_not_overwrite_switched_mode() -> None:
    staff_item = {
        "staff_id": "staff-a",
        "raw_id": "staff-a",
        "display_name": "Alice",
        "seat_status": "active",
    }
    staff_item_changed = dict(staff_item, display_name="Alice (Sales)")
    out = _run(
        f"""
mode='staff'; selEntityId=null;
_byId['entity-body']=new El('div');
renderEntityList([{json.dumps(staff_item)}]);   // baseline paint in staff mode -> lastEntitySig
var count=0; var orig=renderEntityList;
renderEntityList=function(x){{ count++; return orig(x); }};
// A slow response carrying staff (now-stale-for-contact-mode) accounts.
fetch=function(url){{ return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve([{json.dumps(staff_item_changed)}]);}}}}); }};
var p=refreshEntityList();                       // captures reqMode='staff'
mode='contact';                                  // user flips mode BEFORE response lands
await p;
var afterSwitch=count;
// Control: with mode restored, the changed payload DOES render.
mode='staff';
await refreshEntityList();
process.stdout.write(JSON.stringify({{afterSwitch: afterSwitch, afterSameMode: count}}));
"""
    )
    assert out["afterSwitch"] == 0  # stale-mode response suppressed
    assert out["afterSameMode"] == 1  # same mode -> change still applied


# ---------------------------------------------------------------------------
# 6. timelineSignature must cover every field timelineRowHtml renders, so a
#    change confined to a rendered-but-previously-unsigned field (a backfilled
#    sender display name, a corrected msgtype, updated recipients) is detected
#    as a change and the row is refreshed instead of being frozen as "stale".
# ---------------------------------------------------------------------------


def test_timeline_signature_detects_rendered_field_changes() -> None:
    base = _text_msg("m1", 100, "hello")
    base["sender"] = "staff-a"
    base["sender_display_name"] = "Alice"
    base["msgtype"] = "text"
    base["recipient_display_names"] = ["Bob"]
    variants = {
        "sender_display_name": dict(base, sender_display_name="Alice (Sales)"),
        "sender_raw_id": dict(base, sender_raw_id="staff_alice_2"),
        "msgtype": dict(base, msgtype="image"),
        "recipient_display_names": dict(base, recipient_display_names=["Bob", "Carol"]),
        "roomid": dict(base, roomid="room-1"),
    }
    js = "var base=" + json.dumps(base) + ";\nvar results={};\n"
    for name, variant in variants.items():
        js += (
            f"results[{json.dumps(name)}]="
            f"(timelineSignature([base])!==timelineSignature([{json.dumps(variant)}]));\n"
        )
    # A no-op change to a field the row never renders must NOT flip the sig.
    js += (
        "results['ignored_unrendered']="
        "(timelineSignature([base])===timelineSignature(["
        + json.dumps(dict(base, some_internal_only_field="x"))
        + "]));\n"
    )
    js += "process.stdout.write(JSON.stringify(results));"
    out = _run(js)
    assert out["sender_display_name"] is True
    assert out["sender_raw_id"] is True
    assert out["msgtype"] is True
    assert out["recipient_display_names"] is True
    assert out["roomid"] is True
    assert out["ignored_unrendered"] is True
