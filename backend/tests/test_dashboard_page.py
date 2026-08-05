"""Dashboard page and legacy usage-route compatibility coverage for RND-344."""
from __future__ import annotations

import re
from unittest.mock import MagicMock
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.db.session import get_db
from app.main import create_app


_DASHBOARD_JS = Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "dashboard.js"


def test_dashboard_renders_the_unified_overview_shell() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    try:
        response = TestClient(app).get("/dashboard")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "会话存档总览" in response.text
    assert "/web/static/dashboard.js" in response.text
    assert "/web/static/dashboard.css" in response.text
    assert 'href="/dashboard"' in response.text
    assert 'href="/admin/analytics"' not in response.text
    assert not re.search(r"__[A-Z0-9_]+__", response.text)


def test_dashboard_initialises_static_i18n_for_the_shared_sidenav() -> None:
    source = _DASHBOARD_JS.read_text(encoding="utf-8")

    assert "function applyStaticI18n()" in source
    assert "document.querySelectorAll('[data-i18n]')" in source
    assert "applyStaticI18n();load();" in source


def test_legacy_usage_page_redirects_to_dashboard_insights() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    try:
        response = TestClient(app).get("/admin/analytics", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard#data-insights"


def test_dashboard_requires_the_existing_html_session() -> None:
    app = create_app()

    def no_database():
        yield MagicMock()

    app.dependency_overrides[get_db] = no_database
    try:
        response = TestClient(app).get("/dashboard", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"
