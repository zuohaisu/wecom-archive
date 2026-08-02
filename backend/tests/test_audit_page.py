"""RND-336 Security & activity page shell and client-side contract coverage."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.audit import ACTION_CATALOG
from app.auth import require_html_session
from app.main import create_app
from app.web import render_template

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "audit_log.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"


def _audit_blocks() -> list[str]:
    return re.findall(r"/\* RND-328 audit-log page keys — insert below \*/(.*?)/\* RND-329", _I18N.read_text(encoding="utf-8"), re.S)


def test_audit_page_requires_session() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: None
    try:
        response = TestClient(app).get("/admin/audit-logs", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_audit_page_keeps_legacy_url_and_marks_settings_active() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "test-tenant"
    try:
        response = TestClient(app).get("/admin/audit-logs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not re.search(r"__[A-Z0-9_]+__", response.text)
    assert '<a class="side-nav-item active" href="/admin/settings" data-i18n="nav.settings" aria-current="page"></a>' in response.text
    assert 'data-i18n="nav.settings"' in response.text
    assert 'data-i18n="audit.title"' in response.text


def test_audit_page_uses_server_filters_with_ninety_day_human_default() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert 'id="range-filter"' in source
    assert 'value="90" selected' in source
    assert "Date.now()-Number(range)*86400000" in source
    assert "p.set('from',from.toISOString())" in source
    assert "p.set('include_system',system?'true':'false')" in source
    assert "p.set('category',category)" in source
    assert "p.set('operator',operator)" in source
    assert "/api/admin/users?per_page=100" in source
    assert "operator=system" not in source
    assert "offset=0;load()" in source
    assert "hasMore=data.has_more" in source
    assert "document.getElementById('next-page').disabled=!hasMore" in source


def test_audit_page_maps_every_backend_catalog_action_and_has_unknown_fallback() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    mappings = set(re.findall(r"'([^']+)':'audit\.action\.[^']+'", source))

    assert mappings == set(ACTION_CATALOG)
    assert "'audit.action.unknown'" in source
    blocks = _audit_blocks()
    assert len(blocks) == 3
    for block in blocks:
        for action in ACTION_CATALOG:
            assert f'"audit.action.{action}"' in block
        for key in ("audit.action.unknown", "audit.details", "audit.operatorFallback"):
            assert f'"{key}"' in block


def test_audit_page_only_renders_allowlisted_detail_values_with_safe_dom_apis() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "Object.keys(detail)" not in source
    assert ".innerHTML" not in source
    assert "textContent=value" in source
    for allowed in (
        "detail.format", "detail.record_count", "detail.reason", "detail.changed_keys",
        "detail[name]", "detail.locked_count",
    ):
        assert allowed in source
    for forbidden in (
        "detail.message", "detail.content", "detail.token", "detail.secret", "detail.signed_url",
        "detail.storage_key", "detail.path", "detail.search", "detail.html",
    ):
        assert forbidden not in source


def test_audit_page_has_localized_accessible_read_only_states_without_write_controls() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    for control_id in ("range-filter", "category-filter", "operator-filter", "include-system"):
        assert f'id="{control_id}"' in source
    assert 'role="status" aria-live="polite"' in source
    assert 'role="list" aria-live="polite"' in source
    assert "audit.unauthorized" in source
    assert "audit.forbidden" in source
    assert "audit.empty" in source
    assert "audit.loading" in source
    assert "audit.failedToLoad" in source
    assert "fetch('/api/admin/audit-logs?'+params().toString(),{credentials:'include'})" in source
    assert not re.search(r"/api/admin/audit-logs[^'\"]*['\"],\s*\{[^}]*method", source)


def test_audit_i18n_and_settings_entry_exist_in_each_locale() -> None:
    blocks = _audit_blocks()
    assert len(blocks) == 3
    for block in blocks:
        for key in (
            "audit.title", "audit.description", "audit.readOnly", "audit.range90",
            "audit.categoryDataAccess", "audit.includeSystem", "audit.action.unknown",
            "audit.technical.action", "settings.securityActivity.title",
            "settings.securityActivity.open", "settings.securityActivity.readOnly",
        ):
            assert f'"{key}"' in block


def test_audit_template_has_no_template_engine_syntax() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "{%" not in source
    assert "{{" not in source
    rendered = render_template("audit_log", i18n_script="", sidenav="")
    assert not re.search(r"__[A-Z0-9_]+__", rendered)
