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


def test_ai_disabled_notice_lives_only_in_the_chat_panel() -> None:
    """Haisu request: the 提交反馈 tab must not show the AI-disabled notice."""
    source = _TEMPLATE.read_text(encoding="utf-8")

    chat_panel_start = source.index('id="support-chat-panel"')
    feedback_panel_start = source.index('id="support-feedback-panel"')
    notice_start = source.index('id="support-disabled"')
    assert chat_panel_start < notice_start < feedback_panel_start


def test_feedback_intro_copy_names_no_ai() -> None:
    source = _I18N.read_text(encoding="utf-8")

    assert '"support.feedbackIntro": "在此提交问题、Bug或建议"' in source
    assert '"support.feedbackIntro": "在此提交問題、Bug或建議"' in source
    assert '"support.feedbackIntro": "Submit questions, bugs, or suggestions here"' in source
    assert "不需要先向 AI 提问" not in source
