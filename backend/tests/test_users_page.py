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

    assert 'class="toolbar toolbar-compact"' in source
    assert "fetch('/api/admin/users?'" in source
    for field in ("user.role", "user.status", "user.last_active_at", "user.msg_count_30d"):
        assert field in source
    assert "mock" not in source.lower()


def test_users_page_operations_call_existing_user_management_endpoints() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "'/api/admin/users/invite'" in source
    assert "method:'PATCH'" in source
    assert "'/role'" in source
    assert 'data-action="role"' in source
    assert "'/reset-password'" in source
    assert "method:'POST'" in source
    assert "fetch('/api/auth/me'" in source
    assert "function canManage(user)" in source
    assert "user.id!==state.currentUserId" in source
    assert "state.currentRole==='owner'" in source
    assert "function setRoleChoices()" in source
    assert '#invite-role option[value="owner"],#role-select option[value="owner"]' in source


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
    # RND-321: the access-request review keys must ride along with the rest
    # of this ticket's users.* keys, not drift into a separate, unbalanced
    # locale set.
    assert {k for k in key_sets[0] if k.startswith("users.accessRequests.")}


# ---------------------------------------------------------------------------
# RND-321 — access-request review section
# ---------------------------------------------------------------------------


def test_access_requests_section_exists_hidden_by_default_and_separate_from_users_table() -> None:
    """AC-5: structurally distinct from the users table, and hidden until
    JS confirms the caller is owner/admin — never shown-then-hidden after
    a flash of content."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert 'id="access-requests-section"' in source
    assert 'class="card mb-5 hidden" id="access-requests-section"' in source
    assert 'id="access-requests-body"' in source
    # The section's own table, not a reuse of the users table's tbody.
    assert source.index('id="access-requests-body"') < source.index('id="users-body"')


def test_access_requests_section_toggled_by_the_same_role_check_as_invite_button() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "document.getElementById('access-requests-section').classList.toggle('hidden',!state.canManageUsers)" in source


def test_access_requests_fetch_the_real_review_endpoints() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "fetch('/api/admin/access-requests'" in source
    assert "/api/admin/access-requests/'+encodeURIComponent(id)+'/link'" in source
    assert "/api/admin/access-requests/'+encodeURIComponent(id)+'/create-account'" in source
    assert "admin_user_id:accountId" in source
    assert "mock" not in source.lower()


def test_access_request_modals_exist_and_never_default_to_owner_role() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert 'id="link-request-modal"' in source
    assert 'id="create-request-modal"' in source
    assert 'id="link-request-account"' in source
    # AC-4: the reviewer explicitly picks a role; there is no default that
    # could silently grant more than intended, and the owner option is
    # gated by the same setRoleChoices() restriction as invite/role-change.
    assert "document.getElementById('create-request-role').value='admin'" in source
    assert '#create-request-role option[value="owner"]' in source


def test_access_request_row_renders_suspected_match_as_a_hint_only() -> None:
    """AC-3: the suspected-match hint must render as inert text, never as
    something that pre-selects or auto-submits a link action."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "request.suspected_match" in source
    assert "users.accessRequests.suspectedMatch" in source
    # No auto-selection: the account <select> always starts on the
    # placeholder option, never pre-set to the suspected match's id.
    assert "select.value=" not in source


def test_access_request_row_renders_a_distinct_warning_when_multiple_accounts_match() -> None:
    """RND-321 QA-repro: two accounts sharing an email must render a
    distinct "can't determine a single match" warning, never one of them
    picked arbitrarily and shown as if it were unambiguous."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "suspected_match_status" in source
    assert "users.accessRequests.suspectedMatchMultiple" in source
    assert "==='multiple'" in source.replace(" ", "")


def test_access_request_legacy_identity_conflict_prompts_for_explicit_release_and_retries() -> None:
    """RND-321 QA-repro follow-up: a legacy-identity collision must never
    be silently retried — the reviewer sees a distinct confirmation naming
    the conflicting account, and only an explicit confirm retries the same
    action with the opt-in release flag. No auto-release, no silent
    fallback to the generic error path."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "legacy_identity_conflict" in source
    assert "release_conflicting_legacy_account" in source
    assert "users.accessRequests.legacyConflictConfirm" in source
    assert "users.accessRequests.legacyConflictCancelled" in source
    # The retry reuses the same request body plus the release flag, rather
    # than a bespoke second endpoint or payload shape.
    assert "release_conflicting_legacy_account:true" in source.replace(" ", "")
    assert "mock" not in source.lower()
