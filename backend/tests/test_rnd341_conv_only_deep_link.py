"""
Deterministic JS regression test for RND-341's console deep-link relaxation.

RND-341's external-contacts detail drawer needs to send users into the real
conversation console via its "相关会话" links, reusing the *existing*
conversation detail/message endpoints rather than building a new merged
timeline (see the ticket's "不新增跨会话合并消息时间线" constraint). The
console already has exactly this mechanism from RND-229 (search-result jump
via ?focus=&conv=&entityId=&entityType=), but it required a target `focus`
msgid to do anything at all -- a contact's related-conversation link just
wants the conversation opened, with no specific message to locate.

This test proves the minimal relaxation (readFocusFromUrl/focusMessage no
longer require `focus`/msgid, only `conv`) reuses the exact same
select-entity -> select-conversation chain as RND-229, and that omitting a
msgid never raises. It follows the repo's established pattern (see
test_rnd229_focus_locate.py / test_rnd240_focus_shortcircuit.py): extract the
real functions from the actual console source and drive them under Node with
a minimal DOM/URL shim, spying on setMode/onEntityClick/onConvClick.

Run (from backend/):
    pytest tests/test_rnd341_conv_only_deep_link.py -v
"""

from __future__ import annotations

import json
import shutil

import pytest

from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source
from tests.test_rnd229_focus_locate import _extract_fn

_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _bundle_real_fns() -> str:
    names = ["focusMessage", "focusCheckRow", "showFocusBanner", "readFocusFromUrl"]
    return "\n".join(_extract_fn(_REVIEW_CONSOLE_JS, n) for n in names)


_HARNESS = r'''
// ===== minimal DOM / environment stubs =====
var __banner = null;
var __convs = [];
var __entities = [];
var __setModeCalls = [];
var __onEntityClickCalls = [];
var __onConvClickCalls = [];

var mode = 'staff', selEntityId = null, convTypeFilter = 'all';
var timelineConvId = null, timelineMsgs = [];
var focusMsgId = null, focusPending = false;
var focusIsUrlArrival = false;

function setMode(m){ __setModeCalls.push(m); mode = m; }
function onEntityClick(el){ __onEntityClickCalls.push(el.dataset.id); selEntityId = el.dataset.id; }
function onConvClick(el){ __onConvClickCalls.push(el.dataset.id); }
function setConvTypeFilter(t){ convTypeFilter = t; }

function makeClassList(){
  var set = {};
  return {
    add: function(c){ set[c] = true; },
    remove: function(c){ delete set[c]; },
    contains: function(c){ return !!set[c]; },
    toString: function(){ return Object.keys(set).join(' '); }
  };
}
function makeEl(tag){
  var el = {
    tagName: tag, id: '', dataset: {}, style: {}, classList: makeClassList(),
    setAttribute: function(k, v){ if (k.indexOf('data-') === 0) this.dataset[k.substring(5)] = v; },
    appendChild: function(c){ if (this === document.body && c.id === 'focus-banner') __banner = c; },
    addEventListener: function(){},
    onclick: null, textContent: '', innerHTML: '',
    scrollIntoView: function(){ el.__scrolled = true; }
  };
  return el;
}
var document = {
  body: makeEl('body'),
  getElementById: function(id){
    if (id === 'focus-banner') return __banner;
    if (id === 'conv-body'){ var c = makeEl('div'); c.querySelectorAll = function(s){ return s === '.conv-card' ? __convs : []; }; return c; }
    if (id === 'entity-body'){ var e = makeEl('div'); e.querySelectorAll = function(s){ return s === '.entity-item' ? __entities : []; }; return e; }
    return null;
  },
  createElement: function(tag){ return makeEl(tag); },
  querySelector: function(){ return null; }
};
var I18N = { t: function(k){ return k; } };
var location = { search: __SEARCH__ };

// ===== real console functions (extracted from _REVIEW_CONSOLE_JS) =====
__REAL_FNS__

// ===== scenario driver =====
var __IN = __INITIAL__;
__convs = (__IN.convCardIds || []).map(function(id){
  var c = makeEl('div'); c.dataset.id = id; c.dataset.name = 'Conv ' + id; c.dataset.type = 'direct';
  return c;
});
__entities = (__IN.entityCardIds || []).map(function(id){
  var e = makeEl('div'); e.dataset.id = id; e.dataset.name = 'Entity ' + id;
  return e;
});

readFocusFromUrl();

setTimeout(function(){
  process.stdout.write(JSON.stringify({
    setModeCalls: __setModeCalls,
    onEntityClickCalls: __onEntityClickCalls,
    onConvClickCalls: __onConvClickCalls,
    banner: !!__banner
  }));
}, __IN.wait || 400);
'''


def _run(search: str, initial: dict) -> dict:
    assert NODE, "node executable not found"
    js = (
        _HARNESS.replace("__REAL_FNS__", _bundle_real_fns())
        .replace("__SEARCH__", json.dumps(search))
        .replace("__INITIAL__", json.dumps(initial))
    )
    result = run_node(js, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"node harness failed ({result.returncode}):\n{result.stderr}")
    return json.loads(result.stdout)


def test_conv_only_url_opens_the_conversation_without_a_msgid():
    """A related-conversation link from the external-contacts drawer carries
    conv/convType/entityId/entityType but no `focus` -- the console must
    still select the entity and open the conversation via the existing
    chain, exactly as a RND-229 search-result jump would."""
    r = _run(
        "?conv=conv-9&convType=direct&entityId=ext-9&entityType=contact",
        {
            "convCardIds": ["conv-9"],
            "entityCardIds": ["ext-9"],
            "wait": 1500,
        },
    )
    assert r["setModeCalls"] == ["contact"], (
        f"conv-only arrival must still switch to the right mode: {r}"
    )
    assert r["onEntityClickCalls"] == ["ext-9"], f"must select the target entity: {r}"
    assert r["onConvClickCalls"] == ["conv-9"], (
        f"must open the target conversation via the existing chain: {r}"
    )


def test_conv_only_url_never_shows_the_search_jump_banner():
    """No msgid means nothing to locate/highlight -- the "back to search
    results" banner (which only makes sense for an actual message locate)
    must never appear for a bare conversation-open arrival."""
    r = _run(
        "?conv=conv-9&convType=direct&entityId=ext-9&entityType=contact",
        {
            "convCardIds": ["conv-9"],
            "entityCardIds": ["ext-9"],
            "wait": 1500,
        },
    )
    assert r["banner"] is False, r


def test_url_with_neither_focus_nor_conv_does_nothing():
    """Regression guard: readFocusFromUrl must still no-op on a plain page
    load with no relevant query params at all."""
    r = _run("", {"wait": 100})
    assert r["setModeCalls"] == []
    assert r["onEntityClickCalls"] == []
    assert r["onConvClickCalls"] == []
