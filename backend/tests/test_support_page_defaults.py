"""Haisu request: the help page titles itself 帮助与客服 and opens on 提交反馈."""
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
    # 标题不再是 AI 客服；聊天禁用提示的兜底文案不在本断言范围内。
    assert 'data-i18n="support.pageTitle">AI 客服' not in source


def test_support_page_defaults_to_the_feedback_tab() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert 'id="tab-chat" role="tab" aria-selected="false"' in source
    assert 'id="tab-feedback" role="tab" aria-selected="true"' in source
    chat_panel = source[source.index('id="support-chat-panel"') : source.index('id="support-chat-panel"') + 120]
    feedback_panel = source[source.index('id="support-feedback-panel"') : source.index('id="support-feedback-panel"') + 120]
    assert 'class="hidden"' in chat_panel
    assert 'class="hidden"' not in feedback_panel


def test_support_page_title_localized_in_all_three_locales() -> None:
    source = _I18N.read_text(encoding="utf-8")

    for value in (
        '"support.pageTitle": "帮助与客服"',
        '"support.pageTitle": "幫助與客服"',
        '"support.pageTitle": "Help & Support"',
    ):
        assert len(re.findall(re.escape(value), source)) == 1, value
    assert '"support.pageTitle": "AI' not in source
