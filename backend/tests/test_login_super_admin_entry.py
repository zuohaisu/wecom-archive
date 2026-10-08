"""Haisu request: a deliberately faint super-admin entry on the login page."""
from __future__ import annotations

from pathlib import Path

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import create_app


def _no_db():
    yield MagicMock()



_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "login.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"


def test_login_page_carries_the_faint_super_admin_entry() -> None:
    app = create_app()
    app.dependency_overrides[get_db] = _no_db
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/admin/login")

    assert response.status_code == 200
    assert 'href="/platform/login"' in response.text
    assert 'data-i18n="login.superAdminEntry"' in response.text
    # 浅色入口：与合规声明同处最弱位置，样式刻意低对比。
    assert ".admin-entry{margin-left:var(--space-3);color:var(--color-text-5)" in response.text


def test_super_admin_entry_copy_exists_in_all_three_locales() -> None:
    source = _I18N.read_text(encoding="utf-8")
    for value in ("超管入口", "Super-admin entry"):
        assert f'"login.superAdminEntry": "{value}"' in source, value
    assert source.count('"login.superAdminEntry"') == 3
