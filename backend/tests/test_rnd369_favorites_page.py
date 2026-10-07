"""RND-369 favorites-page API and browser-controller contracts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node

_BACKEND = Path(__file__).resolve().parent.parent
_PAGE_JS = _BACKEND / "app" / "web" / "static" / "favorites-page.js"
_I18N_JS = _BACKEND / "app" / "assets" / "i18n.js"
_TEMPLATE = (_BACKEND / "app" / "web" / "templates" / "favorites.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _run(script: str) -> str:
    harness = r"""
var I18N_SOURCE = %s;
eval(I18N_SOURCE);
var elements = Object.create(null);
function escapeValue(value) {
  return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\"/g, '&quot;').replace(/'/g, '&#39;');
}
function Element(id) {
  this.id = id;
  this.value = '';
  this.textContent = '';
  this._innerHTML = '';
  this._nodeCache = Object.create(null);
  Object.defineProperty(this, 'innerHTML', {get: function () { return this._innerHTML; }, set: function (value) { this._innerHTML = value; this._nodeCache = Object.create(null); }});
  this.disabled = false;
  this.checked = false;
  this.attributes = Object.create(null);
  this.handlers = Object.create(null);
  this.classes = Object.create(null);
  this.classList = {
    toggle: function (name, force) { this.classes[name] = force === undefined ? !this.classes[name] : !!force; }.bind(this),
    add: function (name) { this.classes[name] = true; }.bind(this),
    remove: function (name) { delete this.classes[name]; }.bind(this),
    contains: function (name) { return !!this.classes[name]; }.bind(this)
  };
}
Element.prototype.addEventListener = function (name, callback) { this.handlers[name] = callback; };
Element.prototype.dispatch = function (name, event) {
  if (this.disabled) return;
  if (this.handlers[name]) this.handlers[name](event || {preventDefault: function () {}});
};
Element.prototype.setAttribute = function (name, value) { this.attributes[name] = String(value); };
Element.prototype.getAttribute = function (name) { return this.attributes[name] == null ? null : this.attributes[name]; };
Element.prototype.querySelectorAll = function (selector) {
  if (this.id !== 'favorites-rows') return [];
  if (this._nodeCache[selector]) return this._nodeCache[selector];
  var attr = selector.match(/\[([^\]]+)\]/);
  if (!attr) return [];
  var name = attr[1].split('=')[0];
  var pattern = new RegExp(name + '="([^\"]+)"([^>]*)', 'g');
  var output = [], match;
  while ((match = pattern.exec(this.innerHTML))) {
    var item = new Element(name);
    item.attributes[name] = match[1];
    item.disabled = /(?:^|\s)disabled(?:\s|$)/.test(match[2]);
    item.checked = /(?:^|\s)checked(?:\s|$)/.test(match[2]);
    output.push(item);
  }
  this._nodeCache[selector] = output;
  return output;
};
Element.prototype.reset = function () {
  ['favorites-filter-conversation', 'favorites-filter-staff', 'favorites-filter-contact', 'favorites-filter-media-type', 'favorites-filter-actor', 'favorites-filter-favorite-since', 'favorites-filter-favorite-until', 'favorites-filter-message-since', 'favorites-filter-message-until'].forEach(function (id) { elements[id].value = ''; });
};
var filterIds = ['favorites-filter-conversation', 'favorites-filter-staff', 'favorites-filter-contact', 'favorites-filter-media-type', 'favorites-filter-actor', 'favorites-filter-favorite-since', 'favorites-filter-favorite-until', 'favorites-filter-message-since', 'favorites-filter-message-until'];
var allIds = filterIds.concat(['favorites-filters', 'favorites-clear-filters', 'favorites-selection', 'favorites-selected-count', 'favorites-select-page', 'favorites-clear-selection', 'favorites-export-selected', 'favorites-unfavorite-selected', 'favorites-remove-notice', 'favorites-status', 'favorites-error', 'favorites-retry', 'favorites-table-wrap', 'favorites-rows', 'favorites-empty', 'favorites-results', 'favorites-previous', 'favorites-next', 'favorites-permission', 'lang-menu', 'current-user']);
allIds.forEach(function (id) { elements[id] = new Element(id); });
var document = {
  documentElement: {lang: 'zh-CN'},
  title: '',
  addEventListener: function () {},
  getElementById: function (id) { return elements[id] || (elements[id] = new Element(id)); },
  querySelectorAll: function (selector) { return elements['favorites-rows'].querySelectorAll(selector); },
  createElement: function () {
    var div = {};
    Object.defineProperty(div, 'textContent', {set: function (value) { this._value = value; }});
    Object.defineProperty(div, 'innerHTML', {get: function () { return escapeValue(this._value); }});
    return div;
  }
};
var window = {location: {href: ''}};
var sessionStorage = {values: Object.create(null), setItem: function (key, value) { this.values[key] = value; }, getItem: function (key) { return this.values[key] || null; }};
var openedViewer = null;
function openViewer(items, index) { openedViewer = {items: items, index: index}; }
var PAGE_SOURCE = %s;
%s
""" % (
        json.dumps(_I18N_JS.read_text(encoding="utf-8")),
        json.dumps(_PAGE_JS.read_text(encoding="utf-8")),
        script,
    )
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    return result.stdout


def test_favorites_page_builds_server_filter_query_and_supported_locales() -> None:
    assert 'id="favorites-filters"' in _TEMPLATE
    assert 'id="favorites-filter-conversation"' in _TEMPLATE
    assert 'id="favorites-filter-staff"' in _TEMPLATE
    assert 'id="favorites-filter-contact"' in _TEMPLATE
    assert 'id="favorites-filter-media-type"' in _TEMPLATE
    assert 'id="favorites-filter-favorite-since"' in _TEMPLATE
    assert 'id="favorites-filter-message-until"' in _TEMPLATE
    assert 'media-viewer.js' in _TEMPLATE
    assert 'favorites-page.js' in _TEMPLATE
    output = _run(
        r"""
(async function () {
  var query;
  globalThis.fetch = function (url) {
    if (url === '/api/auth/me') return Promise.resolve({ok: true, json: function () { return Promise.resolve({authenticated: true, role: 'owner'}); }});
    query = new URL('https://local.test' + url).searchParams;
    return Promise.resolve({ok: true, json: function () { return Promise.resolve({items: [], total: 0, offset: 0, limit: 50}); }});
  };
  elements['favorites-filter-conversation'].value = 'room/1';
  elements['favorites-filter-staff'].value = 'staff_1';
  elements['favorites-filter-contact'].value = 'Customer One';
  elements['favorites-filter-media-type'].value = 'image';
  elements['favorites-filter-actor'].value = 'Alice';
  elements['favorites-filter-favorite-since'].value = '2026-04-01';
  elements['favorites-filter-favorite-until'].value = '2026-04-02';
  elements['favorites-filter-message-since'].value = '2026-04-03';
  elements['favorites-filter-message-until'].value = '2026-04-04';
  eval(PAGE_SOURCE);
  await new Promise(function (resolve) { setTimeout(resolve, 10); });
  if (query.get('conversation_id') !== 'room/1' || query.get('staff_filter') !== 'staff_1' || query.get('contact_filter') !== 'Customer One' || query.get('media_type') !== 'image' || query.get('favorited_by_name') !== 'Alice') throw new Error('filter identifiers were not sent to the server');
  if (!query.get('favorited_since') || !query.get('favorited_until') || !query.get('message_since_ms') || !query.get('message_until_ms')) throw new Error('date ranges were not sent to the server');
  if (query.get('limit') !== '50' || query.get('offset') !== '0') throw new Error('pagination was not server-side');
  elements['favorites-filter-media-type'].value = 'message';
  elements['favorites-filters'].dispatch('submit', {preventDefault: function () {}});
  await new Promise(function (resolve) { setTimeout(resolve, 5); });
  if (query.get('object_type') !== 'message' || query.has('media_type')) throw new Error('message type filter was not represented distinctly');
  var locales = ['zh-CN', 'zh-TW', 'en'];
  var keys = Object.keys(LocaleRegistry['zh-CN'].translations).filter(function (key) { return key.indexOf('favoritesPage.') === 0 || key === 'nav.favorites'; });
  if (I18N.availableLocales().map(function (item) { return item.code; }).sort().join(',') !== 'en,zh-CN,zh-TW') throw new Error('unsupported locale was added');
  locales.forEach(function (locale) { keys.forEach(function (key) { if (!LocaleRegistry[locale].translations[key]) throw new Error('missing ' + locale + ' translation: ' + key); }); });
  console.log('filters=server-side dates=inclusive locales=zh-CN,zh-TW,en translations=complete');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "filters=server-side dates=inclusive locales=zh-CN,zh-TW,en translations=complete"


def test_favorites_page_preview_batch_remove_and_escape_dynamic_content() -> None:
    output = _run(
        r"""
(async function () {
  var removed = false, batchBody = null, listCalls = 0;
  var message = {favorite_id: 'fav-message', object_type: 'message', object_id: 'msg-1', message_id: 'msg-1', message_time_ms: 1770000000000, conversation_id: 'room/1', conversation_name: 'Support & Safety', conversation_type: 'group', focus_entity_type: 'staff', focus_entity_id: 'staff_1', sender_id: 'staff_1', sender_name: 'Alice <script>', favorited_by_admin_user_id: 'admin-1', favorited_by_name: 'Owner', favorited_at: '2026-04-01T12:00:00Z', preview: '<img src=x onerror=alert(1)>'};
  var media = {favorite_id: 'fav-media', object_type: 'media', object_id: '7', message_id: 'msg-2', message_time_ms: 1770000001000, conversation_id: 'room/1', conversation_name: 'Support & Safety', conversation_type: 'group', focus_entity_type: 'staff', focus_entity_id: 'staff_1', sender_id: 'staff_1', sender_name: 'Alice', favorited_by_admin_user_id: 'admin-1', favorited_by_name: 'Owner', favorited_at: '2026-04-01T12:01:00Z', preview: 'caption', media_file_id: 7, media_type: 'image', media_mime_type: 'image/jpeg', media_size_bytes: 123, media_download_status: 'downloaded'};
  globalThis.fetch = function (url, options) {
    if (url === '/api/auth/me') return Promise.resolve({ok: true, json: function () { return Promise.resolve({authenticated: true, role: 'owner'}); }});
    if (url === '/api/favorites/batch') {
      batchBody = JSON.parse(options.body);
      removed = true;
      return Promise.resolve({ok: true, json: function () { return Promise.resolve({requested: 2, unique: 2, applied: 2, unchanged: 0, not_found: 0, items: []}); }});
    }
    if (url.indexOf('/api/favorites?') === 0) {
      listCalls += 1;
      return Promise.resolve({ok: true, json: function () { return Promise.resolve({items: removed ? [] : [message, media], total: removed ? 0 : 2, offset: 0, limit: 50}); }});
    }
    throw new Error('unexpected request: ' + url);
  };
  eval(PAGE_SOURCE);
  await new Promise(function (resolve) { setTimeout(resolve, 10); });
  var html = elements['favorites-rows'].innerHTML;
  if (html.indexOf('<img src=x') !== -1 || html.indexOf('&lt;img src=x') === -1 || html.indexOf('Alice &lt;script&gt;') === -1) throw new Error('archive-derived text was not escaped');
  var previewButton = elements['favorites-rows'].querySelectorAll('[data-favorites-preview]')[0];
  if (!previewButton || previewButton.disabled) throw new Error('downloaded media preview was unavailable');
  previewButton.dispatch('click');
  if (!openedViewer || openedViewer.items[0].accessUrl.indexOf('/api/conversations/room%2F1/messages/msg-2/media/access?') !== 0) throw new Error('media preview did not use the controlled access route: ' + JSON.stringify(openedViewer));
  if (openedViewer.items[0].accessUrl.indexOf('mode=staff') === -1 || openedViewer.items[0].downloadUrl !== '/api/admin/media/7/download') throw new Error('media preview omitted its authorization context');
  elements['favorites-select-page'].dispatch('click');
  elements['favorites-export-selected'].dispatch('click');
  var prefill = JSON.parse(sessionStorage.getItem('wecom.exportPrefill') || 'null');
  if (!prefill || prefill.message_ids.join(',') !== 'msg-1,msg-2' || window.location.href !== '/admin/exports') throw new Error('selected favorites were not handed to the existing evidence export');
  elements['favorites-unfavorite-selected'].dispatch('click');
  await new Promise(function (resolve) { setTimeout(resolve, 15); });
  if (!batchBody || batchBody.action !== 'unfavorite' || batchBody.items.length !== 2 || batchBody.items.some(function (item) { return item.source_page !== 'favorites'; })) throw new Error('batch removal was not canonical');
  if (elements['favorites-rows'].innerHTML !== '' || listCalls < 2) throw new Error('successful removal did not refresh the server page');
  if (elements['favorites-status'].textContent.indexOf('2') === -1) throw new Error('mutation result was not announced');
  console.log('preview=controlled-context exportPrefill=2 batchUnfavorite=2 refreshed=true escaped=true');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "preview=controlled-context exportPrefill=2 batchUnfavorite=2 refreshed=true escaped=true"


def test_favorites_page_readonly_role_fails_closed() -> None:
    output = _run(
        r"""
(async function () {
  globalThis.fetch = function (url) {
    if (url === '/api/auth/me') return Promise.resolve({ok: true, json: function () { return Promise.resolve({authenticated: true, role: 'readonlyaudit'}); }});
    return Promise.resolve({ok: true, json: function () { return Promise.resolve({items: [{favorite_id: 'fav-1', object_type: 'message', object_id: 'msg-1', message_id: 'msg-1', conversation_id: 'room-1', conversation_name: 'Room', conversation_type: 'group', focus_entity_type: 'staff', focus_entity_id: 'staff_1', favorited_at: '2026-04-01T12:00:00Z'}], total: 1, offset: 0, limit: 50}); }});
  };
  eval(PAGE_SOURCE);
  await new Promise(function (resolve) { setTimeout(resolve, 10); });
  var checkbox = elements['favorites-rows'].querySelectorAll('[data-favorite-select]')[0];
  var remove = elements['favorites-rows'].querySelectorAll('[data-favorites-unfavorite]')[0];
  if (!checkbox || !checkbox.disabled || !remove || !remove.disabled) throw new Error('read-only role received enabled mutation controls');
  if (elements['favorites-permission'].textContent !== I18N.t('favoritesPage.permissionDenied')) throw new Error('permission denial was not explained');
  console.log('readonly=controls-disabled permission-copy=visible');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "readonly=controls-disabled permission-copy=visible"
