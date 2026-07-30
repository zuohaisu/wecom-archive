"""RND-330 contracts for the SSR external contacts list page."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app
from app.web import render_template

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "contacts.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_CONTACT_KEYS = (
    "contacts.pageTitle", "contacts.breadcrumbDirectory", "contacts.description",
    "contacts.companyPlaceholder", "contacts.tagPlaceholder", "contacts.ownerPlaceholder",
    "contacts.applyFilters", "contacts.clearFilters", "contacts.contact", "contacts.company",
    "contacts.tags", "contacts.owner", "contacts.messages", "contacts.lastInteraction",
    "contacts.loading", "contacts.empty", "contacts.results", "contacts.unnamed",
    "contacts.loadFailed",
)


def _authenticated_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


def test_contacts_page_requires_session() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: None
    client = TestClient(app)
    try:
        response = client.get("/admin/contacts", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
        client.close()
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_contacts_page_renders_without_tokens_and_enables_active_navigation() -> None:
    client = _authenticated_client()
    try:
        response = client.get("/admin/contacts")
    finally:
        client.app.dependency_overrides.clear()
        client.close()
    assert response.status_code == 200
    assert not re.search(r"__[A-Z0-9_]+__", response.text)
    assert '<a class="side-nav-item active" href="/admin/contacts" data-i18n="nav.externalContacts" aria-current="page"></a>' in response.text


def test_contacts_page_uses_external_contact_api_fields_and_server_filters() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "'/api/admin/external-contacts?'+params.toString()" in source
    for parameter in ("company", "tags", "owner_wecom_userid", "offset", "limit"):
        assert parameter in source
    for field in (
        "item.name", "item.company", "item.tags", "item.owner_display_name",
        "item.external_userid", "item.message_count", "item.last_interaction_at",
    ):
        assert field in source
    assert "/api/contacts" not in source
    assert "/api/search/contacts" not in source
    assert "mock" not in source.lower()


def test_contacts_page_has_no_unimplemented_detail_link_or_template_syntax() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r'href=["\'][^"\']*/admin/contacts/', source)
    assert "contact-detail" not in source
    assert "{%" not in source
    assert "{{" not in source
    rendered = render_template("contacts", i18n_script="", sidenav="")
    assert not re.search(r"__[A-Z0-9_]+__", rendered)


def test_contacts_i18n_keys_exist_in_each_locale_directly_under_own_anchor() -> None:
    source = _I18N.read_text(encoding="utf-8")
    blocks = re.findall(
        r"/\* RND-330 contacts page keys — insert below \*/(.*?)(?:\n\s*\"console.auditMode\")",
        source,
        re.S,
    )
    assert len(blocks) == 3
    for block in blocks:
        for key in _CONTACT_KEYS:
            assert f'"{key}"' in block
