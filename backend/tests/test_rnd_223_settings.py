"""
RND-223: app/settings.py domain Typed Settings.

Covers every field's default value (mirroring the os.getenv/os.environ.get
call it replaces) and the lazy-read contract the rest of the test suite
depends on: monkeypatch.setenv() before a factory call takes effect, and a
second factory call after the env changes again must reflect the new value
-- i.e. no lru_cache, no module-level singleton, no import-time snapshot.

Qiniu's fail-loud-on-missing-required-field behavior lives in
app.media_storage (_require_qiniu_env), not in app.settings itself -- that
remains an integration-level assertion in tests/test_qiniu_provider_factory.py.
"""

from __future__ import annotations

from app.settings import (
    AuthSettings,
    DatabaseSettings,
    MediaStorageSettings,
    ThumbnailSettings,
    VoiceTranscodeSettings,
    WecomCallbackSettings,
    WecomOAuthSettings,
    get_auth_settings,
    get_database_settings,
    get_media_storage_settings,
    get_thumbnail_settings,
    get_voice_transcode_settings,
    get_wecom_callback_settings,
    get_wecom_oauth_settings,
)


# ---------------------------------------------------------------------------
# Defaults (unset env -> matches the original os.getenv/os.environ.get default)
# ---------------------------------------------------------------------------


def test_database_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get_database_settings().database_url == ""


def test_auth_settings_defaults(monkeypatch) -> None:
    for name in ("AUTH_MODE", "APP_ENV", "ADMIN_USERNAME", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(name, raising=False)
    settings = get_auth_settings()
    assert settings.auth_mode == ""
    assert settings.app_env == "development"
    assert settings.admin_username == ""
    assert settings.admin_password_hash == ""


def test_wecom_oauth_settings_defaults(monkeypatch) -> None:
    for name in (
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_OAUTH_SECRET",
        "ADMIN_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_wecom_oauth_settings()
    assert settings.wecom_corp_id == ""
    assert settings.wecom_agent_id == ""
    assert settings.wecom_oauth_secret == ""
    assert settings.admin_domain == ""


def test_wecom_callback_settings_defaults(monkeypatch) -> None:
    for name in (
        "WECOM_CALLBACK_TOKEN",
        "WECOM_CALLBACK_ENCODING_AES_KEY",
        "WECOM_CORP_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_wecom_callback_settings()
    assert settings.wecom_callback_token == ""
    assert settings.wecom_callback_encoding_aes_key == ""
    assert settings.wecom_corp_id == ""


def test_media_storage_settings_defaults(monkeypatch) -> None:
    for name in (
        "MEDIA_STORAGE_PROVIDER",
        "STORAGE_BACKEND",
        "STORAGE_LOCAL_PATH",
        "QINIU_ACCESS_KEY",
        "QINIU_SECRET_KEY",
        "QINIU_BUCKET",
        "QINIU_DOMAIN",
        "QINIU_REGION",
        "QINIU_TIMEOUT_SECONDS",
        "MEDIA_SIGNED_URL_TTL_SECONDS",
        "MEDIA_SIGNED_URL_WINDOW_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_media_storage_settings()
    assert settings.media_storage_provider is None
    assert settings.storage_backend is None
    assert settings.storage_local_path == ""
    assert settings.qiniu_access_key == ""
    assert settings.qiniu_secret_key == ""
    assert settings.qiniu_bucket == ""
    assert settings.qiniu_domain == ""
    assert settings.qiniu_region == ""
    assert settings.qiniu_timeout_seconds == ""
    assert settings.media_signed_url_ttl_seconds == ""
    assert settings.media_signed_url_window_seconds == ""


def test_thumbnail_settings_defaults(monkeypatch) -> None:
    for name in (
        "MEDIA_THUMBNAIL_ENABLED",
        "MEDIA_THUMBNAIL_MAX_EDGE",
        "MEDIA_THUMBNAIL_JPEG_QUALITY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_thumbnail_settings()
    assert settings.media_thumbnail_enabled == ""
    assert settings.media_thumbnail_max_edge == ""
    assert settings.media_thumbnail_jpeg_quality == ""


def test_voice_transcode_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_TRANSCODE_ENABLED", raising=False)
    assert get_voice_transcode_settings().voice_transcode_enabled == ""


# ---------------------------------------------------------------------------
# Raw pass-through: no implicit strip/lower at the Settings layer -- matches
# os.getenv/os.environ.get returning the literal string, with any
# strip()/lower() applied by the original call site, not by this module.
# ---------------------------------------------------------------------------


def test_values_are_not_stripped_or_lowered_by_settings(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "  WeCom  ")
    assert get_auth_settings().auth_mode == "  WeCom  "

    monkeypatch.setenv("DATABASE_URL", "  sqlite:///x.db  ")
    assert get_database_settings().database_url == "  sqlite:///x.db  "

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", " Qiniu_Kodo ")
    assert get_media_storage_settings().media_storage_provider == " Qiniu_Kodo "


def test_present_but_empty_env_var_is_empty_string_not_default(monkeypatch) -> None:
    """A var explicitly set to "" must behave like os.getenv(name, default)
    called with that same empty string present -- i.e. yield "", not silently
    fall back to the class-level default (APP_ENV's "development")."""
    monkeypatch.setenv("APP_ENV", "")
    assert get_auth_settings().app_env == ""


# ---------------------------------------------------------------------------
# Lazy-read contract: no caching across factory calls (the #0.2 red line).
# ---------------------------------------------------------------------------


def test_database_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///first.db")
    assert get_database_settings().database_url == "sqlite:///first.db"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///second.db")
    assert get_database_settings().database_url == "sqlite:///second.db"


def test_auth_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "wecom")
    assert get_auth_settings().auth_mode == "wecom"

    monkeypatch.setenv("AUTH_MODE", "password")
    assert get_auth_settings().auth_mode == "password"


def test_wecom_oauth_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("WECOM_CORP_ID", "corp-a")
    assert get_wecom_oauth_settings().wecom_corp_id == "corp-a"

    monkeypatch.setenv("WECOM_CORP_ID", "corp-b")
    assert get_wecom_oauth_settings().wecom_corp_id == "corp-b"


def test_wecom_callback_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", "token-a")
    assert get_wecom_callback_settings().wecom_callback_token == "token-a"

    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", "token-b")
    assert get_wecom_callback_settings().wecom_callback_token == "token-b"


def test_media_storage_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "local")
    assert get_media_storage_settings().media_storage_provider == "local"

    monkeypatch.setenv("MEDIA_STORAGE_PROVIDER", "qiniu_kodo")
    assert get_media_storage_settings().media_storage_provider == "qiniu_kodo"


def test_thumbnail_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_THUMBNAIL_MAX_EDGE", "100")
    assert get_thumbnail_settings().media_thumbnail_max_edge == "100"

    monkeypatch.setenv("MEDIA_THUMBNAIL_MAX_EDGE", "500")
    assert get_thumbnail_settings().media_thumbnail_max_edge == "500"


def test_voice_transcode_settings_has_no_process_level_cache(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_TRANSCODE_ENABLED", "false")
    assert get_voice_transcode_settings().voice_transcode_enabled == "false"
    monkeypatch.setenv("VOICE_TRANSCODE_ENABLED", "true")
    assert get_voice_transcode_settings().voice_transcode_enabled == "true"


def test_factories_are_not_memoized_functions() -> None:
    """Guard against a future edit accidentally adding @lru_cache (or an
    equivalent memoizing decorator) to any factory -- each must be a plain
    function that builds a fresh BaseSettings instance every call."""
    for factory, cls in (
        (get_database_settings, DatabaseSettings),
        (get_auth_settings, AuthSettings),
        (get_wecom_oauth_settings, WecomOAuthSettings),
        (get_wecom_callback_settings, WecomCallbackSettings),
        (get_media_storage_settings, MediaStorageSettings),
        (get_thumbnail_settings, ThumbnailSettings),
        (get_voice_transcode_settings, VoiceTranscodeSettings),
    ):
        assert not hasattr(factory, "cache_clear"), (
            f"{factory.__name__} appears to be memoized (has cache_clear) -- "
            "RND-223 requires a fresh instance per call"
        )
        first = factory()
        second = factory()
        assert isinstance(first, cls)
        assert first is not second
