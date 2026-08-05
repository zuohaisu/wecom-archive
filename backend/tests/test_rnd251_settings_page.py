"""RND-251 settings-page navigation skeleton and password-form regression coverage."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app
from app.web import render_template

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "settings.html"
_SCRIPT = _BACKEND / "app" / "web" / "static" / "settings.js"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_GROUPS = ("general", "thirdParty", "storage", "wecom", "advanced")


def test_settings_page_requires_session_and_renders_group_navigation() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "test-tenant"
    try:
        with TestClient(app) as client:
            response = client.get("/admin/settings")
            script_response = client.get("/web/static/settings.js")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert script_response.status_code == 200
    assert '<a class="side-nav-item active" href="/admin/settings" data-i18n="nav.settings" aria-current="page"></a>' in response.text
    assert 'class="settings-nav stack gap-2"' in response.text
    assert '/web/static/settings.js?v=' in response.text
    for section in ("general", "account", "third-party", "storage", "wecom", "advanced"):
        assert f'data-settings-section="{section}"' in response.text
        assert f'id="settings-section-{section}"' in response.text


def test_settings_account_section_links_to_read_only_security_activity() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert 'href="/admin/audit-logs"' in source
    for key in (
        "settings.securityActivity.title",
        "settings.securityActivity.description",
        "settings.securityActivity.open",
        "settings.securityActivity.readOnly",
    ):
        assert f'data-i18n="{key}"' in source


def test_settings_page_keeps_the_existing_change_password_form() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert '<form id="change-password-form" class="set-ctl" novalidate>' in source
    for element_id in (
        "old-password",
        "new-password",
        "confirm-new-password",
        "change-password-message",
        "update-password-button",
    ):
        assert f'id="{element_id}"' in source
    assert "fetch('/api/admin/settings/password', {" in source
    assert "newPassword.length < 8" in source
    assert "newPassword !== confirmation" in source


def test_settings_navigation_script_switches_active_tab_and_visible_panel() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "tab.classList.toggle('active', selected);" in source
    assert "tab.setAttribute('aria-selected', String(selected));" in source
    assert "panel.hidden = panel.id !== panelId;" in source
    assert "tab.addEventListener('click'" in source


def test_settings_group_i18n_keys_exist_in_each_locale() -> None:
    source = _I18N.read_text(encoding="utf-8")

    for group in _GROUPS:
        assert source.count(f'"settings.group.{group}"') == 3
    for key in (
        "settings.securityActivity.title",
        "settings.securityActivity.description",
        "settings.securityActivity.open",
        "settings.securityActivity.readOnly",
    ):
        assert source.count(f'"{key}"') == 3


def test_settings_template_has_no_template_engine_syntax() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "{%" not in source
    assert "{{" not in source
    rendered = render_template("settings", i18n_script="", sidenav="")
    assert not re.search(r"__[A-Z0-9_]+__", rendered)
