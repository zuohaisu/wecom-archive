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


def test_navigation_has_one_configuration_with_thirteen_items_and_analytics() -> None:
    items = [item for group in NAV for item in group["items"]]
    assert len(items) == 13
    assert {item["id"] for item in items} == {
        "review",
        "search",
        "review-tasks",
        "audit-log",
        "messages",
        "analytics",
        "media",
        "exports",
        "users",
        "contacts",
        "diagnostics",
        "sync",
        "settings",
    }
    audit_log = next(item for item in items if item["id"] == "audit-log")
    assert audit_log["path"] == "/admin/audit-logs"
    analytics = next(item for item in items if item["id"] == "analytics")
    assert analytics["path"] == "/admin/analytics"


def test_registered_path_changes_users_from_disabled_to_link_without_config_change() -> None:
    disabled = render_sidenav("review", {"/admin/conversations"})
    enabled = render_sidenav("review", {"/admin/conversations", "/admin/users"})

    assert 'data-nav-id="users" aria-disabled="true"' in disabled
    assert '<a class="side-nav-item" href="/admin/users" data-i18n="nav.staffSeats"></a>' in enabled
    assert 'data-nav-id="users"' not in enabled


def test_active_registered_item_has_current_page_marker() -> None:
    html = render_sidenav("audit-log", {"/admin/audit-logs"})
    assert 'href="/admin/audit-logs" data-i18n="nav.auditLog" aria-current="page"' in html


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
