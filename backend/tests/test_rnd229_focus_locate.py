"""
Deterministic JS regression test for RND-229 AC5/AC6 (auto-locate target
message + visual feedback) under a *delayed* first-screen timeline response.

This is the fix for the Codex Acceptance Report FAIL: previously the first
`focusCheckRow()` ran on a fixed 350ms timer after conversation selection.
When the first-screen timeline request was slow (~900ms), the row did not yet
exist and `timelineHasOlder` was still false, so `focusCheckRow` exited early
and the locate was silently dropped. The fix binds the first locate to the
first-screen render completion (via a `focusPending` flag consumed inside
`fetchTimelinePage`'s `isInitial` success path).

We follow the repo's established pattern (see test_rnd_198_frontend.py):
extract the *real* embedded console functions from `_REVIEW_CONSOLE_JS`
and execute them under Node with a minimal DOM / fetch shim, so the test
exercises live behavior rather than a re-implementation.

Replayed scenarios (per the acceptance report):
  1. normal first-screen (fast)      -> target in first screen -> located
  2. slow first-screen (~900ms)      -> target in first screen -> located
                                        AND locatedAt > firstScreenResolvedAt
                                        (proves locate waits for render, not a
                                         350ms timer)
  3. target in history pagination    -> first screen has_older=true, target in
                                        an older page -> fetched + located

Run (from backend/):
    pytest tests/test_rnd229_focus_locate.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._rnd216_web_shims import review_console_js_source

_REVIEW_CONSOLE_JS = review_console_js_source()

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


# ---------------------------------------------------------------------------
# Extraction helpers — brace-balanced (handles nested blocks) so we pull the
# real function bodies verbatim, including their inner scopes.
# ---------------------------------------------------------------------------

def _extract_fn(src: str, name: str) -> str:
    marker = "function " + name + "("
    idx = src.find(marker)
    if idx < 0:
        raise AssertionError("function %s not found in _REVIEW_CONSOLE_JS" % name)
    brace = src.find("{", idx)
    if brace < 0:
        raise AssertionError("opening brace not found for %s" % name)
    depth = 0
    i = brace
    n = len(src)
    in_s = in_d = in_t = False
    while i < n:
        c = src[i]
        if in_s:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_s = False
        elif in_d:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_d = False
        elif in_t:
            if c == "`":
                in_t = False
        elif c == "'":
            in_s = True
        elif c == '"':
            in_d = True
        elif c == "`":
            in_t = True
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
        i += 1
    raise AssertionError("unbalanced braces in %s" % name)


def _bundle_real_fns() -> str:
    names = [
        "focusMessage",
        "focusCheckRow",
        "showFocusBanner",
        "fetchTimelinePage",
        "fetchOlderMessages",
        "loadTimeline",
    ]
    return "\n".join(_extract_fn(_REVIEW_CONSOLE_JS, n) for n in names)


# ---------------------------------------------------------------------------
# Node harness (stubs + extracted real functions + scenario driver)
# ---------------------------------------------------------------------------

_HARNESS = r'''
// ===== minimal DOM / environment stubs =====
var __rows = {};
var __banner = null;
var __entities = [];
var __convs = [];
var __firstScreenResolvedAt = 0;
var __locatedAt = 0;
var __scrollCalled = false;

var mode = 'staff', selEntityId = null, selConvId = null, selConvName = null;
var focusMsgId = null, focusPending = false;
var timelineConvId = null, timelineMsgs = [], timelineHasOlder = false,
    timelineNextBefore = null, timelineRequestGen = 0, timelineLoadingOlder = false,
    timelineHistoryError = null, timelineConvType = null, timelineMode = null,
    timelineEntityId = null;

function setMode(m){ mode = m; }
// Archive Console v2 (design import): focusMessage() now resets the
// 全部/群聊/单聊 filter to 'all' before locating (a non-'all' filter can
// hide the target conversation's card entirely) -- declare the real
// setConvTypeFilter()'s minimal contract here since this harness stubs
// setMode/onEntityClick/onConvClick rather than extracting them for real.
var convTypeFilter = 'all';
function setConvTypeFilter(t){ convTypeFilter = t; }
function onEntityClick(el){ selEntityId = el.dataset.id; }
function onConvClick(el){ selConvId = el.dataset.id; selConvName = el.dataset.name; loadTimeline(selConvId, el.dataset.type); }
function esc(s){ return s == null ? '' : String(s); }
function handleUnauth(r){ return false; }
var I18N = { setLocale: function(){}, t: function(k){ return k; } };
function startHistoryObserver(){}
function stopHistoryObserver(){}
function hideNewMessageIndicator(){}
function timelineEntityQueryParams(){ return ''; }

function makeClassList(){
  var set = {};
  return {
    add: function(c){ set[c] = true; },
    remove: function(c){ delete set[c]; },
    contains: function(c){ return !!set[c]; },
    toggle: function(c, v){ if (v === undefined || v) set[c] = true; else delete set[c]; },
    toString: function(){ return Object.keys(set).join(' '); }
  };
}
function makeEl(tag){
  var el = {
    tagName: tag, id: '', dataset: {}, style: {}, _attrs: {}, classList: makeClassList(),
    _children: [],
    setAttribute: function(k, v){ this._attrs[k] = v; if (k.indexOf('data-') === 0) this.dataset[k.substring(5)] = v; },
    getAttribute: function(k){ return this._attrs[k]; },
    appendChild: function(c){ this._children.push(c); if (this === document.body && c.id === 'focus-banner') __banner = c; },
    addEventListener: function(){},
    onclick: null, textContent: '', innerHTML: '',
    scrollIntoView: function(){ __scrollCalled = true; __locatedAt = Date.now(); }
  };
  return el;
}
var document = {
  body: makeEl('body'),
  getElementById: function(id){
    if (id === 'focus-banner') return __banner;
    if (id === 'entity-body'){ var e = makeEl('div'); e.querySelectorAll = function(s){ return s === '.entity-item' ? __entities : []; }; return e; }
    if (id === 'conv-body'){ var c = makeEl('div'); c.querySelectorAll = function(s){ return s === '.conv-card' ? __convs : []; }; return c; }
    if (id === 'timeline-body'){ var t = makeEl('div'); return t; }
    return null;
  },
  createElement: function(tag){ return makeEl(tag); },
  querySelector: function(sel){
    var m = sel.match(/^\[data-msgid="(.+)"\]$/);
    if (m) return __rows[m[1]] || null;
    return null;
  },
  querySelectorAll: function(){ return []; }
};
var location = { search: '' };

// renderTimeline stub: rebuild rows from timelineMsgs so focusCheckRow can find them
function renderTimeline(scrollToBottom){
  __rows = {};
  var body = document.getElementById('timeline-body');
  body._children = [];
  timelineMsgs.forEach(function(mm){
    var row = makeEl('div');
    row.setAttribute('data-msgid', mm.msgid);
    body.appendChild(row);
    __rows[mm.msgid] = row;
  });
}

// fetch stub: delayed response; initial vs older page determined by `before=`
var __fetchPlan = null;
function fetch(url){
  var isOlder = /[?&]before=/.test(url);
  var delay = __fetchPlan.delay;
  var payload = isOlder ? __fetchPlan.older : __fetchPlan.initial;
  return new Promise(function(resolve){
    setTimeout(function(){
      if (!isOlder) __firstScreenResolvedAt = Date.now();
      resolve({ ok: true, json: function(){ return Promise.resolve(payload); } });
    }, delay);
  });
}

// ===== real console functions (extracted from _REVIEW_CONSOLE_JS) =====
__REAL_FNS__

// ===== scenario driver =====
function buildMessages(ids){ return ids.map(function(id){ return { msgid: id }; }); }
function runScenario(scn){
  focusMsgId = null; focusPending = false; timelineConvId = null; timelineMsgs = [];
  timelineHasOlder = false; timelineNextBefore = null; timelineRequestGen = 0;
  timelineLoadingOlder = false; timelineHistoryError = null; selEntityId = null; selConvId = null;
  __rows = {}; __banner = null; __firstScreenResolvedAt = 0; __locatedAt = 0; __scrollCalled = false;

  var ent = makeEl('div'); ent.dataset.id = scn.entityId; ent.classList.add('entity-item'); __entities = [ent];
  var card = makeEl('div'); card.dataset.id = scn.convId; card.dataset.name = 'Conv'; card.dataset.type = 'direct'; card.classList.add('conv-card'); __convs = [card];
  __fetchPlan = {
    delay: scn.delay,
    initial: { messages: buildMessages(scn.initialIds), pagination: { has_older: scn.initialHasOlder, next_before: scn.initialNextBefore } },
    older:   { messages: buildMessages(scn.olderIds),   pagination: { has_older: scn.olderHasOlder,   next_before: null } }
  };

  // Drives the REAL focusMessage -> (entity/conv select) -> loadTimeline ->
  // fetchTimelinePage(isInitial) -> focusPending consumed -> focusCheckRow chain.
  focusMessage(scn.target, scn.convId, 'direct', scn.entityId, 'staff');

  return new Promise(function(resolve){
    setTimeout(function(){
      var target = __rows[scn.target];
      resolve({
        name: scn.name,
        located: !!(target && target.classList.contains('target-flash')),
        banner: !!__banner,
        scrollCalled: __scrollCalled,
        locatedAt: __locatedAt,
        firstScreenResolvedAt: __firstScreenResolvedAt,
        hasTargetInDom: !!target
      });
    }, scn.wait);
  });
}

var __SCN = __SCENARIO__;
runScenario(__SCN).then(function(r){ process.stdout.write(JSON.stringify(r)); });
'''


def _run(scenario: dict) -> dict:
    assert NODE, "node executable not found"
    js = _HARNESS.replace("__REAL_FNS__", _bundle_real_fns()).replace(
        "__SCENARIO__", json.dumps(scenario)
    )
    result = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError("node harness failed (%s):\n%s" % (result.returncode, result.stderr))
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def test_normal_first_screen_locate():
    scn = {
        "name": "normal_first_screen",
        "target": "m15",
        "convId": "conv-1",
        "entityId": "ent-1",
        "delay": 150,
        "initialIds": ["m1", "m2", "m3", "m15", "m20"],
        "initialHasOlder": False,
        "initialNextBefore": None,
        "olderIds": [],
        "olderHasOlder": False,
        "wait": 3000,
    }
    r = _run(scn)
    assert r["located"], "target row should be located + highlighted on fast first screen: %s" % r
    assert r["banner"], "focus banner should appear: %s" % r
    assert r["scrollCalled"], "scrollIntoView should have been called: %s" % r


def test_slow_first_screen_locate_waits_for_render():
    scn = {
        "name": "slow_first_screen",
        "target": "m15",
        "convId": "conv-1",
        "entityId": "ent-1",
        "delay": 900,  # slower than the old 350ms fixed timer
        "initialIds": ["m1", "m2", "m3", "m15", "m20"],
        "initialHasOlder": False,
        "initialNextBefore": None,
        "olderIds": [],
        "olderHasOlder": False,
        "wait": 4000,
    }
    r = _run(scn)
    assert r["located"], "target row should be located even when first screen is slow: %s" % r
    assert r["banner"], "focus banner should appear on slow first screen: %s" % r
    # The locate must happen AFTER the first-screen render completed, not on a
    # fixed 350ms timer (which would fire before the 900ms fetch and miss).
    assert r["firstScreenResolvedAt"] > 0, "first screen must have resolved: %s" % r
    assert r["locatedAt"] >= r["firstScreenResolvedAt"], (
        "locate must occur after first-screen render, not on a fixed timer: %s" % r
    )


def test_target_in_history_pagination():
    scn = {
        "name": "target_in_history",
        "target": "m25",
        "convId": "conv-1",
        "entityId": "ent-1",
        "delay": 300,
        # first screen does NOT contain the target; older pages do.
        "initialIds": ["m1", "m2", "m3", "m20"],
        "initialHasOlder": True,
        "initialNextBefore": "B1",
        "olderIds": ["m21", "m25", "m40"],
        "olderHasOlder": False,
        "wait": 5000,
    }
    r = _run(scn)
    assert r["located"], "target in older page should be fetched + located: %s" % r
    assert r["banner"], "focus banner should appear for history target: %s" % r
    assert r["scrollCalled"], "scrollIntoView should have been called: %s" % r
