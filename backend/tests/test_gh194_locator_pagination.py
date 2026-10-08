"""GH-194: synthetic favorites deep-link loads repeated avatars across history pages."""
from __future__ import annotations

import json
import shutil

import pytest

from tests._node_runner import run_node
from tests.test_rnd229_focus_locate import _REVIEW_CONSOLE_JS, _extract_fn

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def test_favorite_locator_pages_history_while_rendering_repeated_avatar_urls() -> None:
    functions = {
        name: _extract_fn(_REVIEW_CONSOLE_JS, name)
        for name in (
            "readFocusFromUrl",
            "focusMessage",
            "focusCheckRow",
            "showFocusBanner",
            "fetchOlderMessages",
            "timelineEntityQueryParams",
        )
    }
    harness = r"""
var funcs = %(functions)s;
var location = {search: '?focus=target-msg&conv=room-1&convType=group&entityId=staff-1&entityType=staff'};
var mode = 'staff', selEntityId = 'staff-1', selConvId = 'room-1';
var convTypeFilter = 'all', focusMsgId = null, focusPending = false;
var focusIsUrlArrival = false;
var timelineConvId = 'room-1', timelineRequestGen = 9;
var timelineMode = 'staff', timelineEntityId = 'staff-1', timelineConvType = 'group';
var timelineFavoritesOnly = false, timelineHasOlder = true, timelineNextBefore = 'cursor-4';
var timelineLoadingOlder = false, timelineHistoryError = null;
var timelineMsgs = [
  {msgid: 'newer-1', sender_avatar_url: '/api/admin/avatars/internal/10'},
  {msgid: 'newer-2', sender_avatar_url: '/api/admin/avatars/internal/11'}
];
var avatarSourcesInLastRender = [], historyCursors = [], olderPageIndex = 0;
var rows = Object.create(null), banner = null, located = false;
var olderPages = [
  {messages: [
    {msgid: 'page-1-a', sender_avatar_url: '/api/admin/avatars/internal/10'},
    {msgid: 'page-1-b', sender_avatar_url: '/api/admin/avatars/internal/12'}
  ], pagination: {has_older: true, next_before: 'cursor-3'}},
  {messages: [
    {msgid: 'page-2-a', sender_avatar_url: '/api/admin/avatars/internal/11'},
    {msgid: 'page-2-b', sender_avatar_url: '/api/admin/avatars/internal/10'}
  ], pagination: {has_older: true, next_before: 'cursor-2'}},
  {messages: [
    {msgid: 'page-3-a', sender_avatar_url: '/api/admin/avatars/internal/12'},
    {msgid: 'page-3-b', sender_avatar_url: '/api/admin/avatars/internal/11'}
  ], pagination: {has_older: true, next_before: 'cursor-1'}},
  {messages: [
    {msgid: 'target-msg', sender_avatar_url: '/api/admin/avatars/internal/10'},
    {msgid: 'page-4-b', sender_avatar_url: '/api/admin/avatars/internal/12'}
  ], pagination: {has_older: false, next_before: null}}
];
var card = {dataset: {id: 'room-1'}};
function makeRow(message) {
  return {
    scrollIntoView: function () { located = true; },
    classList: {add: function () {}},
    addEventListener: function () {},
    message: message
  };
}
var document = {
  querySelector: function (selector) {
    var match = selector.match(/^\[data-msgid="(.+)"\]$/);
    return match ? rows[match[1]] || null : null;
  },
  getElementById: function (id) {
    if (id === 'focus-banner') return banner;
    if (id === 'conv-body') return {querySelectorAll: function () { return [card]; }};
    return null;
  },
  createElement: function () { return {id: '', style: {}, textContent: '', onclick: null}; },
  body: {appendChild: function (node) { banner = node; }}
};
var I18N = {t: function (key) { return key; }};
function setMode(value) { mode = value; }
function setConvTypeFilter(value) { convTypeFilter = value; }
function onEntityClick(element) { selEntityId = element.dataset.id; }
function onConvClick() { throw new Error('same-conversation locate should not reload'); }
function handleUnauth() { return false; }
function renderTimeline() {
  rows = Object.create(null);
  timelineMsgs.forEach(function (message) {
    rows[message.msgid] = makeRow(message);
  });
  avatarSourcesInLastRender = timelineMsgs
    .map(function (message) { return message.sender_avatar_url; })
    .filter(Boolean);
}
function fetch(url) {
  var parsed = new URL(url, 'https://synthetic.test');
  var cursor = parsed.searchParams.get('before');
  if (!cursor || olderPageIndex >= olderPages.length) throw new Error('unexpected history request: ' + url);
  historyCursors.push(cursor);
  return Promise.resolve({ok: true, json: function () {
    return Promise.resolve(olderPages[olderPageIndex++]);
  }});
}
var timelineEntityQueryParams = eval('(' + funcs.timelineEntityQueryParams + ')');
var fetchOlderMessages = eval('(' + funcs.fetchOlderMessages + ')');
var showFocusBanner = eval('(' + funcs.showFocusBanner + ')');
var focusCheckRow = eval('(' + funcs.focusCheckRow + ')');
var focusMessage = eval('(' + funcs.focusMessage + ')');
var readFocusFromUrl = eval('(' + funcs.readFocusFromUrl + ')');
renderTimeline();
readFocusFromUrl();
(async function () {
  var deadline = Date.now() + 5000;
  while (!located && Date.now() < deadline) {
    await new Promise(function (resolve) { setTimeout(resolve, 25); });
  }
  if (!located) throw new Error('target message was not located');
  var ids = avatarSourcesInLastRender.map(function (url) {
    return url.slice(url.lastIndexOf('/') + 1);
  });
  var uniqueIds = Array.from(new Set(ids));
  var repeatedIds = ids.filter(function (id, index) { return ids.indexOf(id) !== index; });
  console.log(JSON.stringify({
    located: located,
    historyPages: historyCursors.length,
    cursors: historyCursors,
    avatarSourceCount: avatarSourcesInLastRender.length,
    uniqueAvatarIds: uniqueIds.length,
    repeatedAvatarSourceCount: repeatedIds.length
  }));
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
""" % {"functions": json.dumps(functions)}
    result = run_node(harness, timeout=10)
    assert result.returncode == 0, "node locator harness failed: %s" % result.stderr
    assert json.loads(result.stdout) == {
        "located": True,
        "historyPages": 4,
        "cursors": ["cursor-4", "cursor-3", "cursor-2", "cursor-1"],
        "avatarSourceCount": 10,
        "uniqueAvatarIds": 3,
        "repeatedAvatarSourceCount": 7,
    }
