"""RND-328 audit-log page shell and client-side API contract coverage."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app
from app.web import render_template

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "audit_log.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"
_AUDIT_KEYS = (
    "audit.title", "audit.description", "audit.readOnly", "audit.operatorPlaceholder",
    "audit.actionPlaceholder", "audit.from", "audit.to", "audit.applyFilters",
    "audit.clearFilters", "audit.time", "audit.operator", "audit.action", "audit.event",
    "audit.auditId", "audit.loading", "audit.empty", "audit.failedToLoad", "audit.system",
    "audit.results", "audit.page",
)


def test_audit_page_requires_session() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: None
    try:
        response = TestClient(app).get("/admin/audit-logs", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_audit_page_renders_without_tokens_and_enables_active_navigation() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "test-tenant"
    try:
        response = TestClient(app).get("/admin/audit-logs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not re.search(r"__[A-Z0-9_]+__", response.text)
    assert '<a class="side-nav-item active" href="/admin/audit-logs" data-i18n="nav.auditLog" aria-current="page"></a>' in response.text


def test_audit_page_uses_paginated_read_only_api_with_all_filters() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "fetch('/api/admin/audit-logs?'+params().toString())" in source
    for parameter in ("limit", "offset", "q", "action", "from", "to"):
        assert f"p.set('{parameter}'" in source or f"{parameter}:String" in source
    assert "data.total" in source
    assert "data.limit" in source
    assert "data.offset" in source
    assert "previous-page" in source
    assert "next-page" in source


def test_audit_page_has_no_audit_write_operation_or_write_controls() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8").lower()
    assert not re.search(r"/api/admin/audit-logs[^'\"]*['\"],\s*\{[^}]*method", source)
    for forbidden in ("edit audit", "delete audit", "update audit", "export audit"):
        assert forbidden not in source
    assert "append-only" in source


def test_audit_page_renders_api_fields_without_html_injection() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    for field in ("item.actor_name", "item.created_at", "item.action", "item.object_type"):
        assert field in source
    assert "cell.textContent=value" in source


def test_audit_i18n_keys_exist_in_each_locale_directly_under_anchor() -> None:
    source = _I18N.read_text(encoding="utf-8")
    blocks = re.findall(
        r'/(?:\* RND-328 audit-log page keys — insert below \*/)(.*?)/\* RND-329',
        source,
        re.S,
    )
    assert len(blocks) == 3
    for block in blocks:
        for key in _AUDIT_KEYS:
            assert f'"{key}"' in block


def test_audit_template_has_no_template_engine_syntax() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "{%" not in source
    assert "{{" not in source
    rendered = render_template("audit_log", i18n_script="", sidenav="")
    assert not re.search(r"__[A-Z0-9_]+__", rendered)
