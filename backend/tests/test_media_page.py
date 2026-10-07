"""RND-329 contracts for the SSR admin media page."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app
from tests._node_runner import run_node

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "media.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_MEDIA_JS = _BACKEND / "app" / "web" / "static" / "media-library.js"
_FAVORITES_JS = _BACKEND / "app" / "web" / "static" / "media-favorites.js"
NODE = shutil.which("node")
_MEDIA_KEYS = (
    "media.pageTitle", "media.breadcrumbData", "media.description",
    "media.searchPlaceholder", "media.allTypes", "media.type.image",
    "media.type.file", "media.type.voice", "media.type.video",
    "media.allTime", "media.days7", "media.days30", "media.days90",
    "media.sortNewest", "media.sortOldest", "media.sortLargest",
    "media.preview", "media.unnamed", "media.empty", "media.results",
    "media.loadMore", "media.loadFailed", "media.enterSelection",
    "media.exitSelection", "media.favoritesOnly", "media.selectedCount",
    "media.selectAllLoaded", "media.clearSelection", "media.favoriteSelected",
    "media.unfavoriteSelected", "media.favoritePermissionDenied",
    "media.favoritePermissionUnavailable", "media.favoriteStatusLoading",
    "media.favoriteStatusFailed", "media.favoriteActionFailed",
    "media.favoritePartial", "media.favoriteResult", "media.favoriteUnavailable",
    "media.favorited", "media.notFavorited", "media.viewFavorites",
    "media.selectItem", "media.loadingMore", "media.favoritesEmpty",
)


def _authenticated_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


def test_media_page_requires_session() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: None
    client = TestClient(app)
    try:
        response = client.get("/admin/media", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
        client.close()
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_media_page_renders_without_tokens_and_enables_active_navigation() -> None:
    client = _authenticated_client()
    try:
        response = client.get("/admin/media")
    finally:
        client.app.dependency_overrides.clear()
        client.close()
    assert response.status_code == 200
    assert not re.search(r"__[A-Z0-9_]+__", response.text)
    assert '<a class="side-nav-item active" href="/admin/media" data-i18n="nav.mediaAttachments" aria-current="page"></a>' in response.text


def test_media_page_fetches_real_api_and_passes_type_time_and_favorite_filters() -> None:
    source = _MEDIA_JS.read_text(encoding="utf-8")
    assert 'fetch("/api/admin/media?" + query' in source
    for parameter in ("file_type", "days", "q", "sort", "offset", "limit", "favorited_only"):
        assert parameter in source
    for field in ("item.file_type", "item.file_size", "item.created_at", "item.session_title"):
        assert field in source
    assert "mock" not in source.lower()


def test_media_thumbnails_use_conversation_message_media_route_without_provider_urls() -> None:
    source = _MEDIA_JS.read_text(encoding="utf-8")
    assert "item.conversation_id" in source
    assert "item.msgid" in source
    assert '"/api/conversations/" + encodeURIComponent(item.conversation_id) + "/messages/" + encodeURIComponent(item.msgid) + "/media?variant=thumb"' in source
    assert not re.search(r"qiniu|QINIU_|qiniu\.com", source, re.IGNORECASE)


def test_media_page_reuses_the_conversation_review_overlay_for_every_media_kind() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    page_js = _MEDIA_JS.read_text(encoding="utf-8")
    viewer = (_BACKEND / "app" / "web" / "static" / "console" / "media-viewer.js").read_text(encoding="utf-8")
    assert 'class="toolbar toolbar-compact"' in source
    assert "/web/static/console/media-viewer.js" in source
    assert "openViewer(state.items.map(viewerItem), index)" in page_js
    assert "/media/access" in page_js
    assert "item.kind==='video'" in viewer
    assert "item.kind==='voice'" in viewer
    assert "item.kind==='file'" in viewer
    assert "target=\"_blank\"" not in source


def test_media_i18n_keys_exist_in_all_locales_directly_under_own_anchor() -> None:
    source = _I18N.read_text(encoding="utf-8")
    blocks = re.findall(
        r"/\* RND-329 media page keys — insert below \*/(.*?)/\* RND-330",
        source,
        re.S,
    )
    assert len(blocks) == 3
    for block in blocks:
        for key in _MEDIA_KEYS:
            assert f'"{key}"' in block


def test_media_selection_ui_is_accessible_and_consumes_shared_favorite_apis() -> None:
    template = _TEMPLATE.read_text(encoding="utf-8")
    page_js = _MEDIA_JS.read_text(encoding="utf-8")
    favorites_js = _FAVORITES_JS.read_text(encoding="utf-8")
    for element_id in (
        "media-select-mode", "media-selection-toolbar", "media-select-all",
        "media-clear-selection", "media-favorite-selected", "media-unfavorite-selected",
        "media-favorited-only", "media-favorites-link",
    ):
        assert 'id="%s"' % element_id in template
    assert 'role="status" aria-live="polite"' in template
    assert 'aria-pressed="false"' in template
    assert "Escape" in page_js
    assert "/api/favorites/status" in favorites_js
    assert "/api/favorites/batch" in favorites_js
    assert "source_page: \"media\"" in favorites_js
    assert "mediaUrl(item)" in page_js
    assert "/api/admin/media/" in page_js
    assert "/admin/favorites" in page_js
    assert not re.search(r"qiniu|QINIU_|qiniu\.com|signed_url|storage_ref", page_js + template, re.IGNORECASE)


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
def test_media_page_filter_clears_selection_and_favorite_action_does_not_reload_page() -> None:
    harness = """
var handlers = Object.create(null);
var elements = Object.create(null);
function element(id) {
  if (!elements[id]) {
    var classes = new Set();
    elements[id] = {
      id: id, value: id === 'media-sort' ? 'newest' : '', checked: false, disabled: false,
      textContent: '', innerHTML: '', placeholder: '', style: {display: 'none'}, attrs: {},
      classList: {
        add: function (name) { classes.add(name); },
        remove: function (name) { classes.delete(name); },
        contains: function (name) { return classes.has(name); },
        toggle: function (name, force) { if (force) classes.add(name); else classes.delete(name); return classes.has(name); }
      },
      addEventListener: function (type, callback) { handlers[id + ':' + type] = callback; },
      setAttribute: function (name, value) { this.attrs[name] = value; },
      getAttribute: function (name) { return this.attrs[name] || null; },
      focus: function () { this.focused = true; },
      insertAdjacentHTML: function (_where, html) { this.innerHTML += html; }
    };
  }
  return elements[id];
}
var document = {
  title: '',
  getElementById: element,
  createElement: function () { var value = ''; return {set textContent(v) { value = String(v); }, get innerHTML() { return value; }}; },
  querySelectorAll: function () { return []; },
  querySelector: function () { return null; },
  addEventListener: function (type, callback) { handlers['document:' + type] = callback; }
};
var window = globalThis;
window.scrollY = 0; window.scrollX = 0; window.scrollTo = function () {};
var I18N = {
  t: function (key) { return key === 'media.selectedCount' ? '{n} selected' : key; },
  getLocale: function () { return 'en-US'; }, availableLocales: function () { return []; },
  onChange: function () {}, setLocale: function () {}
};
var calls = [];
var favorited = false;
function response(data, ok) { return Promise.resolve({ok: ok !== false, json: function () { return Promise.resolve(data); }}); }
function fetch(url, options) {
  options = options || {}; calls.push({url: url, options: options});
  if (url === '/api/auth/me') return response({authenticated: true, role: 'owner'});
  if (url.indexOf('/api/auth/me/preferences') === 0) return response({});
  if (url.indexOf('/api/admin/media?') === 0) {
    var parsed = new URL(url, 'https://unit.test');
    var id = parsed.searchParams.get('favorited_only') === 'true' ? 1 : 2;
    return response({items: [{id: id, name: 'synthetic.png', file_type: 'image', mime_type: 'image/png', file_size: 10, created_at: '2026-01-01T00:00:00Z', conversation_id: 'c1', msgid: 'm1', session_title: 'Synthetic'}], total: 1, has_more: false});
  }
  if (url === '/api/favorites/status') {
    var statusBody = JSON.parse(options.body);
    return response({items: statusBody.items.map(function (item) { return {object_type: 'media', object_id: item.object_id, result: 'found', is_favorited: favorited}; })});
  }
  if (url === '/api/favorites/batch') {
    var batch = JSON.parse(options.body); favorited = batch.action === 'favorite';
    return response({requested: batch.items.length, unique: batch.items.length, applied: batch.items.length, unchanged: 0, not_found: 0, items: batch.items.map(function (item) { return {object_type: 'media', object_id: item.object_id, result: favorited ? 'favorited' : 'unfavorited'}; })});
  }
  throw new Error('unexpected endpoint');
}
var openViewer = function () {};
var refreshViewerLabels = function () {};
global.document = document; global.window = window; global.fetch = fetch;
global.I18N = I18N; global.openViewer = openViewer; global.refreshViewerLabels = refreshViewerLabels;
require(%(favorites)s);
require(%(page)s);
function event(id, type, payload) { handlers[id + ':' + type](payload || {target: element(id)}); }
function flush() { return new Promise(function (resolve) { setTimeout(resolve, 5); }); }
(async function () {
  await flush();
  event('media-select-mode', 'click');
  event('media-select-all', 'click');
  if (element('media-selection-count').textContent !== '1 selected') throw new Error('loaded row was not selected');
  element('media-favorited-only').checked = true;
  event('media-favorited-only', 'change');
  if (element('media-selection-count').textContent !== '0 selected') throw new Error('filter change retained stale selection');
  await flush();
  var mediaCallsBeforeMutation = calls.filter(function (call) { return call.url.indexOf('/api/admin/media?') === 0; }).length;
  event('media-select-all', 'click');
  if (element('media-favorite-selected').disabled) throw new Error('permitted selected action stayed disabled');
  event('media-favorite-selected', 'click');
  await flush();
  var mediaCallsAfterMutation = calls.filter(function (call) { return call.url.indexOf('/api/admin/media?') === 0; }).length;
  if (mediaCallsAfterMutation !== mediaCallsBeforeMutation) throw new Error('favorite mutation reloaded media page');
  if (!favorited || !calls.some(function (call) { return call.url === '/api/favorites/batch'; })) throw new Error('shared favorite API was not called');
  if (element('media-favorites-link').classList.contains('hidden')) throw new Error('success did not offer favorites link');
  if (!calls.some(function (call) { return call.url.indexOf('/api/admin/media?') === 0 && new URL(call.url, 'https://unit.test').searchParams.get('favorited_only') === 'true'; })) throw new Error('favorites filter was not server-side');
  console.log('selection=cleared filter=server-side action=no-page-reload link=shown');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
""" % {"favorites": json.dumps(str(_FAVORITES_JS)), "page": json.dumps(str(_MEDIA_JS))}
    result = run_node(harness)
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    assert result.stdout.strip() == "selection=cleared filter=server-side action=no-page-reload link=shown"


def test_media_template_has_no_template_engine_syntax() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "{%" not in source
    assert "{{" not in source
