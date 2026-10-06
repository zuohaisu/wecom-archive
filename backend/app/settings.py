"""
Domain-grouped Typed Settings (RND-223).

Each Settings class is a thin, per-call pydantic-settings wrapper around the
exact environment variables the corresponding module used to read directly
via os.getenv/os.environ.get. Every factory function below (get_xxx_settings)
constructs a brand-new instance on every call -- there is no lru_cache and no
module-level singleton -- because the rest of the codebase (and its test
suite, which relies heavily on monkeypatch.setenv after import time) depends
on changing an environment variable taking effect on the very next read, not
on the next process restart. app/db/session.py's engine cache is the one
exception to that rule: it caches the constructed engine, not a config value,
so it is unaffected by this module.

Field types are deliberately str/Optional[str] mirroring the exact default
argument each replaced os.getenv/os.environ.get call used to pass. The
strip/lower/int-parsing/bounds-checking/fail-loud logic that used to sit next
to that call stays exactly where it was, in the original module -- it now
reads from a field on one of these classes instead of from os.environ
directly. Typed Settings here is a delivery mechanism for the *read*, not a
replacement for that logic; letting pydantic itself parse/validate would risk
changing an exception type or a default that dozens of existing tests pin.

This module must never import any other app module (routers, services, or
app.main) -- it exists purely to be imported BY those modules, and importing
inward here would create a cycle.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    database_url: str = ""


def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()


class AuthSettings(BaseSettings):
    auth_mode: str = ""
    app_env: str = "development"
    admin_username: str = ""
    admin_password_hash: str = ""
    session_ttl_hours: str = ""


def get_auth_settings() -> AuthSettings:
    return AuthSettings()


class EmailSettings(BaseSettings):
    email_provider: str = "resend"
    resend_api_key: str = Field(default="", repr=False)
    email_from: str = "康冠时代企业微信会话存档 <notifications@mail.crowntime.cn>"
    email_reply_to: str = ""
    app_env: str = "development"
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    reset_base_url: str = Field(default="", validation_alias="PASSWORD_RESET_BASE_URL")
    invite_base_url: str = Field(default="", validation_alias="INVITE_BASE_URL")
    reset_token_ttl_hours: str = Field(
        default="1", validation_alias="PASSWORD_RESET_TOKEN_TTL_HOURS"
    )


def get_email_settings() -> EmailSettings:
    return EmailSettings()


class WecomOAuthSettings(BaseSettings):
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_oauth_secret: str = ""
    admin_domain: str = ""


def get_wecom_oauth_settings() -> WecomOAuthSettings:
    return WecomOAuthSettings()


class WecomThirdPartySettings(BaseSettings):
    """Credentials and isolated callbacks for the provider install flow."""

    wecom_third_party_suite_id: str = ""
    wecom_third_party_suite_secret: str = ""
    wecom_third_party_callback_url: str = ""
    wecom_third_party_instruction_token: str = ""
    wecom_third_party_instruction_encoding_aes_key: str = ""
    wecom_third_party_corp_id: str = ""


def get_wecom_third_party_settings() -> WecomThirdPartySettings:
    return WecomThirdPartySettings()


class MediaStorageSettings(BaseSettings):
    # No default (None) mirrors os.environ.get("MEDIA_STORAGE_PROVIDER") /
    # os.environ.get("STORAGE_BACKEND") being called with no second
    # argument -- callers rely on the "unset" case being falsy in an `or`
    # chain, and on a present-but-empty value staying an empty string
    # rather than being coerced to the fallback.
    media_storage_provider: Optional[str] = None  # noqa: UP045 -- Python 3.9 runtime compatibility
    storage_backend: Optional[str] = None  # noqa: UP045 -- Python 3.9 runtime compatibility
    storage_local_path: str = ""
    qiniu_access_key: str = ""
    qiniu_secret_key: str = ""
    qiniu_bucket: str = ""
    qiniu_domain: str = ""
    qiniu_region: str = ""
    qiniu_timeout_seconds: str = ""
    media_signed_url_ttl_seconds: str = ""
    media_signed_url_window_seconds: str = ""


def get_media_storage_settings() -> MediaStorageSettings:
    return MediaStorageSettings()


class ThumbnailSettings(BaseSettings):
    media_thumbnail_enabled: str = ""
    media_thumbnail_max_edge: str = ""
    media_thumbnail_jpeg_quality: str = ""


def get_thumbnail_settings() -> ThumbnailSettings:
    return ThumbnailSettings()


class VoiceTranscodeSettings(BaseSettings):
    voice_transcode_enabled: str = ""


def get_voice_transcode_settings() -> VoiceTranscodeSettings:
    return VoiceTranscodeSettings()


class EventMediaDownloadSettings(BaseSettings):
    """Controls for archive-complete generic-media dispatch and retries.

    The historical ``event_media_download_*`` names remain stable for the
    settings UI and deployed environment files.  Dispatch is now generic
    (not image-only) and defaults on; set ``EVENT_MEDIA_DOWNLOAD_ENABLED``
    explicitly to ``false`` only for an emergency rollback.
    """

    event_media_download_enabled: str = "true"
    event_media_download_batch_limit: str = "20"
    event_media_download_recent_window_hours: str = "24"
    event_media_download_retry_count: str = "3"
    event_media_download_backoff_seconds: str = "30"
    # Retained for configuration-store compatibility. RND-343 removes the
    # in-process sweep thread, so this no longer schedules worker execution.
    event_media_download_sweep_interval_seconds: str = "15"


def get_event_media_download_settings() -> EventMediaDownloadSettings:
    return EventMediaDownloadSettings()


class SettingsEncryptionSettings(BaseSettings):
    settings_encryption_key: str = ""


def get_settings_encryption_settings() -> SettingsEncryptionSettings:
    return SettingsEncryptionSettings()


class WechatPaySettings(BaseSettings):
    """WeChat Pay v3 Native configuration; values are never cached."""

    wechat_pay_enabled: str = "false"
    wechat_pay_app_id: str = ""
    wechat_pay_mch_id: str = ""
    wechat_pay_merchant_serial_no: str = ""
    wechat_pay_merchant_private_key: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_public_key_id: str = ""
    wechat_pay_public_key: str = ""
    wechat_pay_notify_url: str = ""
    wechat_pay_refund_notify_url: str = ""


def get_wechat_pay_settings() -> WechatPaySettings:
    return WechatPaySettings()


class AlipaySettings(BaseSettings):
    """Alipay computer-website payment configuration; values are never cached."""

    alipay_enabled: str = "false"
    alipay_app_id: str = ""
    alipay_seller_id: str = ""
    alipay_merchant_private_key: str = ""
    alipay_public_key: str = ""
    alipay_notify_url: str = ""
    alipay_return_url: str = ""


def get_alipay_settings() -> AlipaySettings:
    return AlipaySettings()


class SelfServiceTrialSettings(BaseSettings):
    """Controls visibility of the public "start 15-day trial" entry point
    (RND-396) on the login page. Defaults closed: the underlying WeCom
    third-party authorization endpoints (RND-346/347/348/350) are governed
    separately by real suite credentials and by RND-353's own controlled
    production rollout -- this flag only decides whether the CTA that
    points visitors at them is discoverable. Deployment config is the only
    thing that turns it on, once non-prod E2E has passed.
    """

    self_service_trial_entry_enabled: str = "false"


def get_self_service_trial_settings() -> SelfServiceTrialSettings:
    return SelfServiceTrialSettings()


class ProductAnalyticsSettings(BaseSettings):
    """Closed-by-default collection switch for RND-162 product events."""

    product_analytics_enabled: str = "false"


def get_product_analytics_settings() -> ProductAnalyticsSettings:
    return ProductAnalyticsSettings()


class AiSettings(BaseSettings):
    """AI support (RND-354 epic) configuration. ai_support_enabled is the
    single kill switch every AI surface (T2 answer service, T3 UI, T5
    eval/handoff) reads — never duplicate this flag elsewhere.

    RND-408 adds ai_public_support_enabled: a separate switch for the
    anonymous visitor pre-sales chat. It is gated by ai_support_enabled
    (public cannot be on while overall AI support is off) and has its own
    daily token budgets so visitor traffic never consumes tenant quotas.
    """

    ai_support_enabled: str = "false"
    ai_llm_provider: str = ""  # "" | "deepseek" | "fake"
    ai_llm_model: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # RND-359 (T5) cost cap: total prompt+completion tokens a single tenant
    # may spend per UTC day before answer_service refuses further LLM
    # calls (still serves the deterministic disabled/insufficient-evidence
    # paths). Empty/unset = no cap.
    ai_daily_token_budget_per_tenant: str = ""
    # RND-408 public AI support kill switch and budgets. Default closed.
    ai_public_support_enabled: str = "false"
    # Total public-token budget across all anonymous visitors per UTC day.
    ai_public_support_daily_token_budget: str = ""
    # Per-visitor daily token budget (still UTC day). Empty/unset = no cap.
    ai_public_support_daily_token_budget_per_visitor: str = ""


def get_ai_settings() -> AiSettings:
    return AiSettings()
