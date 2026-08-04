"""Metadata contract for Settings values editable through the Settings UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.config.constants import RESTART_REQUIRED_KEYS, ConfigGroup


@dataclass(frozen=True)
class ConfigItemSpec:
    """One editable configuration value and its UI/runtime metadata."""

    key: str
    group: ConfigGroup
    value_type: Literal["string", "secret", "int", "bool"]
    is_secret: bool
    required: bool
    conditional_on: Optional[str]  # noqa: UP045 -- Python 3.9 runtime compatibility
    restart_required: bool
    default: Optional[str]  # noqa: UP045 -- Python 3.9 runtime compatibility


CONFIG_REGISTRY: dict[str, ConfigItemSpec] = {
    "admin_domain": ConfigItemSpec(
        key="admin_domain",
        group=ConfigGroup.GENERAL,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "smtp_host": ConfigItemSpec(
        key="smtp_host",
        group=ConfigGroup.THIRD_PARTY,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "smtp_port": ConfigItemSpec(
        key="smtp_port",
        group=ConfigGroup.THIRD_PARTY,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "smtp_user": ConfigItemSpec(
        key="smtp_user",
        group=ConfigGroup.THIRD_PARTY,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "smtp_password": ConfigItemSpec(
        key="smtp_password",
        group=ConfigGroup.THIRD_PARTY,
        value_type="secret",
        is_secret=True,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "smtp_from": ConfigItemSpec(
        key="smtp_from",
        group=ConfigGroup.THIRD_PARTY,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "media_storage_provider": ConfigItemSpec(
        key="media_storage_provider",
        group=ConfigGroup.STORAGE,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=True,
        default=None,
    ),
    "storage_backend": ConfigItemSpec(
        key="storage_backend",
        group=ConfigGroup.STORAGE,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=True,
        default=None,
    ),
    "qiniu_access_key": ConfigItemSpec(
        key="qiniu_access_key",
        group=ConfigGroup.STORAGE,
        value_type="secret",
        is_secret=True,
        required=False,
        conditional_on="media_storage_provider=qiniu_kodo",
        restart_required=False,
        default="",
    ),
    "qiniu_secret_key": ConfigItemSpec(
        key="qiniu_secret_key",
        group=ConfigGroup.STORAGE,
        value_type="secret",
        is_secret=True,
        required=False,
        conditional_on="media_storage_provider=qiniu_kodo",
        restart_required=False,
        default="",
    ),
    "qiniu_bucket": ConfigItemSpec(
        key="qiniu_bucket",
        group=ConfigGroup.STORAGE,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on="media_storage_provider=qiniu_kodo",
        restart_required=False,
        default="",
    ),
    "qiniu_domain": ConfigItemSpec(
        key="qiniu_domain",
        group=ConfigGroup.STORAGE,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on="media_storage_provider=qiniu_kodo",
        restart_required=False,
        default="",
    ),
    "qiniu_region": ConfigItemSpec(
        key="qiniu_region",
        group=ConfigGroup.STORAGE,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on="media_storage_provider=qiniu_kodo",
        restart_required=False,
        default="",
    ),
    "wecom_corp_id": ConfigItemSpec(
        key="wecom_corp_id",
        group=ConfigGroup.WECOM,
        value_type="string",
        is_secret=False,
        required=True,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "wecom_agent_id": ConfigItemSpec(
        key="wecom_agent_id",
        group=ConfigGroup.WECOM,
        value_type="string",
        is_secret=False,
        required=True,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "wecom_oauth_secret": ConfigItemSpec(
        key="wecom_oauth_secret",
        group=ConfigGroup.WECOM,
        value_type="secret",
        is_secret=True,
        required=True,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "wecom_callback_token": ConfigItemSpec(
        key="wecom_callback_token",
        group=ConfigGroup.WECOM,
        value_type="secret",
        is_secret=True,
        required=True,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "wecom_callback_encoding_aes_key": ConfigItemSpec(
        key="wecom_callback_encoding_aes_key",
        group=ConfigGroup.WECOM,
        value_type="secret",
        is_secret=True,
        required=True,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "media_thumbnail_enabled": ConfigItemSpec(
        key="media_thumbnail_enabled",
        group=ConfigGroup.ADVANCED,
        value_type="bool",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "media_thumbnail_max_edge": ConfigItemSpec(
        key="media_thumbnail_max_edge",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "media_thumbnail_jpeg_quality": ConfigItemSpec(
        key="media_thumbnail_jpeg_quality",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "voice_transcode_enabled": ConfigItemSpec(
        key="voice_transcode_enabled",
        group=ConfigGroup.ADVANCED,
        value_type="bool",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="",
    ),
    "event_media_download_enabled": ConfigItemSpec(
        key="event_media_download_enabled",
        group=ConfigGroup.ADVANCED,
        value_type="bool",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="true",
    ),
    "event_media_download_batch_limit": ConfigItemSpec(
        key="event_media_download_batch_limit",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="20",
    ),
    "event_media_download_recent_window_hours": ConfigItemSpec(
        key="event_media_download_recent_window_hours",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="24",
    ),
    "event_media_download_retry_count": ConfigItemSpec(
        key="event_media_download_retry_count",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="3",
    ),
    "event_media_download_backoff_seconds": ConfigItemSpec(
        key="event_media_download_backoff_seconds",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="30",
    ),
    "event_media_download_sweep_interval_seconds": ConfigItemSpec(
        key="event_media_download_sweep_interval_seconds",
        group=ConfigGroup.ADVANCED,
        value_type="int",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default="15",
    ),
}


def validate_registry() -> None:
    """Raise ValueError if the registry has internally inconsistent metadata."""
    spec_keys = [spec.key for spec in CONFIG_REGISTRY.values()]
    if len(spec_keys) != len(set(spec_keys)):
        raise ValueError("Duplicate configuration key in registry")

    for registry_key, spec in CONFIG_REGISTRY.items():
        if registry_key != spec.key:
            raise ValueError(
                f"Registry key {registry_key!r} does not match spec key {spec.key!r}"
            )
        if spec.is_secret and spec.value_type != "secret":
            raise ValueError(f"Secret configuration key {spec.key!r} must use value_type='secret'")
        if spec.restart_required != (spec.key in RESTART_REQUIRED_KEYS):
            raise ValueError(f"Restart metadata is inconsistent for {spec.key!r}")
        if spec.conditional_on:
            conditional_key, separator, _value = spec.conditional_on.partition("=")
            if not separator or not conditional_key or conditional_key not in CONFIG_REGISTRY:
                raise ValueError(
                    f"Configuration key {spec.key!r} has an unknown conditional key"
                )


validate_registry()
