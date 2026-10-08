"""Haisu request: support-page feedback must email the owner inbox."""
from __future__ import annotations

import pytest

from app.email import FEEDBACK_INBOX_EMAIL
from app.services.ai import feedback_notify
from app.services.ai.feedback_notify import notify_new_feedback


@pytest.fixture()
def sent(monkeypatch):
    captured = {}

    def fake_send(subject: str, body: str, *, operation_id: str) -> bool:
        captured["subject"] = subject
        captured["body"] = body
        captured["operation_id"] = operation_id
        return True

    monkeypatch.setattr(feedback_notify, "send_feedback_email", fake_send)
    return captured


def test_feedback_notification_emails_the_owner_inbox(sent) -> None:
    delivered = notify_new_feedback(
        feedback_id="fb-1234567890",
        tenant_id="tenant-a",
        feedback_type="bug",
        body="导出按钮点击后无响应",
        contact="alice@example.com",
        submitter="Alice",
        page_id=None,
        product_version=None,
    )

    assert delivered is True
    assert sent["operation_id"] == "feedback/fb-1234567890"
    assert "Bug / 异常" in sent["subject"]
    # 收件人固定为产品负责人邮箱；内容只进正文，不进主题。
    assert FEEDBACK_INBOX_EMAIL == "hs@crowntime.cn"
    assert "导出按钮点击后无响应" in sent["body"]
    assert "alice@example.com" in sent["body"]
    assert "Alice" in sent["body"]


def test_feedback_notification_is_best_effort_on_delivery_failure(monkeypatch, caplog) -> None:
    def failing_send(subject: str, body: str, *, operation_id: str) -> bool:
        return False

    monkeypatch.setattr(feedback_notify, "send_feedback_email", failing_send)
    with caplog.at_level("WARNING", logger="app.services.ai.feedback_notify"):
        delivered = notify_new_feedback(
            feedback_id="fb-fail",
            tenant_id="tenant-a",
            feedback_type="suggestion",
            body="希望支持批量导出",
        )

    # 投递失败只记录告警，不向提交方抛错：提交 API 必须始终可用。
    assert delivered is False
    assert any("undelivered" in record.getMessage() for record in caplog.records)


def test_feedback_notification_renders_unknown_type_and_missing_fields(sent) -> None:
    notify_new_feedback(
        feedback_id="fb-min",
        tenant_id="tenant-a",
        feedback_type="other",
        body="Hello",
    )

    assert "Hello" in sent["body"]
    assert "未填写" in sent["body"]
    assert "未记录" in sent["body"]
