"""Resolve application configuration from DB, environment, then defaults."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from types import ModuleType
from typing import Optional

from sqlalchemy.orm import Session

from app.config import repository
from app.config.crypto import decrypt_value
from app.config.schema import CONFIG_REGISTRY
from app.settings import (
    get_email_settings,
    get_event_media_download_settings,
    get_media_storage_settings,
    get_thumbnail_settings,
    get_voice_transcode_settings,
    get_wecom_callback_settings,
    get_wecom_oauth_settings,
)

_CACHE_TTL_SECONDS = 30.0
_CACHE: dict[str, tuple[Optional[str], float]] = {}
_CACHE_LOCK = threading.Lock()

# Every registry key deliberately calls a settings factory on each cache miss.
# These factories construct fresh BaseSettings instances, preserving the
# existing monkeypatch.setenv semantics at the environment layer.
_ENV_ACCESSORS: dict[str, Callable[[], Optional[str]]] = {
    "admin_domain": lambda: get_wecom_oauth_settings().admin_domain,
    "smtp_host": lambda: get_email_settings().smtp_host,
    "smtp_port": lambda: get_email_settings().smtp_port,
    "smtp_user": lambda: get_email_settings().smtp_user,
    "smtp_password": lambda: get_email_settings().smtp_password,
    "smtp_from": lambda: get_email_settings().smtp_from,
    "media_storage_provider": lambda: get_media_storage_settings().media_storage_provider,
    "storage_backend": lambda: get_media_storage_settings().storage_backend,
    "qiniu_access_key": lambda: get_media_storage_settings().qiniu_access_key,
    "qiniu_secret_key": lambda: get_media_storage_settings().qiniu_secret_key,
    "qiniu_bucket": lambda: get_media_storage_settings().qiniu_bucket,
    "qiniu_domain": lambda: get_media_storage_settings().qiniu_domain,
    "qiniu_region": lambda: get_media_storage_settings().qiniu_region,
    "wecom_corp_id": lambda: get_wecom_oauth_settings().wecom_corp_id,
    "wecom_agent_id": lambda: get_wecom_oauth_settings().wecom_agent_id,
    "wecom_oauth_secret": lambda: get_wecom_oauth_settings().wecom_oauth_secret,
    "wecom_callback_token": lambda: get_wecom_callback_settings().wecom_callback_token,
    "wecom_callback_encoding_aes_key": lambda: get_wecom_callback_settings().wecom_callback_encoding_aes_key,
    "media_thumbnail_enabled": lambda: get_thumbnail_settings().media_thumbnail_enabled,
    "media_thumbnail_max_edge": lambda: get_thumbnail_settings().media_thumbnail_max_edge,
    "media_thumbnail_jpeg_quality": lambda: get_thumbnail_settings().media_thumbnail_jpeg_quality,
    "voice_transcode_enabled": lambda: get_voice_transcode_settings().voice_transcode_enabled,
    "event_media_download_enabled": lambda: get_event_media_download_settings().event_media_download_enabled,
    "event_media_download_batch_limit": lambda: get_event_media_download_settings().event_media_download_batch_limit,
    "event_media_download_recent_window_hours": lambda: get_event_media_download_settings().event_media_download_recent_window_hours,
    "event_media_download_retry_count": lambda: get_event_media_download_settings().event_media_download_retry_count,
    "event_media_download_backoff_seconds": lambda: get_event_media_download_settings().event_media_download_backoff_seconds,
    "event_media_download_sweep_interval_seconds": lambda: get_event_media_download_settings().event_media_download_sweep_interval_seconds,
}


def resolve(db: Session, key: str) -> Optional[str]:
    """Return a plaintext config value using DB > environment > registry default."""
    spec = CONFIG_REGISTRY.get(key)
    accessor = _ENV_ACCESSORS.get(key)
    if spec is None or accessor is None:
        return None

    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and now < cached[1]:
            return cached[0]

    stored = repository.get_raw(db, key)
    if stored is not None and stored.value is not None:
        value = decrypt_value(stored.value) if stored.is_secret else stored.value
    else:
        environment_value = accessor()
        value = environment_value if environment_value is not None else spec.default

    with _CACHE_LOCK:
        _CACHE[key] = (value, time.monotonic() + _CACHE_TTL_SECONDS)
    return value


def invalidate(key: Optional[str] = None) -> None:
    """Invalidate one resolved value, or all values when no key is supplied."""
    with _CACHE_LOCK:
        if key is None:
            _CACHE.clear()
        else:
            _CACHE.pop(key, None)


def get_config_resolver() -> ModuleType:
    """Return this module for simple dependency injection by the Settings API."""
    return sys.modules[__name__]
