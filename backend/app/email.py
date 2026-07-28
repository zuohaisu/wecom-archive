"""Password-reset email delivery (RND-278 / F0-3).

Uses only the standard library. With no SMTP host or sender configured, the
console transport records that a reset email was generated without exposing a
token in application logs.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.settings import get_email_settings

logger = logging.getLogger(__name__)


def send_password_reset_email(
    to_email: str, reset_link: str, locale: str = "zh-CN"
) -> bool:
    """Deliver a reset email, or use the deterministic no-SMTP transport.

    The console fallback intentionally does not log ``reset_link``: reset
    tokens are secrets and must remain limited to the email/browser URL.
    """
    settings = get_email_settings()
    subject, body = _render_reset_email(reset_link, locale)
    if not settings.smtp_host or not settings.smtp_from:
        logger.warning("[email-console] password reset email generated for %s", to_email)
        return True

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.smtp_host, int(settings.smtp_port or 465), context=context
        ) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        return True
    except Exception:  # noqa: BLE001 - delivery failure must not enumerate accounts
        logger.exception("send_password_reset_email failed for %s", to_email)
        return False


def _render_reset_email(reset_link: str, locale: str) -> tuple[str, str]:
    """Render the reset mail; the link is the only action in the message."""
    subject = "重置您的康冠时代会话存档密码"
    body = (
        "我们收到了您的密码重置请求。\n\n"
        f"请点击以下链接重置密码（1 小时内有效，且仅可使用一次）：\n{reset_link}\n\n"
        "如果您没有请求重置密码，请忽略此邮件，链接将自动失效。"
    )
    return subject, body
