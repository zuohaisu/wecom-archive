"""Contracts for the internal-staff directory page (Haisu split request)."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "staff.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"


def _authenticated_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    return TestClient(app, raise_server_exceptions=False)


def test_staff_page_is_authenticated_ssr_without_unresolved_tokens() -> None:
    client = _authenticated_client()
    try:
        response = client.get("/admin/staff", follow_redirects=False)
    finally:
        client.app.dependency_overrides.clear()
        client.close()

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert not re.search(r"__[A-Z0-9_]+__", response.text)


def test_staff_page_uses_the_directory_group_sidenav_link() -> None:
    client = _authenticated_client()
    try:
        response = client.get("/admin/staff")
    finally:
        client.app.dependency_overrides.clear()
        client.close()

    assert (
        'href="/admin/staff" data-i18n="nav.staffDirectory" aria-current="page"'
        in response.text
    )
    # 并列外部联系人：两者同属通讯录组。
    assert 'href="/admin/contacts" data-i18n="nav.externalContacts"' in response.text


def test_staff_page_is_read_only_and_fetches_the_real_directory_api() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "fetch('/api/admin/staff?'" in source
    # 只读目录 + RND-374 相关客户下钻（GET only）：仍无任何管理端点或操作按钮。
    assert "method:'PATCH'" not in source
    assert "'/api/admin/users" not in source
    assert "data-action=" not in source
    assert "fetch('/api/admin/staff/'" in source  # RND-374: 按员工下钻相关客户
    assert source.count("fetch(") == 3  # 目录列表、相关客户、登出
    for field in ("staff.messages30", "staff.messagesTotal", "msg_count_30d"):
        assert field in source
    assert "mock" not in source.lower()


def test_staff_i18n_keys_are_complete_in_all_three_locales() -> None:
    source = _I18N.read_text(encoding="utf-8")

    title_values = ('"staff.pageTitle": "内部员工"', '"staff.pageTitle": "內部員工"', '"staff.pageTitle": "Internal staff"')
    for value in title_values:
        assert source.count(value) == 1, value
    key_lines = [line for line in source.splitlines() if '"staff.pageTitle"' in line]
    assert len(key_lines) == 3
    key_sets = [set(re.findall(r'"(staff\.[^"]+)"\s*:', line)) for line in key_lines]
    assert key_sets[0] == key_sets[1] == key_sets[2]
    expected = {
        "staff.pageTitle",
        "staff.description",
        "staff.searchPlaceholder",
        "staff.member",
        "staff.department",
        "staff.seat",
        "staff.seatYes",
        "staff.seatNo",
        "staff.messages30",
        "staff.messagesTotal",
        "staff.count",
        "staff.loading",
        "staff.empty",
        "staff.unnamed",
        "staff.requestFailed",
        "staff.customersSearch",
        "staff.customerName",
        "staff.customerMessages",
        "staff.customerLast",
        "staff.customerFirst",
    }
    assert key_sets[0] == expected


def test_users_page_retirement_keys_removed_from_all_locales() -> None:
    """The seats rename must not leave stale keys behind in any locale."""
    source = _I18N.read_text(encoding="utf-8")

    assert source.count('"nav.staffSeats"') == 0
    assert source.count('"users.breadcrumbDirectory"') == 0
    assert source.count('"users.messages30"') == 0
    assert source.count('"nav.users":') == 3
    assert source.count('"nav.users": "User Management"') == 1
    assert source.count('"nav.staffDirectory":') == 3
