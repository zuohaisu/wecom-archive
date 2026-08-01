"""RND-253 Settings-page interaction wiring regression coverage."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "settings.html"
_SCRIPT = _BACKEND / "app" / "web" / "static" / "settings.js"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_CONFIG_GROUPS = ("general", "third_party", "storage", "wecom", "advanced")
_INTERACTION_KEYS = (
    "settings.source.default",
    "settings.source.env",
    "settings.source.db",
    "settings.save",
    "settings.restartRequired",
    "settings.secret.showDetails",
    "settings.secret.hideDetails",
    "settings.secret.copy",
    "settings.testConnection",
    "settings.connectionOk",
    "settings.connectionFailed",
)


def test_settings_page_serves_t8_containers_and_rnd253_script() -> None:
    """The authenticated page exposes the fixed T8 containers consumed by the script."""
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "rnd253-tenant"
    try:
        with TestClient(app) as client:
            response = client.get("/admin/settings")
            script_response = client.get("/web/static/settings.js")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert script_response.status_code == 200
    for section in ("general", "third-party", "storage", "wecom", "advanced"):
        assert f'id="settings-section-{section}"' in response.text
    assert "initSettingsConfiguration" in script_response.text


def test_settings_script_fetches_and_renders_only_config_registry_groups() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "fetch('/api/admin/settings', {credentials: 'include'})" in source
    assert "#settings-section-' + sectionName + ' .card-bd" in source
    assert "CONFIG_GROUPS = {" in source
    for group in _CONFIG_GROUPS:
        assert f"{group}:" in source
    assert "account:" not in source
    assert "data-settings-config-form" in source


def test_untouched_secret_mask_is_never_sent_as_an_update() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "input.dataset.secretDirty = 'false';" in source
    assert "input.dataset.secretDirty = 'true';" in source
    assert "input.hasAttribute('data-secret-field') && input.dataset.secretDirty !== 'true'" in source
    assert "Only a user input event" in source
    assert "body: JSON.stringify({updates: collected.updates})" in source


def test_settings_script_wires_source_restart_errors_and_connection_results() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "function SourceBadge(source)" in source
    assert "data-settings-source" in source
    assert "function RestartBanner(keys)" in source
    assert "restart_required_keys" in source
    assert "data-settings-error" in source
    assert "result.response.status === 400 && result.body.errors" in source
    assert "fetch('/api/admin/settings/test-connection', {" in source
    assert "data-settings-test-result" in source


def test_rnd253_i18n_keys_are_present_for_all_locales() -> None:
    source = _I18N.read_text(encoding="utf-8")

    for key in _INTERACTION_KEYS:
        assert source.count(f'"{key}"') == 3


def test_settings_template_remains_static_template_engine_free() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "{%" not in source
    assert "{{" not in source
