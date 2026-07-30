"""RND-332 key-provider, tenant isolation, and decrypt-audit coverage."""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import KeyVersion, Tenant
from app.key_provider import KmsEnvelopeKeyProvider, LocalFileKeyProvider, get_key_provider
from app.services.decrypt_worker import run_decrypt_once
from tests.fakes import (
    FakeWecomSdk,
    _TENANT_A,
    generate_test_rsa_keypair,
    insert_archive_message,
    insert_tenant,
    rsa_encrypt_key_b64,
    worker_db,  # noqa: F401 -- fixture discovery
)


def _pem(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _key_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Tenant.__table__.create(engine)
    KeyVersion.__table__.create(engine)
    return Session(engine)


def _tenant(db: Session, tenant_id: str) -> None:
    db.add(Tenant(id=tenant_id, name=tenant_id, slug=tenant_id))
    db.flush()


def test_local_file_provider_loads_tenant_scoped_key(tmp_path) -> None:
    db = _key_db()
    private_key, _ = generate_test_rsa_keypair()
    path = tmp_path / "tenant-key.pem"
    path.write_text(_pem(private_key))
    _tenant(db, "tenant-a")
    db.add(KeyVersion(tenant_id="tenant-a", publickey_ver=1, key_alias="primary", private_key_path=str(path)))
    db.commit()

    assert LocalFileKeyProvider(db).get_private_key("tenant-a", 1).private_numbers() == private_key.private_numbers()


def test_kms_envelope_provider_encrypts_at_rest_and_loads_in_memory(monkeypatch) -> None:
    db = _key_db()
    private_key, _ = generate_test_rsa_keypair()
    pem = _pem(private_key)
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    _tenant(db, "tenant-a")
    db.add(KeyVersion(tenant_id="tenant-a", publickey_ver=1, key_alias="managed", private_key_path="placeholder"))
    db.commit()

    provider = KmsEnvelopeKeyProvider(db)
    provider.store_private_key("tenant-a", 1, pem)
    stored = db.execute(text("SELECT private_key_path FROM key_versions")).scalar_one()
    assert stored != pem
    assert "BEGIN PRIVATE KEY" not in stored
    assert provider.get_private_key("tenant-a", 1).private_numbers() == private_key.private_numbers()


def test_same_publickey_version_is_allowed_for_two_tenants_but_not_one_tenant() -> None:
    db = _key_db()
    _tenant(db, "tenant-a")
    _tenant(db, "tenant-b")
    db.add_all(
        [
            KeyVersion(tenant_id="tenant-a", publickey_ver=1, key_alias="a", private_key_path="/a"),
            KeyVersion(tenant_id="tenant-b", publickey_ver=1, key_alias="b", private_key_path="/b"),
        ]
    )
    db.commit()
    assert db.query(KeyVersion).filter_by(publickey_ver=1).count() == 2

    db.add(KeyVersion(tenant_id="tenant-a", publickey_ver=1, key_alias="duplicate", private_key_path="/other"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    else:
        raise AssertionError("same tenant must not reuse a publickey_ver")


def test_provider_factory_defaults_to_local_file(monkeypatch, tmp_path) -> None:
    db = _key_db()
    private_key, _ = generate_test_rsa_keypair()
    path = tmp_path / "legacy.pem"
    path.write_text(_pem(private_key))
    monkeypatch.delenv("KEY_PROVIDER", raising=False)
    assert get_key_provider(db, legacy_private_key_path=str(path)).get_private_key("unregistered", 1).private_numbers() == private_key.private_numbers()


def test_decrypt_batch_writes_key_version_audit_record(worker_db, monkeypatch) -> None:
    worker_db.execute(
        text(
            "CREATE TABLE audit_logs (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "admin_user_id TEXT, action TEXT NOT NULL, object_type TEXT NOT NULL, "
            "object_id TEXT, detail JSON, created_at DATETIME NOT NULL)"
        )
    )
    worker_db.commit()
    original_private_key, public_key = generate_test_rsa_keypair()
    # The KMS envelope provider's returned in-memory key is passed through the
    # unchanged worker seam for a real decrypt batch.
    key_db = _key_db()
    _tenant(key_db, _TENANT_A)
    key_db.add(
        KeyVersion(
            tenant_id=_TENANT_A,
            publickey_ver=1,
            key_alias="managed",
            private_key_path="placeholder",
        )
    )
    key_db.commit()
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    provider = KmsEnvelopeKeyProvider(key_db)
    provider.store_private_key(_TENANT_A, 1, _pem(original_private_key))
    private_key = provider.get_private_key(_TENANT_A, 1)
    insert_tenant(worker_db, _TENANT_A)
    insert_archive_message(
        worker_db,
        tenant_id=_TENANT_A,
        decrypt_status="pending",
        publickey_ver=1,
        encrypt_random_key=rsa_encrypt_key_b64(public_key, "symmetric-key"),
        encrypt_chat_msg="payload",
    )
    sdk = FakeWecomSdk()
    sdk.set_decrypt_response("payload", {"msgtype": "text", "from": "sender", "tolist": [], "roomid": "", "msgtime": 1, "text": {"content": "ok"}})

    run_decrypt_once(worker_db, _TENANT_A, "fake-lib", private_key, 1, sdk=sdk)

    row = worker_db.execute(text("SELECT tenant_id, action, object_type, object_id, detail FROM audit_logs")).one()
    assert row.tenant_id == _TENANT_A
    assert (row.action, row.object_type, row.object_id) == ("decrypt.completed", "key_version", "1")
    assert '"publickey_ver": 1' in row.detail
