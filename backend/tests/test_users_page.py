"""RND-327 contracts for the SSR admin users page."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "users.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"


def _authenticated_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


def test_users_page_is_authenticated_ssr_without_unresolved_tokens() -> None:
    client = _authenticated_client()
    try:
        response = client.get("/admin/users", follow_redirects=False)
    finally:
        client.app.dependency_overrides.clear()
        client.close()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert not re.search(r"__[A-Z0-9_]+__", response.text)


def test_users_page_uses_registered_active_users_sidenav_link() -> None:
    client = _authenticated_client()
    try:
        response = client.get("/admin/users")
    finally:
        client.app.dependency_overrides.clear()
        client.close()

    assert 'href="/admin/users" data-i18n="nav.staffSeats" aria-current="page"' in response.text
    assert 'data-nav-id="users"' not in response.text


def test_users_page_fetches_real_api_and_exposes_all_user_fields() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "fetch('/api/admin/users?'" in source
    for field in ("user.role", "user.status", "user.last_active_at", "user.msg_count_30d"):
        assert field in source
    assert "mock" not in source.lower()


def test_users_page_operations_call_existing_user_management_endpoints() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "'/api/admin/users/invite'" in source
    assert "method:'PATCH'" in source
    assert "'/reset-password'" in source
    assert "method:'POST'" in source


def test_users_i18n_keys_are_complete_in_all_three_locales_and_at_own_anchor() -> None:
    source = _I18N.read_text(encoding="utf-8")
    anchors = list(re.finditer(r"/\* RND-327 users page keys — insert below \*/", source))
    assert len(anchors) == 3

    key_sets = []
    for anchor in anchors:
        following = source[anchor.end() : source.find("/* RND-328", anchor.end())]
        keys = set(re.findall(r'"(users\.[^"]+)"\s*:', following))
        assert keys
        key_sets.append(keys)
    assert key_sets[0] == key_sets[1] == key_sets[2]
