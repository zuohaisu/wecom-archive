"""
Tests for RND-180 — System Diagnostics Page: Message Reachability Statistics.

Scope: this is a thin diagnostics UI over the existing RND-178 admin audit
endpoint (GET /api/admin/reachability-audit) — no new backend classification
logic, no new aggregate computation on the server. These tests therefore
cover only what's new:
  - /admin/diagnostics/reachability requires a valid admin session, same as
    every other protected HTML admin route (redirect to /admin/login).
  - The rendered page shell never embeds message content, raw payloads, or
    WeCom/media identifiers — it has no server-side access to messages at
    all, but this is asserted directly rather than assumed.
  - The page fetches from the real, unmodified RND-178 endpoint URL (so it
    can't have silently started duplicating audit logic client-side).
  - The review console gained a nav entry pointing at the new page.
  - No per-message sample fields or a message list/detail drawer are wired
    into the page.

Auth/tenant-scoping/aggregate-only/pagination-semantics coverage for the
underlying endpoint itself already exists in test_reachability_audit.py
(Part D) and is unmodified by this ticket — see that file for those cases.

Run (from backend/):
    pytest tests/test_reachability_diagnostics_page.py -v
"""

from __future__ import annotations

from typing import Generator
from unittest.mock import MagicMock

import pytest

from tests._rnd216_web_shims import (
    diagnostics_html,
    diagnostics_js_source,
    review_console_html,
)

_DIAGNOSTICS_HTML = diagnostics_html()
_DIAGNOSTICS_JS = diagnostics_js_source()
_REVIEW_CONSOLE_HTML = review_console_html()


def _mock_db_no_session() -> Generator:
    """get_db override: DB that returns no session for every lookup (unauthenticated)."""
    mock = MagicMock()
    mock.query.return_value.filter.return_value.first.return_value = None
    yield mock


def _db_with_session(tenant_id: str):
    """get_db factory: AdminSession lookup succeeds for tenant_id, so
    _resolve_session_tenant_id() returns it (authenticated)."""

    def _override():
        mock = MagicMock()
        session_mock = MagicMock()
        session_mock.tenant_id = tenant_id

        session_q = MagicMock()
        session_q.filter.return_value = session_q
        session_q.first.return_value = session_mock

        mock.query.return_value = session_q
        yield mock

    return _override


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_diagnostics_page_redirects_to_login_when_unauthenticated(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _mock_db_no_session
    try:
        resp = client.get("/admin/diagnostics/reachability", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin/login"
    finally:
        app.dependency_overrides.clear()


def test_diagnostics_page_renders_when_authenticated(client) -> None:
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = _db_with_session("tenant-a")
    try:
        resp = client.get("/admin/diagnostics/reachability")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Architecture: thin UI reusing the real RND-178 endpoint, not new logic
# ---------------------------------------------------------------------------


def test_diagnostics_page_fetches_the_real_reachability_audit_endpoint() -> None:
    assert "/api/admin/reachability-audit" in _DIAGNOSTICS_JS


def test_diagnostics_page_does_not_pass_include_samples() -> None:
    """The page must never opt into per-message samples — aggregate-only by default."""
    assert "include_samples" not in _DIAGNOSTICS_JS


# ---------------------------------------------------------------------------
# Browser tab title: must go through i18n, not be hardcoded English (QA fix)
# ---------------------------------------------------------------------------


def test_diagnostics_page_title_is_not_hardcoded_english() -> None:
    assert "<title>Message Reachability Diagnostics</title>" not in _DIAGNOSTICS_HTML


def test_diagnostics_page_sets_document_title_via_i18n() -> None:
    assert "document.title=I18N.t('diagnostics.pageTitle')" in _DIAGNOSTICS_JS


def test_diagnostics_page_has_no_message_list_or_detail_rendering() -> None:
    """Out of scope per RND-180: no message list, no message detail drawer."""
    lowered = _DIAGNOSTICS_HTML.lower()
    for banned in ("msgid", "sdkfileid", "media_key", "local_path", "oss_key", "content_text"):
        assert banned not in lowered, f"unexpected {banned!r} reference in diagnostics page"


def test_diagnostics_page_status_registry_covers_all_known_statuses() -> None:
    """ReachabilityStatusRegistry must have an entry per RND-178 ReachabilityStatus value."""
    from app.reachability_audit import ReachabilityStatus

    for status in ReachabilityStatus:
        assert f"{status.value}:{{labelKey:" in _DIAGNOSTICS_JS.replace(" ", ""), (
            f"expected a ReachabilityStatusRegistry entry for {status.value!r}"
        )


def test_diagnostics_page_status_registry_has_fallback_for_unknown_status() -> None:
    assert "reachability.status.unknown" in _DIAGNOSTICS_JS
    assert "FALLBACK" in _DIAGNOSTICS_JS


# ---------------------------------------------------------------------------
# Nav entry
# ---------------------------------------------------------------------------


def test_review_console_has_diagnostics_nav_entry() -> None:
    assert '/admin/diagnostics/reachability' in _REVIEW_CONSOLE_HTML
    assert 'data-i18n="nav.diagnostics"' in _REVIEW_CONSOLE_HTML


# ---------------------------------------------------------------------------
# i18n: no hardcoded strings for the new visible labels this ticket requires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "nav.diagnostics",
        "diagnostics.pageTitle",
        "diagnostics.successfulDecrypted",
        "diagnostics.reachableMessages",
        "diagnostics.unreachableMessages",
        "diagnostics.reachabilityRate",
        "diagnostics.unreachableReasons",
        "diagnostics.byMessageType",
        "diagnostics.scannedMessages",
        "diagnostics.matchingTotal",
        "diagnostics.moreMessagesExist",
        "diagnostics.noData",
        "diagnostics.failedToLoad",
        "diagnostics.refresh",
    ],
)
def test_required_i18n_key_defined_in_all_locales(key: str) -> None:
    from app.assets import __file__ as _assets_init  # noqa: F401  (ensure package importable)
    import re

    with open(
        __file__.rsplit("/tests/", 1)[0] + "/app/assets/i18n.js", encoding="utf-8"
    ) as f:
        src = f.read()

    blocks = re.findall(r'"(?:zh-CN|zh-TW|en)":\s*\{.*?translations:\s*\{(.*?)\n\s*\}\s*\n\s*\},?\n', src, re.S)
    # en block uses `en: {` (no quotes) — capture separately.
    en_block = re.search(r"en:\s*\{.*?translations:\s*\{(.*?)\n\s*\}\s*\n\s*\}\s*\n\s*\};", src, re.S)
    assert en_block is not None
    blocks.append(en_block.group(1))

    assert len(blocks) == 3, "expected exactly 3 locale translation blocks (zh-CN, zh-TW, en)"
    for block in blocks:
        assert f'"{key}"' in block, f"missing i18n key {key!r} in one of the locale blocks"
