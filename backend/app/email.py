"""Password-reset email delivery (RND-278 / F0-3).

Uses only the standard library. With no SMTP host or sender configured, the
console transport records that a reset email was generated without exposing a
token in application logs.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime
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


def send_invite_email(
    to_email: str, accept_link: str, locale: str = "zh-CN"
) -> bool:
    """Deliver an invitation email without logging its secret acceptance link."""
    settings = get_email_settings()
    subject, body = _render_invite_email(accept_link, locale)
    if not settings.smtp_host or not settings.smtp_from:
        logger.warning("[email-console] invitation email generated for %s", to_email)
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
    except Exception:  # noqa: BLE001 - delivery failure must not expose invitation state
        logger.exception("send_invite_email failed for %s", to_email)
        return False


def send_export_ready_email(
    to_email: str,
    export_link: str,
    expires_at: datetime,
    locale: str = "zh-CN",
) -> bool:
    """Notify an Owner that a private seven-day export is ready.

    The URL points to the authenticated export page rather than acting as a
    bearer credential.  It is never written to logs on either transport.
    """
    settings = get_email_settings()
    subject, body = _render_export_ready_email(export_link, expires_at, locale)
    if not settings.smtp_host or not settings.smtp_from:
        # Unlike password-reset development previews, an export notice is
        # the only promised delivery path for a seven-day artifact.  Never
        # mark it sent when no transport actually exists.
        logger.warning("export ready email transport is not configured")
        return False

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
    except Exception:  # noqa: BLE001 - never expose the private export link
        logger.exception("send_export_ready_email failed")
        return False


def _render_invite_email(accept_link: str, locale: str) -> tuple[str, str]:
    """Render the invitation mail; its link is the only account action."""
    subject = "您被邀请加入康冠时代企业微信会话存档"
    body = (
        "您被邀请加入康冠时代企业微信会话存档。\n\n"
        f"请点击以下链接设置密码并激活账号：\n{accept_link}\n\n"
        "如果您不认识邀请发起人，请忽略此邮件。"
    )
    return subject, body


def _render_reset_email(reset_link: str, locale: str) -> tuple[str, str]:
    """Render the reset mail; the link is the only action in the message."""
    subject = "重置您的康冠时代会话存档密码"
    body = (
        "我们收到了您的密码重置请求。\n\n"
        f"请点击以下链接重置密码（1 小时内有效，且仅可使用一次）：\n{reset_link}\n\n"
        "如果您没有请求重置密码，请忽略此邮件，链接将自动失效。"
    )
    return subject, body


def _render_export_ready_email(
    export_link: str, expires_at: datetime, locale: str
) -> tuple[str, str]:
    subject = "您的企业微信多媒体归档已可下载"
    body = (
        "您申请的企业微信全量多媒体归档 ZIP 已生成。\n\n"
        f"请登录后下载：\n{export_link}\n\n"
        f"文件保留至：{expires_at.isoformat()}\n"
        "到期后系统会自动删除文件；如您未发起本次导出，请立即检查账号安全。"
    )
    return subject, body
