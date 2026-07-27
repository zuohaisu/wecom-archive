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


def get_auth_settings() -> AuthSettings:
    return AuthSettings()


class WecomOAuthSettings(BaseSettings):
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_oauth_secret: str = ""
    admin_domain: str = ""


def get_wecom_oauth_settings() -> WecomOAuthSettings:
    return WecomOAuthSettings()


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
    media_storage_provider: Optional[str] = None
    storage_backend: Optional[str] = None
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
