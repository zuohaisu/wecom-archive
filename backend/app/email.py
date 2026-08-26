"""Password-reset email delivery (RND-278 / F0-3).

Uses only the standard library. With no SMTP host or sender configured, the
console transport records that a reset email was generated without exposing a
token in application logs.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timezone
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


def send_billing_notification_email(
    to_email: str,
    kind: str,
    effective_at: datetime,
    action_link: str,
    locale: str = "zh-CN",
) -> bool:
    """Deliver a fixed billing-state notice without provider/payment details."""
    settings = get_email_settings()
    if not settings.smtp_host or not settings.smtp_from:
        logger.warning("billing notification email transport is not configured")
        return False
    subject, body = _render_billing_notification_email(
        kind, effective_at, action_link, locale
    )
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
    except Exception:  # noqa: BLE001 - never log recipient or billing identifiers
        logger.exception("send_billing_notification_email failed")
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


def _render_billing_notification_email(
    kind: str,
    effective_at: datetime,
    action_link: str,
    locale: str,
) -> tuple[str, str]:
    """Render only allow-listed state text and an authenticated action link."""
    zh = {
        "subscription_expiry_30d": ("年度套餐将在 30 天后到期", "年度套餐即将到期，请提前安排续费。"),
        "subscription_expiry_7d": ("年度套餐将在 7 天后到期", "年度套餐即将到期，请尽快续费。"),
        "subscription_expiry_1d": ("年度套餐将在明日到期", "年度套餐即将到期，请立即检查续费安排。"),
        "subscription_expired": ("年度套餐已到期", "年度套餐已进入七天宽限期，请续费以避免服务冻结。"),
        "subscription_grace_ending_1d": ("服务宽限期将在明日结束", "宽限期即将结束，未续费将停止新的归档和业务访问。"),
        "tenant_frozen": ("组织服务已冻结", "组织因套餐到期已冻结；历史数据保留，请登录续费恢复服务。"),
        "payment_activation_pending": ("支付激活需要处理", "可信收款已确认，但订阅激活尚未完成，请在运营后台处理。"),
        "payment_pending_timeout": ("支付订单确认超时", "支付订单超过确认窗口，请在运营后台检查渠道状态。"),
        "payment_channel_paid_local_pending": ("渠道已支付但本地仍待处理", "渠道已确认收款，但本地处理仍待完成，请在运营后台检查。"),
        "payment_query_failed": ("支付查单失败", "支付渠道查询未能完成，请在运营后台检查恢复状态。"),
        "payment_reconciliation_mismatch": ("支付对账差异", "渠道支付事实与本地状态不一致，请在运营后台完成人工复核。"),
        "payment_callback_signature_failure": ("微信支付回调验签失败", "微信支付回调签名无法验证，请立即检查支付配置和回调入口。"),
        "payment_callback_decrypt_failure": ("微信支付回调解密失败", "微信支付回调资源无法解密，请立即检查支付配置和回调入口。"),
        "refund_processing_timeout": ("退款处理超时", "退款长时间仍在处理中，请在运营后台查询渠道状态。"),
        "refund_abnormal": ("退款状态异常", "退款渠道返回异常状态，请在运营后台检查并处理。"),
        "refund_manual_recovery_required": ("退款需要人工恢复", "退款与订阅期限存在歧义，请在运营后台完成受控恢复。"),
    }
    en = {
        "subscription_expiry_30d": ("Annual plan expires in 30 days", "Your annual plan is approaching expiry. Please plan the renewal."),
        "subscription_expiry_7d": ("Annual plan expires in 7 days", "Your annual plan is approaching expiry. Please renew soon."),
        "subscription_expiry_1d": ("Annual plan expires tomorrow", "Your annual plan is approaching expiry. Please review renewal now."),
        "subscription_expired": ("Annual plan has expired", "Your plan is in its seven-day grace period. Renew to prevent a service freeze."),
        "subscription_grace_ending_1d": ("Service grace period ends tomorrow", "The grace period is ending. New archive and business access will stop without renewal."),
        "tenant_frozen": ("Organization service is frozen", "The plan expired and service is frozen. Historical data is retained; sign in to renew."),
        "payment_activation_pending": ("Payment activation needs attention", "A trusted payment was confirmed but subscription activation is pending. Review it in platform operations."),
        "payment_pending_timeout": ("Payment order confirmation timed out", "A payment order exceeded its confirmation window. Review the provider state in platform operations."),
        "payment_channel_paid_local_pending": ("Channel payment pending locally", "The channel confirmed payment while local processing remains pending. Review it in platform operations."),
        "payment_query_failed": ("Payment query failed", "A payment-provider query could not complete. Review recovery status in platform operations."),
        "payment_reconciliation_mismatch": ("Payment reconciliation mismatch", "The channel payment fact conflicts with local state. Complete manual review in platform operations."),
        "payment_callback_signature_failure": ("WeChat Pay callback signature failure", "A WeChat Pay callback signature could not be verified. Review payment configuration and callback ingress immediately."),
        "payment_callback_decrypt_failure": ("WeChat Pay callback decrypt failure", "A WeChat Pay callback resource could not be decrypted. Review payment configuration and callback ingress immediately."),
        "refund_processing_timeout": ("Refund processing timed out", "A refund remains in processing. Query the provider state in platform operations."),
        "refund_abnormal": ("Refund status is abnormal", "The provider reported an abnormal refund state. Review it in platform operations."),
        "refund_manual_recovery_required": ("Refund needs manual recovery", "The refund and subscription term are ambiguous. Complete controlled recovery in platform operations."),
    }
    translations = en if locale.lower().startswith("en") else zh
    if kind not in translations:
        raise ValueError("unsupported billing notification kind")
    subject, message = translations[kind]
    when = effective_at.astimezone(timezone.utc).isoformat()
    if translations is en:
        body = f"{message}\n\nEffective time (UTC): {when}\n\nSign in securely:\n{action_link}\n"
    else:
        body = f"{message}\n\n生效时间（UTC）：{when}\n\n请通过安全入口登录：\n{action_link}\n"
    return subject, body
