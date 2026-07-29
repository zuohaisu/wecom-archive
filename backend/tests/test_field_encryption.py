"""RND-333 field encryption and app_secret re-encryption coverage."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.crypto import (
    FieldEncryptionConfigurationError,
    decrypt_value,
    encrypt_value,
    is_encrypted,
)
from app.db.base import Base
from app.db.models import Tenant, TenantWecomConfig
from scripts.reencrypt_app_secrets_once import reencrypt_app_secrets


@pytest.fixture()
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Use a newly generated process-local key; never a fixture credential."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", key)
    return key


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine, tables=[Tenant.__table__, TenantWecomConfig.__table__]
    )
    session = Session(engine)
    yield session
    session.close()


def _config(config_id: str, app_secret: str) -> TenantWecomConfig:
    return TenantWecomConfig(
        id=config_id,
        tenant_id=f"tenant-{config_id}",
        corp_id=f"corp-{config_id}",
        agent_id="1000001",
        app_secret=app_secret,
    )


def test_encrypt_decrypt_are_symmetric(encryption_key: str) -> None:
    plain = "credential value with unicode 密钥"

    cipher = encrypt_value(plain)

    assert cipher != plain
    assert is_encrypted(cipher)
    assert not is_encrypted(plain)
    assert decrypt_value(cipher) == plain


def test_encrypt_without_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = "must-not-be-returned"
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)

    with pytest.raises(FieldEncryptionConfigurationError, match="FIELD_ENCRYPTION_KEY") as exc:
        encrypt_value(plain)

    assert plain not in str(exc.value)


def test_app_secret_accessor_persists_ciphertext_and_decrypts(
    db: Session, encryption_key: str
) -> None:
    plain = "wecom-app-secret"
    config = _config("config-1", "")
    config.set_app_secret(plain)
    db.add(config)
    db.commit()

    stored = (
        db.query(TenantWecomConfig.app_secret)
        .filter(TenantWecomConfig.id == config.id)
        .scalar()
    )
    assert stored != plain
    assert config.decrypted_app_secret == plain


def test_reencryption_dry_run_and_repeat_are_idempotent(
    db: Session, encryption_key: str
) -> None:
    plaintext_config = _config("config-plain", "historical-secret")
    encrypted_config = _config("config-encrypted", "")
    encrypted_config.set_app_secret("already-encrypted")
    db.add_all([plaintext_config, encrypted_config])
    db.commit()

    original = plaintext_config.app_secret
    assert reencrypt_app_secrets(db, dry_run=True) == (1, 1)
    db.rollback()
    assert plaintext_config.app_secret == original

    assert reencrypt_app_secrets(db, dry_run=False) == (1, 1)
    db.commit()
    db.expire_all()
    assert plaintext_config.app_secret != original
    assert plaintext_config.decrypted_app_secret == "historical-secret"

    assert reencrypt_app_secrets(db, dry_run=False) == (0, 2)
