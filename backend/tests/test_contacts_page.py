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
    # RND-341
    "contacts.searchPlaceholder", "contacts.actions", "contacts.viewDetail",
    "contacts.detailTitle", "contacts.detailLoading", "contacts.detailLoadFailed",
    "contacts.remarkName", "contacts.currentNickname", "contacts.nicknameHistory",
    "contacts.noNicknameHistory", "contacts.source", "contacts.relatedConversations", "contacts.noConversations",
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
    for parameter in ("q", "company", "tags", "owner_wecom_userid", "offset", "limit"):
        assert parameter in source
    for field in (
        "item.display_name", "item.current_nickname", "item.follow_remarks",
        "item.company", "item.tags", "item.source", "item.owner_display_name",
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


def test_contacts_page_supports_identity_search() -> None:
    """RND-341: one server-side query searches RND-170's employee remarks,
    current nickname, and nickname history rather than filtering one fetched page."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert 'id="q-filter"' in source
    assert "params.set('q',q)" in source


def test_contacts_page_uses_compact_filters_and_a_real_tag_select() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert 'class="toolbar toolbar-compact"' in source
    assert '<select class="select" id="tag-filter"' in source
    assert "renderTagOptions(data.available_tags)" in source
    assert 'id="tag-filter" type="search"' not in source


def test_contacts_page_has_detail_drawer_with_rnd170_identity_fields() -> None:
    """The drawer consumes the merged RND-170 contract: employee-scoped
    remarks, current nickname, and observed nickname history stay separate."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert 'id="contact-drawer"' in source
    assert "contacts.viewDetail" in source
    assert "/api/admin/external-contacts/'+encodeURIComponent(externalUserid)" in source
    assert "contacts.remarkName" in source
    assert "contacts.currentNickname" in source
    assert "item.follow_remarks" in source
    assert "item.current_nickname" in source
    assert "item.nickname_history" in source
    assert "detailField(t('contacts.remarkName'),item.name)" not in source


def test_contacts_page_renders_history_in_reverse_observation_order() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "contacts.nicknameHistory" in source
    assert "contacts.noNicknameHistory" in source
    assert "(item.nickname_history||[]).slice().reverse()" in source
    assert "nicknameHistoryPending" not in source


def test_contacts_page_related_conversations_reuse_existing_console_endpoints() -> None:
    """RND-341: clicking a related conversation must reuse the existing
    console (same conversation detail/message endpoints via the RND-229
    focus-from-URL mechanism), not a new merged timeline view."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "/admin/conversations?conv=" in source
    assert "entityType=contact" in source
    assert "conv.conversation_id" in source
    assert "conv.conversation_type" in source


def test_contacts_i18n_keys_exist_in_each_locale_directly_under_own_anchor() -> None:
    source = _I18N.read_text(encoding="utf-8")
    blocks = re.findall(
        r"/\* RND-330 contacts page keys — insert below \*/(.*?)(?:\n\s*\"console.auditMode\")",
        source,
        re.DOTALL,
    )
    assert len(blocks) == 3
    for block in blocks:
        for key in _CONTACT_KEYS:
            assert f'"{key}"' in block
