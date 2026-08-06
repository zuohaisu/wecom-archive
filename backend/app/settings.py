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
    """Credentials and callback for the isolated third-party install flow."""

    wecom_third_party_suite_id: str = ""
    wecom_third_party_suite_secret: str = ""
    wecom_third_party_suite_ticket: str = ""
    wecom_third_party_callback_url: str = ""


def get_wecom_third_party_settings() -> WecomThirdPartySettings:
    return WecomThirdPartySettings()


class WecomCallbackSettings(BaseSettings):
    wecom_callback_token: str = ""
    wecom_callback_encoding_aes_key: str = ""
    wecom_corp_id: str = ""


def get_wecom_callback_settings() -> WecomCallbackSettings:
    return WecomCallbackSettings()


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
