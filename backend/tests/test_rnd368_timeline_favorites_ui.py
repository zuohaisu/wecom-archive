"""RND-368 timeline favorite UI and i18n contracts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_html

_BACKEND = Path(__file__).resolve().parent.parent
_FAVORITES_JS = _BACKEND / "app" / "web" / "static" / "console" / "timeline-favorites.js"
_TIMELINE_JS = _BACKEND / "app" / "web" / "static" / "console" / "timeline.js"
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
