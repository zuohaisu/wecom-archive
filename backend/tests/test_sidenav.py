from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app
from app.web.sidenav import NAV, render_sidenav


_BACKEND = Path(__file__).resolve().parent.parent
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "review_console.html"
_DESIGN_SOURCE = (
    _BACKEND.parent / "design" / "Crowntime WeCom Archive Design System" / "styles.css"
)
_DESIGN_TARGET = _BACKEND / "app" / "web" / "static" / "design-system.css"


def test_design_system_is_a_new_copy_of_the_design_source() -> None:
    assert _DESIGN_TARGET.read_bytes() == _DESIGN_SOURCE.read_bytes()


def test_navigation_has_no_top_level_audit_log_and_keeps_other_items() -> None:
    items = [item for group in NAV for item in group["items"]]
    assert len(items) == 11
    assert {item["id"] for item in items} == {
        "review",
        "search",
        "review-tasks",
        "messages",
        "analytics",
        "media",
        "exports",
        "users",
        "contacts",
        "diagnostics",
        "settings",
    }
    assert "audit-log" not in {item["id"] for item in items}
    assert "/admin/audit-logs" not in {item["path"] for item in items}
    analytics = next(item for item in items if item["id"] == "analytics")
    assert analytics["path"] == "/admin/analytics"
    assert "sync" not in {item["id"] for item in items}


def test_registered_path_changes_users_from_disabled_to_link_without_config_change() -> None:
    disabled = render_sidenav("review", {"/admin/conversations"})
    enabled = render_sidenav("review", {"/admin/conversations", "/admin/users"})

    assert 'data-nav-id="users" aria-disabled="true"' in disabled
    assert '<a class="side-nav-item" href="/admin/users" data-i18n="nav.staffSeats"></a>' in enabled
    assert 'data-nav-id="users"' not in enabled


def test_settings_can_be_the_active_navigation_item_for_security_activity() -> None:
    html = render_sidenav("settings", {"/admin/settings", "/admin/audit-logs"})
    assert 'href="/admin/settings" data-i18n="nav.settings" aria-current="page"' in html
    assert "/admin/audit-logs" not in html


def test_production_conversations_route_renders_the_sidenav_without_tokens() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "test-tenant"
    try:
        response = TestClient(app).get("/admin/conversations")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert '<nav class="side-nav">' in response.text
    assert not re.search(r"__[A-Z0-9_]+__", response.text)


def test_template_uses_sidenav_token_and_i18n_has_audit_and_page_anchors() -> None:
    template = _TEMPLATE.read_text(encoding="utf-8")
    assert "__SIDENAV__" in template
    assert '<nav class="side-nav">' not in template

    source = _I18N.read_text(encoding="utf-8")
    assert source.count('"nav.auditLog":') == 3
    for ticket in ("RND-327", "RND-328", "RND-329", "RND-330"):
        assert source.count(f"/* {ticket} ") == 3
