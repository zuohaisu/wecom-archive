"""RND-245 configuration-secret Fernet helper coverage."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config.crypto import (
    SettingsDecryptionError,
    SettingsEncryptionConfigurationError,
    decrypt_value,
    encrypt_value,
    is_encrypted,
    mask,
)
from app.settings import get_settings_encryption_settings


@pytest.fixture()
def settings_encryption_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Use a newly generated process-local key; never a fixture credential."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    return key


def test_encrypt_decrypt_are_symmetric_for_unicode(
    settings_encryption_key: str,
) -> None:
    plain = "credential value with unicode 配置密钥!@#$%^&*()"

    cipher = encrypt_value(plain)

    assert cipher != plain
    assert is_encrypted(cipher)
    assert decrypt_value(cipher) == plain


def test_operations_without_key_fail_closed_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = "must-not-be-returned"
    cipher = "must-not-be-decrypted"
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)

    with pytest.raises(SettingsEncryptionConfigurationError) as encrypt_exc:
        encrypt_value(plain)
    with pytest.raises(SettingsEncryptionConfigurationError) as decrypt_exc:
        decrypt_value(cipher)

    assert "SETTINGS_ENCRYPTION_KEY" in str(encrypt_exc.value)
    assert plain not in str(encrypt_exc.value)
    assert cipher not in str(decrypt_exc.value)


def test_decryption_error_does_not_expose_ciphertext(
    settings_encryption_key: str,
) -> None:
    cipher = "not-a-valid-fernet-token"

    with pytest.raises(SettingsDecryptionError) as exc:
        decrypt_value(cipher)

    assert cipher not in str(exc.value)


def test_is_encrypted_distinguishes_fernet_tokens_from_plaintext(
    settings_encryption_key: str,
) -> None:
    assert is_encrypted(encrypt_value("secret"))
    assert not is_encrypted("plain secret")
    assert not is_encrypted("gAAAAA-not-base64")
    assert not is_encrypted("")
    assert not is_encrypted(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "****"),
        ("a", "****"),
        ("ab12", "****"),
        ("配置密钥", "****"),
        ("abcde", "****bcde"),
        ("前缀配置密钥", "****配置密钥"),
        ("secret-ab12", "****ab12"),
    ],
)
def test_mask_exposes_only_the_last_four_characters_for_long_values(
    value: str, expected: str
) -> None:
    assert mask(value) == expected


def test_settings_encryption_settings_reads_environment_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "first-test-value")
    assert get_settings_encryption_settings().settings_encryption_key == "first-test-value"

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "second-test-value")
    assert get_settings_encryption_settings().settings_encryption_key == "second-test-value"
