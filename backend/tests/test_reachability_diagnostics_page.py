"""RND-338 archive-health page shell, auth, and localization contracts."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest

from tests._rnd216_web_shims import diagnostics_html, diagnostics_js_source

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "diagnostics.html"
_JS = diagnostics_js_source()
_HTML = diagnostics_html()


def _mock_db_no_session() -> Generator:
    mock = MagicMock()
    mock.query.return_value.filter.return_value.first.return_value = None
    yield mock


def _db_with_session(tenant_id: str):
    def _override():
        mock = MagicMock()
        session_mock = MagicMock()
        session_mock.tenant_id = tenant_id
        query = MagicMock()
        query.filter.return_value = query
        query.first.return_value = session_mock
        mock.query.return_value = query
        yield mock

    return _override


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


def test_archive_health_url_keeps_its_session_gate(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_no_session
    try:
        response = client.get("/admin/diagnostics/reachability", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/login"


def test_archive_health_url_and_sidenav_render_when_authenticated(client) -> None:
    from app.auth import require_html_session
    from app.main import app

    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    try:
        response = client.get("/admin/diagnostics/reachability")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert 'id="diag-root" aria-live="polite"' in response.text
    assert 'data-i18n="nav.diagnostics"' in response.text
    assert "__SIDENAV__" not in response.text


def test_page_is_archive_health_not_a_client_side_audit_or_rate() -> None:
    assert "/api/admin/reachability-checks/latest" in _JS
    assert "/api/admin/reachability-checks" in _JS
    assert "/api/admin/reachability-audit" not in _JS
    assert "reachabilityRate" not in _JS
    assert "pct(" not in _JS
    assert "innerHTML" not in _JS


def test_template_has_no_template_engine_syntax_and_uses_accessible_native_controls() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")
    assert "{%" not in source
    assert "{{" not in source
    assert 'aria-live="polite"' in source
    assert 'aria-busy="true"' in source
    assert "innerHTML" not in source


_REQUIRED_KEYS = (
    "nav.diagnostics",
    "diagnostics.pageTitle",
    "diagnostics.pageDescription",
    "diagnostics.loading",
    "diagnostics.checkNow",
    "diagnostics.checkAgain",
    "diagnostics.startRequestFailed",
    "diagnostics.checking",
    "diagnostics.state.healthy.title",
    "diagnostics.state.attention.title",
    "diagnostics.state.checking.title",
    "diagnostics.state.noData.title",
    "diagnostics.state.incomplete.title",
    "diagnostics.state.error.title",
    "diagnostics.authFailed",
    "diagnostics.forbidden",
    "diagnostics.technicalDetails",
    "diagnostics.reason.missingRecipient",
    "diagnostics.reason.unknown",
)


def test_archive_health_i18n_keys_exist_in_each_locale() -> None:
    source = (_BACKEND / "app" / "assets" / "i18n.js").read_text(encoding="utf-8")
    blocks = re.findall(
        r'"(?:zh-CN|zh-TW)":\s*\{.*?translations:\s*\{(.*?)\n\s*\}\s*\n\s*\},?',
        source,
        re.S,
    )
    english = re.search(r"en:\s*\{.*?translations:\s*\{(.*?)\n\s*\}\s*\n\s*\}\s*\n\s*};", source, re.S)
    assert english is not None
    blocks.append(english.group(1))
    assert len(blocks) == 3
    for block in blocks:
        for key in _REQUIRED_KEYS:
            assert f'"{key}"' in block


@pytest.mark.parametrize(
    ("locale", "title"),
    (("zh-CN", "归档健康"), ("zh-TW", "歸檔健康"), ("en", "Archive health")),
)
def test_archive_health_title_is_localized(locale: str, title: str) -> None:
    assert f'"diagnostics.pageTitle": "{title}"' in _JS
