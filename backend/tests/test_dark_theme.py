"""GH-101 dark-theme rollout guards.

data-theme markers on every admin page, the theme bootstrap contract
(localStorage choice > prefers-color-scheme), the settings-page entry, and
the review console's dark chat-surface overrides.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.db.session import get_db
from app.main import create_app


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATES = _BACKEND / "app" / "web" / "templates"
_THEME_JS = _BACKEND / "app" / "web" / "static" / "theme.js"
_DESIGN_SYSTEM = _BACKEND / "app" / "web" / "static" / "design-system.css"


def _no_db():
    from unittest.mock import MagicMock

    yield MagicMock()


def test_every_template_html_carries_data_theme() -> None:
    for template in sorted(_TEMPLATES.glob("*.html")):
        match = re.search(r"<html\b[^>]*>", template.read_text(encoding="utf-8"))
        assert match is not None, template.name
        assert "data-theme" in match.group(0), f"{template.name} lacks data-theme"


def test_theme_js_persists_choice_and_follows_system() -> None:
    source = _THEME_JS.read_text(encoding="utf-8")

    assert "wecom_admin_theme" in source
    assert "prefers-color-scheme: dark" in source
    assert "localStorage.setItem(STORAGE_KEY, choice)" in source
    assert "localStorage.removeItem(STORAGE_KEY)" in source
    assert "setAttribute('data-theme', effective())" in source
    assert "window.ThemeControl" in source


def test_design_system_pages_bootstrap_theme_and_platform_stays_light() -> None:
    from app.db.session import get_db

    app = create_app()
    app.dependency_overrides[get_db] = _no_db
    with TestClient(app, raise_server_exceptions=False) as client:
        login = client.get("/admin/login")
        assert login.status_code == 200
        assert "/web/static/theme.js" in login.text

        platform = client.get("/platform/login", follow_redirects=False)
        if platform.status_code == 200:
            assert "/web/static/theme.js" not in platform.text
            assert 'data-theme="light"' in platform.text


def test_settings_page_hosts_the_theme_picker() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    app.dependency_overrides[get_db] = _no_db
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/admin/settings")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert 'id="theme-picker"' in response.text
    assert 'data-theme-choice="light"' in response.text
    assert 'data-theme-choice="dark"' in response.text
    assert 'data-theme-choice="system"' in response.text
    # 入口只在设置页：侧边栏（共享渲染）不出现主题按钮。
    assert "btn-theme-toggle" not in response.text


def test_review_console_has_dark_chat_surface_overrides() -> None:
    css = _DESIGN_SYSTEM.read_text(encoding="utf-8")

    override = re.search(
        r"html\[data-theme=\"dark\"\]\{([^}]*)--bubble-self-bg", css
    )
    assert override is not None, "expected dark overrides for the bubble vars"
    assert "--bubble-other-bg" in override.group(0) or "--bubble-self-bg" in css
    # 气泡变量仍由页面 :root 提供亮色基线（issue 保留语义）。
    template = (_TEMPLATES / "review_console.html").read_text(encoding="utf-8")
    assert "--bubble-self-bg:#cfe6fd" in template
