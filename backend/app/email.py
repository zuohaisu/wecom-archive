"""Canonical transactional email boundary (GH-148).

Resend is the default; SMTP is an explicitly selected rollback transport.
Provider acceptance is not proof of inbox delivery. Missing configuration and
unknown outcomes never count as success. Templates remain provider-neutral.
"""

from __future__ import annotations

import hashlib
import json
import logging
import smtplib
import ssl
from contextvars import ContextVar
from datetime import datetime, timezone
from email.message import EmailMessage
from uuid import UUID, uuid4

import httpx

from app.settings import EmailSettings, get_email_settings

logger = logging.getLogger(__name__)
_HTTP_TIMEOUT_SECONDS = 10.0
_SENDING = ContextVar("transactional_email_sending", default=False)


class _DeliveryLogFilter(logging.Filter):
    """Suppress third-party wire diagnostics only in the active send context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENDING.get()


_WIRE_LOG_FILTER = _DeliveryLogFilter()


def _suppress_wire_logs() -> None:
    # Ancestor logger filters do not apply to children. Cover httpcore's
    # lazily-created wire loggers as well as any already-created descendants.
    names = {
        "httpx", "httpcore", "httpcore.connection", "httpcore.connection_pool",
        "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks",
    }
    names.update(
        name for name in logging.Logger.manager.loggerDict
        if name.startswith(("httpx.", "httpcore."))
    )
    for name in names:
        logging.getLogger(name).addFilter(_WIRE_LOG_FILTER)


def _valid_mailbox(value: str, *, sending_domain: bool = False) -> bool:
    if not value or "\r" in value or "\n" in value:
        return False
    try:
        message = EmailMessage()
        message["From"] = value
        header = message["From"]
        if header.defects or len(header.addresses) != 1:
            return False
        address = header.addresses[0]
        return bool(
            address.username and "." in address.domain
            and (not sending_domain or address.domain.lower() == "mail.crowntime.cn")
        )
    except (ValueError, IndexError):
        return False


def _configuration_ready(settings: EmailSettings) -> bool:
    if settings.email_provider == "resend":
        return bool(
            settings.resend_api_key.strip()
            and not any(c.isspace() for c in settings.resend_api_key)
            and _valid_mailbox(settings.email_from, sending_domain=True)
            and (not settings.email_reply_to or _valid_mailbox(settings.email_reply_to))
        )
    if settings.email_provider == "smtp":
        try:
            port = int(settings.smtp_port or "465")
        except ValueError:
            return False
        return bool(
            settings.smtp_host and 1 <= port <= 65535
            and _valid_mailbox(settings.smtp_from)
            and settings.smtp_user and settings.smtp_password
            and (not settings.email_reply_to or _valid_mailbox(settings.email_reply_to))
        )
    return False


def email_delivery_ready() -> bool:
    """Local configuration readiness, not remote domain verification."""
    try:
        return _configuration_ready(get_email_settings())
    except Exception:  # noqa: BLE001 - never expose settings validation input
        return False


def _send_transactional_email(
    to_email: str, subject: str, body: str, *, operation_id: str | None = None,
) -> bool:
    """One bounded attempt, no automatic retry or fallback to another provider.

    Hash the operation identity with the payload, never exposing either in the
    key. Callers with repeatable/distinct events must supply an operation ID:
    reuse it for technical retries, replace it for intentional resends. The
    payload-only fallback is for reset/export messages whose token/expiry
    already identifies the operation. Resend retains keys for only 24 hours.
    """
    try:
        settings = get_email_settings()
        if not _configuration_ready(settings):
            logger.warning("transactional email outcome=configuration_incomplete")
            return False
        if not _valid_mailbox(to_email):
            logger.warning("transactional email outcome=invalid_recipient")
            return False
        if settings.app_env.lower() != "production":
            subject = f"[NON-PRODUCTION] {subject}"
        if settings.email_provider == "smtp":
            message = EmailMessage()
            message["From"] = settings.smtp_from
            message["To"] = to_email
            message["Subject"] = subject
            if settings.email_reply_to:
                message["Reply-To"] = settings.email_reply_to
            message.set_content(body)
            with smtplib.SMTP_SSL(
                settings.smtp_host, int(settings.smtp_port or "465"),
                context=ssl.create_default_context(), timeout=_HTTP_TIMEOUT_SECONDS,
            ) as smtp:
                smtp.login(settings.smtp_user, settings.smtp_password)
                refused = smtp.send_message(message)
            if refused:
                logger.warning("transactional email provider=smtp outcome=rejected")
                return False
            logger.info("transactional email provider=smtp outcome=accepted")
            return True
        payload = {
            "from": settings.email_from,
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
        if settings.email_reply_to:
            payload["reply_to"] = settings.email_reply_to
        fingerprint = hashlib.sha256(
            json.dumps(
                [operation_id, payload] if operation_id is not None else payload,
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        _suppress_wire_logs()
        token = _SENDING.set(True)
        try:
            # Disable ambient proxies and redirects; credentials only go to the
            # fixed HTTPS endpoint. HTTPX has no transport retries by default.
            with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, trust_env=False) as client:
                response = client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {settings.resend_api_key}",
                        "Idempotency-Key": f"transactional/{fingerprint}",
                    },
                    json=payload,
                )
        finally:
            _SENDING.reset(token)
        if response.status_code != 200:
            logger.warning(
                "transactional email provider=resend outcome=rejected status=%d",
                response.status_code,
            )
            return False
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            logger.warning("transactional email provider=resend outcome=invalid_response")
            return False
        provider_id = str(UUID(result["id"]))
        logger.info(
            "transactional email provider=resend outcome=accepted provider_id=%s",
            provider_id,
        )
        return True
    except httpx.TimeoutException:
        logger.warning("transactional email provider=resend outcome=timeout_unknown")
    except httpx.RequestError:
        logger.warning("transactional email provider=resend outcome=network_unknown")
    except Exception:  # noqa: BLE001 - no exception strings, bodies or traceback secrets
        logger.warning("transactional email outcome=failed_or_unknown")
    return False


def send_password_reset_email(
    to_email: str, reset_link: str, locale: str = "zh-CN"
) -> bool:
    """Deliver a reset email without logging its secret link or recipient."""
    subject, body = _render_reset_email(reset_link, locale)
    return _send_transactional_email(to_email, subject, body)


FEEDBACK_INBOX_EMAIL = "hs@crowntime.cn"


def send_feedback_email(subject: str, body: str, *, operation_id: str) -> bool:
    """Deliver one product-feedback submission to the fixed owner inbox.

    Feedback content is user-written free text destined for a human reader:
    it travels only in the body, never the subject or headers. Each
    submission is a distinct event, so `operation_id` (the feedback id)
    makes provider-side retries idempotent without suppressing intentional
    resends.
    """
    return _send_transactional_email(
        FEEDBACK_INBOX_EMAIL, subject, body, operation_id=operation_id
    )


def send_invite_email(
    to_email: str, accept_link: str, locale: str = "zh-CN",
    *, operation_id: str | None = None,
) -> bool:
    """Each explicit invitation is a new send, even when its token is reused.

    A technical retry must supply the same operation_id as its original send.
    There are no automatic retries here or in the invitation route.
    """
    subject, body = _render_invite_email(accept_link, locale)
    return _send_transactional_email(
        to_email, subject, body,
        operation_id=operation_id if operation_id is not None else f"invite/{uuid4()}",
    )


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
    subject, body = _render_export_ready_email(export_link, expires_at, locale)
    return _send_transactional_email(to_email, subject, body)


def send_billing_notification_email(
    to_email: str,
    kind: str,
    effective_at: datetime,
    action_link: str,
    locale: str = "zh-CN",
    *, operation_id: str,
) -> bool:
    """Deliver one durable billing intent; retries reuse its operation ID."""
    subject, body = _render_billing_notification_email(
        kind, effective_at, action_link, locale
    )
    return _send_transactional_email(
        to_email, subject, body, operation_id=operation_id,
    )


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
