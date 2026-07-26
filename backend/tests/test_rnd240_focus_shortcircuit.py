"""
Deterministic JS regression test for RND-240's frontend fix: focusMessage()
must short-circuit the full entity-list/conversation-list reload chain when
the clicked search result belongs to the entity/conversation the console is
already showing.

Before this fix, every call to focusMessage() unconditionally ran
setMode(targetMode) -> reload the entity list -> poll for the entity card
(waitForEntity, 200ms/tick, ~8s cap) -> onEntityClick -> reload the
conversation list -> poll for the conversation card (waitForConv, 100ms/tick,
8s cap) -> onConvClick, even when the target conversation was already the
active selection on screen. That polling cap is what stretched a same-
conversation locate to ~8s in practice.

Follows the repo's established pattern (see test_rnd229_focus_locate.py):
extract the *real* focusMessage/focusCheckRow from the actual console source
and execute them under Node with a minimal DOM shim, spying on
setMode/onEntityClick/onConvClick to prove which path was taken -- rather
than re-implementing the decision in the test.

Scenarios:
  1. Target conversation is already selected AND its timeline is already
     loaded -> setMode/onConvClick must NOT run; focusCheckRow runs
     directly against the already-rendered row.
  2. Target entity/mode/filter already match, but the target is a
     *different* conversation card under that same entity -> setMode must
     NOT run (no entity-list reload), but onConvClick(hit) must run once
     to load that conversation's timeline.
  3. Target belongs to a different entity than the one currently selected
     -> short-circuit must NOT engage; the existing setMode -> entity-list
     reload -> waitForEntity/waitForConv chain must still run (proves the
     fix does not disable the general case RND-229 already covers).

Run (from backend/):
    pytest tests/test_rnd240_focus_shortcircuit.py -v
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
    names = ["focusMessage", "focusCheckRow", "showFocusBanner"]
    return "\n".join(_extract_fn(_REVIEW_CONSOLE_JS, n) for n in names)


_HARNESS = r'''
// ===== minimal DOM / environment stubs =====
var __banner = null;
var __convs = [];
var __entities = [];
var __setModeCalls = [];
var __onEntityClickCalls = [];
var __onConvClickCalls = [];

var mode = __INITIAL__.mode;
var selEntityId = __INITIAL__.selEntityId;
var convTypeFilter = __INITIAL__.convTypeFilter;
var timelineConvId = __INITIAL__.timelineConvId;
var timelineMsgs = __INITIAL__.timelineMsgs;
var focusMsgId = null, focusPending = false;
var focusIsUrlArrival = true;

// Spies standing in for the real setMode/onEntityClick/onConvClick --
// this test isolates focusMessage's ROUTING decision (does it call these
// at all), not their own internal behavior (already covered elsewhere:
// setMode by test_archive_console_v2.py, onEntityClick/onConvClick's
// timeline-loading by test_rnd229_focus_locate.py).
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
    addEventListener: function(cb_name, cb){ if (cb_name === 'animationend') el.__onAnimEnd = cb; },
    onclick: null, textContent: '', innerHTML: '',
    scrollIntoView: function(){ el.__scrolled = true; }
  };
  return el;
}
var __rows = {};
var document = {
  body: makeEl('body'),
  getElementById: function(id){
    if (id === 'focus-banner') return __banner;
    if (id === 'conv-body'){ var c = makeEl('div'); c.querySelectorAll = function(s){ return s === '.conv-card' ? __convs : []; }; return c; }
    if (id === 'entity-body'){ var e = makeEl('div'); e.querySelectorAll = function(s){ return s === '.entity-item' ? __entities : []; }; return e; }
    return null;
  },
  createElement: function(tag){ return makeEl(tag); },
  querySelector: function(sel){
    var m = sel.match(/^\[data-msgid="(.+)"\]$/);
    if (m) return __rows[m[1]] || null;
    return null;
  }
};
var I18N = { t: function(k){ return k; } };

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
if (__IN.preRenderedMsgId) {
  var row = makeEl('div');
  row.setAttribute('data-msgid', __IN.preRenderedMsgId);
  __rows[__IN.preRenderedMsgId] = row;
}

focusMessage(__IN.msgid, __IN.convId, 'direct', __IN.entityId, __IN.entityType);

setTimeout(function(){
  var row = __rows[__IN.msgid];
  process.stdout.write(JSON.stringify({
    setModeCalls: __setModeCalls,
    onEntityClickCalls: __onEntityClickCalls,
    onConvClickCalls: __onConvClickCalls,
    located: !!(row && row.classList.contains('target-flash')),
    scrolled: !!(row && row.__scrolled)
  }));
}, __IN.wait || 50);
'''


def _run(initial: dict) -> dict:
    assert NODE, "node executable not found"
    js = _HARNESS.replace("__REAL_FNS__", _bundle_real_fns()).replace(
        "__INITIAL__", json.dumps(initial)
    )
    result = run_node(js, timeout=30)
    if result.returncode != 0:
        raise AssertionError("node harness failed (%s):\n%s" % (result.returncode, result.stderr))
    return json.loads(result.stdout)


def test_shortcircuit_skips_everything_when_conversation_already_open():
    r = _run(
        {
            "mode": "staff",
            "selEntityId": "ent-1",
            "convTypeFilter": "all",
            "timelineConvId": "conv-1",
            "timelineMsgs": [{"msgid": "m5"}],
            "convCardIds": ["conv-1"],
            "msgid": "m5",
            "convId": "conv-1",
            "entityId": "ent-1",
            "entityType": "staff",
            "preRenderedMsgId": "m5",
            "wait": 50,
        }
    )
    assert r["setModeCalls"] == [], "same-conversation locate must not reload the entity list: %s" % r
    assert r["onConvClickCalls"] == [], "already-loaded timeline must not be re-fetched: %s" % r
    assert r["located"], "target row should be located directly via focusCheckRow: %s" % r
    assert r["scrolled"], "scrollIntoView should have been called: %s" % r


def test_shortcircuit_reuses_entity_but_switches_conversation():
    r = _run(
        {
            "mode": "staff",
            "selEntityId": "ent-1",
            "convTypeFilter": "all",
            "timelineConvId": "conv-OLD",
            "timelineMsgs": [{"msgid": "m1"}],
            "convCardIds": ["conv-2"],
            "msgid": "m9",
            "convId": "conv-2",
            "entityId": "ent-1",
            "entityType": "staff",
            "preRenderedMsgId": None,
            "wait": 50,
        }
    )
    assert r["setModeCalls"] == [], "same-entity locate must not reload the entity list: %s" % r
    assert r["onConvClickCalls"] == ["conv-2"], (
        "a different conversation under the SAME entity should load via onConvClick, "
        "not the full waitForEntity/waitForConv poll chain: %s" % r
    )


def test_no_shortcircuit_when_entity_differs_full_chain_still_runs():
    r = _run(
        {
            "mode": "staff",
            "selEntityId": "ent-OLD",
            "convTypeFilter": "all",
            "timelineConvId": None,
            "timelineMsgs": [],
            "convCardIds": ["conv-3"],
            "entityCardIds": ["ent-NEW"],
            "msgid": "m1",
            "convId": "conv-3",
            "entityId": "ent-NEW",
            "entityType": "staff",
            "preRenderedMsgId": None,
            "wait": 800,
        }
    )
    assert r["setModeCalls"] == ["staff"], (
        "a different target entity must still fall through to the full "
        "setMode/entity-list-reload chain (RND-229 general case): %s" % r
    )
    assert r["onEntityClickCalls"] == ["ent-NEW"], (
        "the pre-existing waitForEntity poll must still find and select the "
        "target entity when the short-circuit does not apply: %s" % r
    )
    assert r["onConvClickCalls"] == ["conv-3"], (
        "the pre-existing waitForConv poll must still find and select the "
        "target conversation when the short-circuit does not apply: %s" % r
    )
