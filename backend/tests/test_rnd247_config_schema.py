"""RND-247 configuration metadata registry contract tests."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

import app.config.schema as schema
from app.config.constants import ConfigGroup, RESTART_REQUIRED_KEYS, RESOLVE_ORDER
from app.config.schema import CONFIG_REGISTRY, ConfigItemSpec, validate_registry
from app.settings import (
    EmailSettings,
    EventMediaDownloadSettings,
    MediaStorageSettings,
    ThumbnailSettings,
    VoiceTranscodeSettings,
    WecomCallbackSettings,
    WecomOAuthSettings,
)


EXPECTED_KEYS = {
    "admin_domain",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "media_storage_provider",
    "storage_backend",
    "qiniu_access_key",
    "qiniu_secret_key",
    "qiniu_bucket",
    "qiniu_domain",
    "qiniu_region",
    "wecom_corp_id",
    "wecom_agent_id",
    "wecom_oauth_secret",
    "wecom_callback_token",
    "wecom_callback_encoding_aes_key",
    "media_thumbnail_enabled",
    "media_thumbnail_max_edge",
    "media_thumbnail_jpeg_quality",
    "voice_transcode_enabled",
    "event_media_download_enabled",
    "event_media_download_batch_limit",
    "event_media_download_recent_window_hours",
    "event_media_download_retry_count",
    "event_media_download_backoff_seconds",
    "event_media_download_sweep_interval_seconds",
}


SETTINGS_INSTANCE_BY_KEY = {
    "admin_domain": WecomOAuthSettings(),
    "smtp_host": EmailSettings(),
    "smtp_port": EmailSettings(),
    "smtp_user": EmailSettings(),
    "smtp_password": EmailSettings(),
    "smtp_from": EmailSettings(),
    "media_storage_provider": MediaStorageSettings(),
    "storage_backend": MediaStorageSettings(),
    "qiniu_access_key": MediaStorageSettings(),
    "qiniu_secret_key": MediaStorageSettings(),
    "qiniu_bucket": MediaStorageSettings(),
    "qiniu_domain": MediaStorageSettings(),
    "qiniu_region": MediaStorageSettings(),
    "wecom_corp_id": WecomOAuthSettings(),
    "wecom_agent_id": WecomOAuthSettings(),
    "wecom_oauth_secret": WecomOAuthSettings(),
    "wecom_callback_token": WecomCallbackSettings(),
    "wecom_callback_encoding_aes_key": WecomCallbackSettings(),
    "media_thumbnail_enabled": ThumbnailSettings(),
    "media_thumbnail_max_edge": ThumbnailSettings(),
    "media_thumbnail_jpeg_quality": ThumbnailSettings(),
    "voice_transcode_enabled": VoiceTranscodeSettings(),
    "event_media_download_enabled": EventMediaDownloadSettings(),
    "event_media_download_batch_limit": EventMediaDownloadSettings(),
    "event_media_download_recent_window_hours": EventMediaDownloadSettings(),
    "event_media_download_retry_count": EventMediaDownloadSettings(),
    "event_media_download_backoff_seconds": EventMediaDownloadSettings(),
    "event_media_download_sweep_interval_seconds": EventMediaDownloadSettings(),
}


def test_registry_has_the_complete_config_item_contract() -> None:
    assert set(CONFIG_REGISTRY) == EXPECTED_KEYS
    assert tuple(field.name for field in fields(ConfigItemSpec)) == (
        "key",
        "group",
        "value_type",
        "is_secret",
        "required",
        "conditional_on",
        "restart_required",
        "default",
    )
    for registry_key, spec in CONFIG_REGISTRY.items():
        assert isinstance(spec, ConfigItemSpec)
        assert registry_key == spec.key
        assert isinstance(spec.group, ConfigGroup)
        assert spec.value_type in {"string", "secret", "int", "bool"}
        assert isinstance(spec.is_secret, bool)
        assert isinstance(spec.required, bool)
        assert spec.conditional_on is None or isinstance(spec.conditional_on, str)
        assert isinstance(spec.restart_required, bool)
        assert spec.default is None or isinstance(spec.default, str)


def test_every_registry_key_is_an_existing_settings_attribute() -> None:
    assert set(SETTINGS_INSTANCE_BY_KEY) == set(CONFIG_REGISTRY)
    assert len({spec.key for spec in CONFIG_REGISTRY.values()}) == len(CONFIG_REGISTRY)
    for key, settings_instance in SETTINGS_INSTANCE_BY_KEY.items():
        assert hasattr(settings_instance, key)
        assert isinstance(getattr(settings_instance, key), (str, type(None)))


def test_secret_metadata_is_consistent_and_only_marks_credentials() -> None:
    secret_keys = {
        "smtp_password",
        "qiniu_access_key",
        "qiniu_secret_key",
        "wecom_oauth_secret",
        "wecom_callback_token",
        "wecom_callback_encoding_aes_key",
    }
    assert {key for key, spec in CONFIG_REGISTRY.items() if spec.is_secret} == secret_keys
    for spec in CONFIG_REGISTRY.values():
        assert spec.is_secret is (spec.key in secret_keys)
        if spec.is_secret:
            assert spec.value_type == "secret"


def test_wecom_initialization_fields_are_required() -> None:
    required_keys = {
        "wecom_corp_id",
        "wecom_agent_id",
        "wecom_oauth_secret",
        "wecom_callback_token",
        "wecom_callback_encoding_aes_key",
    }
    assert {key for key, spec in CONFIG_REGISTRY.items() if spec.required} == required_keys


def test_registry_validation_accepts_the_real_registry() -> None:
    validate_registry()


def test_registry_validation_rejects_duplicate_spec_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = ConfigItemSpec(
        key="duplicate",
        group=ConfigGroup.GENERAL,
        value_type="string",
        is_secret=False,
        required=False,
        conditional_on=None,
        restart_required=False,
        default=None,
    )
    monkeypatch.setattr(schema, "CONFIG_REGISTRY", {"first": duplicate, "second": duplicate})

    with pytest.raises(ValueError, match="Duplicate configuration key"):
        schema.validate_registry()


def test_conditional_references_and_restart_flags_are_registry_backed() -> None:
    for spec in CONFIG_REGISTRY.values():
        if spec.conditional_on:
            conditional_key, separator, _value = spec.conditional_on.partition("=")
            assert separator == "="
            assert conditional_key in CONFIG_REGISTRY
        assert spec.restart_required is (spec.key in RESTART_REQUIRED_KEYS)
    assert RESOLVE_ORDER == ("db", "env", "default")
    assert RESTART_REQUIRED_KEYS == frozenset({"media_storage_provider", "storage_backend"})


def test_bootstrap_only_keys_are_not_registered() -> None:
    assert not {
        "database_url",
        "auth_mode",
        "admin_password_hash",
        "settings_encryption_key",
    }.intersection(CONFIG_REGISTRY)


def test_config_package_has_no_router_or_composition_root_imports() -> None:
    config_directory = Path(__file__).resolve().parent.parent / "app" / "config"
    forbidden = (
        "from app.routers",
        "from app.main",
        "import app.routers",
        "import app.main",
    )
    for path in config_directory.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(token in source for token in forbidden), path
