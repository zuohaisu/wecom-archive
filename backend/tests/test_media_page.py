"""RND-329 contracts for the SSR admin media page."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "media.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_MEDIA_KEYS = (
    "media.pageTitle", "media.breadcrumbData", "media.description",
    "media.searchPlaceholder", "media.allTypes", "media.type.image",
    "media.type.file", "media.type.voice", "media.type.video",
    "media.allTime", "media.days7", "media.days30", "media.days90",
    "media.sortNewest", "media.sortOldest", "media.sortLargest",
    "media.preview", "media.unnamed", "media.empty", "media.results",
    "media.loadMore", "media.loadFailed",
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


def test_media_page_fetches_real_api_and_passes_type_and_time_filters_to_it() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "fetch('/api/admin/media?'+params().toString()" in source
    for parameter in ("file_type", "days", "q", "sort", "offset", "limit"):
        assert parameter in source
    for field in ("item.file_type", "item.file_size", "item.created_at", "item.session_title"):
        assert field in source
    assert "mock" not in source.lower()


def test_media_thumbnails_use_conversation_message_media_route_without_provider_urls() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "item.conversation_id" in source
    assert "item.msgid" in source
    assert "'/api/conversations/'+encodeURIComponent(item.conversation_id)+'/messages/'+encodeURIComponent(item.msgid)+'/media?variant=thumb'" in source
    assert not re.search(r"qiniu|QINIU_|qiniu\.com", source, re.IGNORECASE)


def test_media_page_reuses_the_conversation_review_overlay_for_every_media_kind() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    viewer = (_BACKEND / "app" / "web" / "static" / "console" / "media-viewer.js").read_text(encoding="utf-8")
    assert 'class="toolbar toolbar-compact"' in source
    assert "/web/static/console/media-viewer.js" in source
    assert "openViewer(state.items.map(viewerItem),index)" in source
    assert "/media/access" in source
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


def test_media_template_has_no_template_engine_syntax() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "{%" not in source
    assert "{{" not in source
