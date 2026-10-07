"""GH-100 follow-up + GH-188 convergence: the help page is the feedback form.

After #188's feedback-only rewrite (and the merged dark theme), the page
carries a single feedback form — no tabs, no AI-chat surfaces, no
diagnostics consent checkbox — titled 帮助与客服.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app" / "web" / "templates" / "support.html"
_I18N = _BACKEND / "app" / "assets" / "i18n.js"


def test_support_page_titles_itself_help_and_support() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "<title>帮助与客服</title>" in source
    assert source.count('data-i18n="support.pageTitle">帮助与客服') == 2  # breadcrumb + h1
    assert "AI 客服" not in source


def test_support_page_has_no_diagnostics_consent_checkbox() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert "feedback-include-diagnostics" not in source
    assert "support.includeDiagnostics" not in source


def test_support_page_is_the_single_feedback_form() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert 'id="feedback-form"' in source
    assert "fetch('/api/ai/support/feedback'" in source
    # 单一表单页：无 tab、无聊天面板、无转人工弹层。
    assert "support-tab" not in source
    assert 'id="support-chat-panel"' not in source
    assert 'id="handoff-modal"' not in source


def test_support_page_title_localized_in_all_three_locales() -> None:
    source = _I18N.read_text(encoding="utf-8")

    for value in (
        '"support.pageTitle": "帮助与客服"',
        '"support.pageTitle": "幫助與客服"',
        '"support.pageTitle": "Help & Support"',
    ):
        assert len(re.findall(re.escape(value), source)) == 1, value
