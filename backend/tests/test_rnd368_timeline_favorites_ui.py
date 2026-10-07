"""RND-368 timeline favorite UI and i18n contracts."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_html

_BACKEND = Path(__file__).resolve().parent.parent
_FAVORITES_JS = _BACKEND / "app" / "web" / "static" / "console" / "timeline-favorites.js"
_TIMELINE_JS = _BACKEND / "app" / "web" / "static" / "console" / "timeline.js"
_CONVERSATION_LIST_JS = _BACKEND / "app" / "web" / "static" / "console" / "conversation-list.js"
_API_CLIENT_JS = _BACKEND / "app" / "web" / "static" / "console" / "api-client.js"
_REFRESH_JS = _BACKEND / "app" / "web" / "static" / "console" / "refresh.js"
_CONSOLE_ENTRY_JS = _BACKEND / "app" / "web" / "static" / "console" / "console-entry.js"
_I18N_JS = _BACKEND / "app" / "assets" / "i18n.js"
_TEMPLATE = review_console_html()
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _run(script: str) -> str:
    harness = """
var I18N_SOURCE = %s;
eval(I18N_SOURCE);
var document = {
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  addEventListener: function () {}
};
var timelineMsgs = [];
var timelineConvId = 'conversation-1';
var timelineRequestGen = 1;
var timelineFavoritesOnly = false;
var timelineMode = 'staff';
var timelineEntityId = 'staff-1';
var timelineConvType = 'group';
var selConvId = 'conversation-1';
var deleteMode = false;
var favoriteMode = false;
var favoriteSelection = {};
var favoriteStates = {};
var favoriteStateVersions = {};
var favoriteUserCanWrite = false;
var favoriteBusy = false;
var favoriteStatusFailed = false;
var favoriteStatusRetrying = false;
var selectedMsgId = null;
function esc(value) { return String(value == null ? '' : value).replace(/[&<>\"]/g, function (char) { return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[char]; }); }
function isNearBottom() { return false; }
function applyTimelineRefresh() {}
function renderTimeline() {}
eval(%s);
eval(%s);
%s
""" % (
        json.dumps(_I18N_JS.read_text(encoding="utf-8")),
        json.dumps(_TIMELINE_JS.read_text(encoding="utf-8")),
        json.dumps(_FAVORITES_JS.read_text(encoding="utf-8")),
        script,
    )
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    return result.stdout


def _extract_function(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    match = re.search(r"^function\s+" + re.escape(name) + r"\([^)]*\)\s*\{.*?^\}", source, re.M | re.S)
    assert match is not None, "could not find function %s in %s" % (name, path)
    return match.group(0)


def test_favorite_status_permission_batch_mutation_and_filter_payload() -> None:
    output = _run(
        """
(async function () {
  var calls = [];
  globalThis.fetch = function (url, options) {
    var body = JSON.parse(options.body);
    calls.push({url: url, body: body});
    if (url === '/api/favorites/status') return Promise.resolve({ok: true, json: function () {
      return Promise.resolve({items: body.items.map(function (item) {
        return {object_type: 'message', object_id: item.object_id, result: 'found', is_favorited: false};
      })});
    }});
    return Promise.resolve({ok: true, json: function () {
      return Promise.resolve({requested: 2, unique: 2, applied: 1, unchanged: 1, not_found: 0, items: [
        {object_type: 'message', object_id: 'm1', result: 'favorited'},
        {object_type: 'message', object_id: 'm2', result: 'already_favorited'}
      ]});
    }});
  };
  timelineMsgs = [{msgid: 'm1'}, {msgid: 'm2'}];
  await loadTimelineFavoriteStatuses(timelineMsgs, timelineConvId, timelineRequestGen);
  updateTimelineFavoriteRole('readonlyaudit');
  favoriteSelection = {m1: true, m2: true};
  var readonly = await runTimelineFavoriteAction('favorite', ['m1', 'm2']);
  if (readonly !== false || calls.some(function (call) { return call.url === '/api/favorites/batch'; })) throw new Error('read-only role reached mutation API');
  updateTimelineFavoriteRole('compliance');
  var result = await runTimelineFavoriteAction('favorite', ['m1', 'm2']);
  if (result.applied !== 1 || result.unchanged !== 1 || result.not_found !== 0) throw new Error('batch result was not preserved');
  if (!favoriteStates.m1.isFavorited || !favoriteStates.m2.isFavorited) throw new Error('favorite statuses were not updated');
  if (Object.keys(favoriteSelection).length !== 0) throw new Error('successful batch retained selection');
  var write = calls.filter(function (call) { return call.url === '/api/favorites/batch'; })[0];
  if (write.body.action !== 'favorite' || write.body.items.some(function (item) { return item.source_page !== 'messages' || item.object_type !== 'message'; })) throw new Error('mutation payload was not canonical');
  timelineFavoritesOnly = true;
  if (timelineEntityQueryParams().indexOf('&favorited_only=true') === -1) throw new Error('favorite filter is not server-side');
  console.log('readonly=blocked mutation=2 statuses=updated selection=cleared serverFilter=enabled');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "readonly=blocked mutation=2 statuses=updated selection=cleared serverFilter=enabled"


def test_unavailable_status_removes_message_and_clears_selection() -> None:
    output = _run(
        """
(async function () {
  globalThis.fetch = function (_url, _options) {
    return Promise.resolve({ok: true, json: function () { return Promise.resolve({items: [
      {object_type: 'message', object_id: 'deleted-id', result: 'not_found'}
    ]}); }});
  };
  timelineMsgs = [{msgid: 'deleted-id'}, {msgid: 'active-id'}];
  favoriteSelection = {'deleted-id': true};
  await loadTimelineFavoriteStatuses(timelineMsgs.slice(0, 1), timelineConvId, timelineRequestGen);
  if (timelineMsgs.length !== 1 || timelineMsgs[0].msgid !== 'active-id') throw new Error('unavailable message remained in the timeline');
  if (favoriteSelection['deleted-id']) throw new Error('unavailable message remained selected');
  if (favoriteStates['deleted-id'].result !== 'not_found') throw new Error('unavailable status was not retained');
  console.log('unavailable=removed selection=cleared');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "unavailable=removed selection=cleared"


def test_status_failure_is_visible_and_retryable() -> None:
    output = _run(
        """
(async function () {
  var retry = {hidden: true, disabled: false};
  var result = {textContent: '', hidden: true, classList: {toggle: function () {}}, parentNode: {classList: {toggle: function () {}}}};
  var elements = {'timeline-favorite-status-retry': retry, 'timeline-favorite-result': result};
  document.getElementById = function (id) { return elements[id] || null; };
  timelineMsgs = [{msgid: 'm1'}];
  globalThis.fetch = function () { return Promise.reject(new Error('offline')); };
  await loadTimelineFavoriteStatuses(timelineMsgs, timelineConvId, timelineRequestGen);
  if (!favoriteStatusFailed || retry.hidden || result.textContent !== I18N.t('favorites.statusFailed')) throw new Error('status failure was not announced');
  globalThis.fetch = function (_url, options) {
    var body = JSON.parse(options.body);
    return Promise.resolve({ok: true, json: function () { return Promise.resolve({items: body.items.map(function (item) {
      return {object_type: 'message', object_id: item.object_id, result: 'found', is_favorited: false};
    })}); }});
  };
  retryTimelineFavoriteStatuses();
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (favoriteStatusFailed || retry.hidden !== true || result.textContent !== '') throw new Error('status retry did not recover');
  console.log('statusFailure=announced retry=recovers');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "statusFailure=announced retry=recovers"


def test_favorite_controls_are_loaded_and_copy_exists_in_supported_locales() -> None:
    assert 'id="btn-favorite-mode"' in _TEMPLATE
    assert 'id="timeline-favorites-only"' in _TEMPLATE
    assert 'id="favorite-selection-bar"' in _TEMPLATE
    assert 'id="timeline-favorite-result"' in _TEMPLATE
    assert 'id="timeline-favorite-status-retry"' in _TEMPLATE
    assert 'console/timeline-favorites.js' in _TEMPLATE
    output = _run(
        """
var required = ['favorites.enterSelection', 'favorites.onlyMessages', 'favorites.selectAllLoaded', 'favorites.favoriteSelected', 'favorites.unfavoriteSelected', 'favorites.permissionDenied', 'favorites.actionFailed', 'favorites.viewAll'];
var locales = ['zh-CN', 'zh-TW', 'en'];
var result = {};
locales.forEach(function (locale) {
  I18N.setLocale(locale);
  result[locale] = required.every(function (key) { return I18N.t(key) !== key; });
});
process.stdout.write(JSON.stringify({locales: I18N.availableLocales().map(function (item) { return item.code; }).sort(), translated: result}));
"""
    )
    assert json.loads(output) == {
        "locales": ["en", "zh-CN", "zh-TW"],
        "translated": {"zh-CN": True, "zh-TW": True, "en": True},
    }


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_switching_conversation_during_write_loads_new_favorites_status() -> None:
    functions = {
        "favorites": json.dumps(_FAVORITES_JS.read_text(encoding="utf-8")),
        "fetch_page": json.dumps(_extract_function(_TIMELINE_JS, "fetchTimelinePage")),
        "load_timeline": json.dumps(_extract_function(_API_CLIENT_JS, "loadTimeline")),
        "on_conv_click": json.dumps(_extract_function(_CONVERSATION_LIST_JS, "onConvClick")),
    }
    harness = r"""
var elements = {
  'timeline-header': {textContent: ''}, 'timeline-body': {innerHTML: ''}
};
function node(id) { return elements[id] || null; }
var cardA = {dataset: {id: 'conversation-A', name: 'A', type: 'group'}, classList: {add: function(){}, remove: function(){}}};
var cardB = {dataset: {id: 'conversation-B', name: 'B', type: 'group'}, classList: {add: function(){}, remove: function(){}}};
var document = {
  getElementById: node,
  querySelectorAll: function (selector) { return selector === '.conv-card' ? [cardA, cardB] : []; },
  querySelector: function () { return null; }
};
var I18N = {t: function (key) { return key; }};
var timelineConvId = 'conversation-A', timelineRequestGen = 1, timelineMsgs = [{msgid: 'message-A'}];
var timelineHasOlder = false, timelineNextBefore = null, timelineLoadingOlder = false;
var timelineFavoritesOnly = false, timelineMode = 'staff', timelineEntityId = 'staff-1', timelineConvType = 'group';
var selConvId = 'conversation-A', selConvName = 'A', selEntityId = 'staff-1', mode = 'staff', deleteMode = false;
var favoriteMode = false, favoriteSelection = {}, favoriteStates = {'message-A': {result: 'found', isFavorited: false}};
var favoriteStateVersions = {}, favoriteUserCanWrite = true, favoriteBusy = false;
var favoriteStatusFailed = false, favoriteStatusRetrying = false, timelineFavoriteRevision = 0;
var selectedMsgId = null, focusPending = false;
var statusCalls = 0, batchResolve;
function clearDeleteSelection() {}
function updateDeleteModeButton() {}
function updateConversationExportButton() {}
function clearPanelForNewConversation() {}
function renderTimeline() {}
function startHistoryObserver() {}
function stopHistoryObserver() {}
function hideNewMessageIndicator() {}
function loadConversationDetail() {}
function timelineEntityQueryParams() { return ''; }
function handleUnauth() { return false; }
function response(data) { return Promise.resolve({ok: true, json: function () { return Promise.resolve(data); }}); }
function fetch(url, options) {
  if (url.indexOf('/api/conversations/conversation-B/messages?') === 0) return response({messages: [{msgid: 'message-B'}], pagination: {has_older: false, next_before: null}});
  if (url === '/api/favorites/status') {
    statusCalls += 1;
    var body = JSON.parse(options.body);
    return response({items: body.items.map(function (item) { return {object_type: 'message', object_id: item.object_id, result: 'found', is_favorited: false}; })});
  }
  if (url === '/api/favorites/batch') return new Promise(function (resolve) { batchResolve = function (data) { resolve({ok: true, json: function () { return Promise.resolve(data); }}); }; });
  throw new Error('unexpected request: ' + url);
}
global.document = document; global.I18N = I18N; global.fetch = fetch;
eval(%(favorites)s); eval(%(fetch_page)s); eval(%(load_timeline)s); eval(%(on_conv_click)s);
(async function () {
  var write = runTimelineFavoriteAction('favorite', ['message-A']);
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (!batchResolve) throw new Error('favorite write did not start');
  onConvClick(cardB);
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (timelineConvId !== 'conversation-B' || timelineMsgs[0].msgid !== 'message-B') throw new Error('actual conversation-click path did not load B');
  if (statusCalls !== 0 || timelineFavoriteStatusReady('message-B')) throw new Error('fixture failed to hold B status loading behind A write');
  batchResolve({requested: 1, unique: 1, applied: 1, unchanged: 0, not_found: 0, items: [{object_type: 'message', object_id: 'message-A', result: 'favorited'}]});
  await write;
  if (statusCalls !== 1 || !timelineFavoriteStatusReady('message-B')) throw new Error('new conversation remained unhydrated after old write settled');
  console.log('onConvClick=B loaded B status=ready after A write');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
""" % functions
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    assert result.stdout.strip() == "onConvClick=B loaded B status=ready after A write"


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_sync_refresh_does_not_restore_a_removed_favorite_row() -> None:
    functions = {
        "favorites": json.dumps(_FAVORITES_JS.read_text(encoding="utf-8")),
        "refresh_timeline": json.dumps(_extract_function(_REFRESH_JS, "refreshTimelineIfSelected")),
        "refresh_data": json.dumps(_extract_function(_REFRESH_JS, "refreshData")),
        "refresh_for_version": json.dumps(_extract_function(_REFRESH_JS, "refreshForSyncVersion")),
        "merge": json.dumps(_extract_function(_TIMELINE_JS, "mergeMessagesByMsgid")),
    }
    harness = r"""
var body = {scrollTop: 0};
var document = {getElementById: function (id) { return id === 'timeline-body' ? body : null; }, querySelector: function () { return null; }};
var I18N = {t: function (key) { return key; }};
var timelineConvId = 'conversation-C', timelineRequestGen = 7, timelineMsgs = [{msgid: 'message-C', msgtime: 10}];
var timelineFavoritesOnly = true, timelineMode = 'staff', timelineEntityId = 'staff-1', timelineConvType = 'group';
var timelineLoadingOlder = false, timelineFavoriteRevision = 0, favoriteSelection = {};
var favoriteStates = {'message-C': {result: 'found', isFavorited: true}}, favoriteStateVersions = {};
var favoriteUserCanWrite = true, favoriteBusy = false, favoriteMode = false;
var favoriteStatusFailed = false, favoriteStatusRetrying = false, deleteMode = false;
var selectedMsgId = null, lastRenderedTimelineSignature = null, refreshInFlight = false, selEntityId = null;
var requestCount = 0, resolveOldRefresh;
function handleUnauth() { return false; }
function timelineEntityQueryParams() { return '&mode=staff&staff_id=staff-1&conversation_type=group&favorited_only=true'; }
function isNearBottom() { return false; }
function timelineSignature(messages) { return JSON.stringify((messages || []).map(function (message) { return message.msgid; })); }
function applyTimelineRefresh() {}
function renderTimeline() {}
function finishRefresh() { refreshInFlight = false; }
function refreshEntityList() { return Promise.resolve(); }
function refreshConversationList() { return Promise.resolve(); }
function response(data) { return Promise.resolve({ok: true, json: function () { return Promise.resolve(data); }}); }
function fetch(url, options) {
  if (url.indexOf('/api/conversations/conversation-C/messages?') === 0) {
    requestCount += 1;
    if (requestCount === 1) return new Promise(function (resolve) { resolveOldRefresh = function (data) { resolve({ok: true, json: function () { return Promise.resolve(data); }}); }; });
    return response({messages: []});
  }
  if (url === '/api/favorites/batch') return response({requested: 1, unique: 1, applied: 1, unchanged: 0, not_found: 0, items: [{object_type: 'message', object_id: 'message-C', result: 'unfavorited'}]});
  if (url === '/api/favorites/status') {
    var body = JSON.parse(options.body);
    return response({items: body.items.map(function (item) { return {object_type: 'message', object_id: item.object_id, result: 'found', is_favorited: false}; })});
  }
  throw new Error('unexpected request: ' + url);
}
global.document = document; global.I18N = I18N; global.fetch = fetch;
eval(%(favorites)s); eval(%(merge)s); eval(%(refresh_timeline)s); eval(%(refresh_data)s); eval(%(refresh_for_version)s);
(async function () {
  refreshForSyncVersion();
  for (var i = 0; i < 20 && requestCount < 1; i++) await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (!resolveOldRefresh) throw new Error('sync-version refresh did not start');
  await runTimelineFavoriteAction('unfavorite', ['message-C']);
  if (timelineMsgs.length) throw new Error('fixture did not remove the favorite first');
  resolveOldRefresh({messages: [{msgid: 'message-C', msgtime: 10}]});
  for (var j = 0; j < 30 && requestCount < 2; j++) await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (requestCount < 2) throw new Error('changed favorite state did not trigger a fresh filtered read');
  for (var k = 0; k < 30 && refreshInFlight; k++) await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (timelineMsgs.length || favoriteStates['message-C'].isFavorited !== false) throw new Error('stale refresh restored an unfavorited row');
  console.log('sync-version-refresh=reconciled unfavorited-row=absent');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
""" % functions
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    assert result.stdout.strip() == "sync-version-refresh=reconciled unfavorited-row=absent"


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_failed_favorite_write_invalidates_stale_favorites_only_refresh() -> None:
    functions = {
        "favorites": json.dumps(_FAVORITES_JS.read_text(encoding="utf-8")),
        "refresh_timeline": json.dumps(_extract_function(_REFRESH_JS, "refreshTimelineIfSelected")),
        "refresh_data": json.dumps(_extract_function(_REFRESH_JS, "refreshData")),
        "refresh_for_version": json.dumps(_extract_function(_REFRESH_JS, "refreshForSyncVersion")),
        "merge": json.dumps(_extract_function(_TIMELINE_JS, "mergeMessagesByMsgid")),
    }
    harness = r"""
var body = {scrollTop: 0};
var document = {getElementById: function (id) { return id === 'timeline-body' ? body : null; }, querySelector: function () { return null; }};
var I18N = {t: function (key) { return key; }};
var timelineConvId = 'conversation-E', timelineRequestGen = 8, timelineMsgs = [{msgid: 'message-E', msgtime: 20}];
var timelineFavoritesOnly = true, timelineMode = 'staff', timelineEntityId = 'staff-1', timelineConvType = 'group';
var timelineLoadingOlder = false, timelineFavoriteRevision = 0, favoriteSelection = {};
var favoriteStates = {'message-E': {result: 'found', isFavorited: true}}, favoriteStateVersions = {};
var favoriteUserCanWrite = true, favoriteBusy = false, favoriteMode = false;
var favoriteStatusFailed = false, favoriteStatusRetrying = false, deleteMode = false;
var selectedMsgId = null, lastRenderedTimelineSignature = null, refreshInFlight = false, selEntityId = null;
var timelineReads = 0, statusCalls = 0, resolveOldRefresh;
function handleUnauth() { return false; }
function timelineEntityQueryParams() { return '&mode=staff&staff_id=staff-1&conversation_type=group&favorited_only=true'; }
function isNearBottom() { return false; }
function timelineSignature(messages) { return JSON.stringify((messages || []).map(function (message) { return message.msgid; })); }
function applyTimelineRefresh() {}
function renderTimeline() {}
function finishRefresh() { refreshInFlight = false; }
function refreshEntityList() { return Promise.resolve(); }
function refreshConversationList() { return Promise.resolve(); }
function response(data) { return Promise.resolve({ok: true, json: function () { return Promise.resolve(data); }}); }
function fetch(url, options) {
  if (url.indexOf('/api/conversations/conversation-E/messages?') === 0) {
    timelineReads += 1;
    if (timelineReads === 1) return new Promise(function (resolve) { resolveOldRefresh = function (data) { resolve({ok: true, json: function () { return Promise.resolve(data); }}); }; });
    return response({messages: []});
  }
  if (url === '/api/favorites/batch') return Promise.reject(new Error('write response lost'));
  if (url === '/api/favorites/status') {
    statusCalls += 1;
    if (statusCalls === 1) {
      return response({items: [{object_type: 'message', object_id: 'message-E', result: 'found', is_favorited: false}]});
    }
    return Promise.reject(new Error('status retry unavailable'));
  }
  throw new Error('unexpected request: ' + url);
}
global.document = document; global.I18N = I18N; global.fetch = fetch;
eval(%(favorites)s); eval(%(merge)s); eval(%(refresh_timeline)s); eval(%(refresh_data)s); eval(%(refresh_for_version)s);
(async function () {
  refreshForSyncVersion();
  for (var i = 0; i < 20 && !resolveOldRefresh; i++) await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (!resolveOldRefresh) throw new Error('fixture did not start the stale refresh');
  await runTimelineFavoriteAction('unfavorite', ['message-E']);
  if (timelineMsgs.length || favoriteStates['message-E'].isFavorited !== false) throw new Error('fixture did not reconcile the lost write as unfavorited');
  resolveOldRefresh({messages: [{msgid: 'message-E', msgtime: 20}]});
  for (var j = 0; j < 50 && (timelineReads < 2 || refreshInFlight); j++) await new Promise(function (resolve) { setTimeout(resolve, 0); });
  for (var k = 0; k < 5; k++) await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (timelineReads !== 2 || statusCalls !== 1) throw new Error('stale response was not discarded in favor of one authoritative list read');
  if (timelineMsgs.length) throw new Error('late favorites-only refresh reinserted the cancelled row after status retry failure');
  console.log('lost-write=invalidated stale-refresh status-retry=failure row=absent');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
""" % functions
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    assert result.stdout.strip() == "lost-write=invalidated stale-refresh status-retry=failure row=absent"


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_search_location_history_page_clears_favorite_selection_at_fetch_boundary() -> None:
    functions = {
        "favorites": json.dumps(_FAVORITES_JS.read_text(encoding="utf-8")),
        "fetch_older": json.dumps(_extract_function(_TIMELINE_JS, "fetchOlderMessages")),
        "focus_check": json.dumps(_extract_function(_CONSOLE_ENTRY_JS, "focusCheckRow")),
    }
    harness = r"""
var loaded = false, cleared = 0;
var targetRow = {scrollIntoView: function () {}, classList: {add: function () {}}, addEventListener: function () {}};
var document = {
  querySelector: function () { return loaded ? targetRow : null; },
  getElementById: function () { return null; }
};
var I18N = {t: function (key) { return key; }};
var timelineConvId = 'conversation-D', timelineRequestGen = 4, timelineMsgs = [{msgid: 'newer'}];
var timelineHasOlder = true, timelineNextBefore = 'cursor-1', timelineMode = 'staff', timelineEntityId = 'staff-1', timelineConvType = 'group';
var timelineFavoritesOnly = false, favoriteMode = true, favoriteSelection = {selected: true};
var favoriteStates = {}, favoriteStateVersions = {}, favoriteUserCanWrite = true, favoriteBusy = false;
var favoriteStatusFailed = false, favoriteStatusRetrying = false, deleteMode = false;
var focusMsgId = 'target', focusIsUrlArrival = false, selectedMsgId = null;
function clearTimelineFavoriteSelection() { favoriteSelection = {}; cleared += 1; }
function timelineEntityQueryParams() { return ''; }
function handleUnauth() { return false; }
function renderTimeline() {}
function loadTimelineFavoriteStatuses() {}
function fetch(url) {
  if (url.indexOf('/api/conversations/conversation-D/messages?') !== 0) throw new Error('unexpected request: ' + url);
  loaded = true;
  return Promise.resolve({ok: true, json: function () { return Promise.resolve({messages: [{msgid: 'target'}], pagination: {has_older: false, next_before: null}}); }});
}
global.document = document; global.I18N = I18N; global.fetch = fetch;
eval(%(favorites)s);
var clearActual = clearTimelineFavoriteSelection;
clearTimelineFavoriteSelection = function () { clearActual(); cleared += 1; };
eval(%(fetch_older)s); eval(%(focus_check)s);
(async function () {
  focusCheckRow();
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  if (cleared !== 1 || Object.keys(favoriteSelection).length) throw new Error('locator-driven history fetch retained favorite selection');
  await new Promise(function (resolve) { setTimeout(resolve, 270); });
  console.log('locator-history-fetch=selection-cleared target=focused');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
""" % functions
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    assert result.stdout.strip() == "locator-history-fetch=selection-cleared target=focused"
