from __future__ import annotations

import base64
import hashlib
import logging
import os
import struct
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.crypto import decrypt_value
from app.db.base import Base
from app.db.models import PlatformAdmin, WecomSuiteTicketState
from app.db.session import get_db
from app.main import _RedactOAuthCallbackQueryFilter, create_app
from app.routers import wecom_provider_instructions
from app.services import wecom_org_authorization
from app.services.wecom_org_authorization import (
    OfficialWecomOrganizationAuthorizationProvider,
    SuiteAccessTokenCache,
    WecomAuthorizationError,
)
from app.services.wecom_suite_ticket import (
    SuiteTicketUnavailable,
    SuiteTicketWriteOutcome,
    load_fresh_suite_ticket,
    store_suite_ticket,
    suite_ticket_freshness,
)
from app.settings import WecomThirdPartySettings

_SUITE_ID = "ww-provider-suite-test"
_CORP_ID = "ww-provider-corp-test"
_TOKEN = "ProviderCallbackToken350"
_KEY = bytes(range(32))
_AES_KEY = base64.b64encode(_KEY).decode("ascii").rstrip("=")
_PATH = "/api/wecom/third-party/instructions"

_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
try:
    _IS_POSTGRESQL = bool(
        _DATABASE_URL and make_url(_DATABASE_URL).get_backend_name() == "postgresql"
    )
except Exception:
    _IS_POSTGRESQL = False


def _signature(timestamp: str, nonce: str, payload: str) -> str:
    values = sorted([_TOKEN, timestamp, nonce, payload])
    return hashlib.sha1("".join(values).encode("utf-8")).hexdigest()


def _encrypt_envelope(message: bytes, receiver_id: str = _CORP_ID) -> str:
    plaintext = (
        b"r" * 16
        + struct.pack("!I", len(message))
        + message
        + receiver_id.encode("utf-8")
    )
    pad_len = 32 - len(plaintext) % 32
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_KEY[:16])).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def _instruction_xml(
    *,
    ticket: str = "provider-ticket-sensitive",
    suite_id: str = _SUITE_ID,
    info_type: str = "suite_ticket",
    source_timestamp: int,
) -> bytes:
    ticket_xml = (
        f"<SuiteTicket><![CDATA[{ticket}]]></SuiteTicket>"
        if info_type == "suite_ticket"
        else ""
    )
    return (
        "<xml>"
        f"<SuiteId><![CDATA[{suite_id}]]></SuiteId>"
        f"<InfoType><![CDATA[{info_type}]]></InfoType>"
        f"<TimeStamp>{source_timestamp}</TimeStamp>"
        f"{ticket_xml}"
        "</xml>"
    ).encode("utf-8")


def _post(
    client: TestClient,
    *,
    ticket: str = "provider-ticket-sensitive",
    suite_id: str = _SUITE_ID,
    receiver_id: str = _CORP_ID,
    info_type: str = "suite_ticket",
    source_timestamp: int | None = None,
    signature: str | None = None,
):
    current = source_timestamp or int(datetime.now(timezone.utc).timestamp())
    timestamp = str(current)
    nonce = "nonce-test"
    encrypted = _encrypt_envelope(
        _instruction_xml(
            ticket=ticket,
            suite_id=suite_id,
            info_type=info_type,
            source_timestamp=current,
        ),
        receiver_id,
    )
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    return client.post(
        _PATH,
        params={
            "msg_signature": signature or _signature(timestamp, nonce, encrypted),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=body,
        headers={"content-type": "application/xml"},
    )


@pytest.fixture()
def instruction_client(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("WECOM_THIRD_PARTY_SUITE_ID", _SUITE_ID)
    monkeypatch.setenv("WECOM_THIRD_PARTY_CORP_ID", _CORP_ID)
    monkeypatch.setenv("WECOM_THIRD_PARTY_INSTRUCTION_TOKEN", _TOKEN)
    monkeypatch.setenv(
        "WECOM_THIRD_PARTY_INSTRUCTION_ENCODING_AES_KEY",
        _AES_KEY,
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[PlatformAdmin.__table__, WecomSuiteTicketState.__table__],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            PlatformAdmin(
                id="platform-rnd350",
                email="platform-rnd350@example.test",
                password_hash=hash_password("platform-password"),
                role="superadmin",
                status="active",
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(wecom_provider_instructions.router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, factory


def test_url_verification_uses_independent_callback_configuration(instruction_client):
    client, _factory = instruction_client
    now = str(int(datetime.now(timezone.utc).timestamp()))
    nonce = "verify-nonce"
    encrypted = _encrypt_envelope(b"verification-plaintext")
    response = client.get(
        _PATH,
        params={
            "msg_signature": _signature(now, nonce, encrypted),
            "timestamp": now,
            "nonce": nonce,
            "echostr": encrypted,
        },
    )

    assert response.status_code == 200
    assert response.content == b"verification-plaintext"


def test_valid_ticket_is_encrypted_and_newest_signed_event_wins(
    instruction_client, caplog
):
    client, factory = instruction_client
    caplog.set_level(logging.INFO)
    first_timestamp = int(datetime.now(timezone.utc).timestamp())
    assert _post(client, source_timestamp=first_timestamp).text == "success"
    with factory() as db:
        first = db.get(WecomSuiteTicketState, _SUITE_ID)
        assert first is not None
        first_received_at = first.received_at
        assert first.ticket_encrypted != "provider-ticket-sensitive"
        assert decrypt_value(first.ticket_encrypted) == "provider-ticket-sensitive"

    duplicate = _post(client, source_timestamp=first_timestamp)
    newer = _post(
        client,
        ticket="newest-provider-ticket",
        source_timestamp=first_timestamp + 1,
    )
    replayed_old = _post(client, source_timestamp=first_timestamp)
    assert [duplicate.status_code, newer.status_code, replayed_old.status_code] == [200, 200, 200]
    with factory() as db:
        row = db.get(WecomSuiteTicketState, _SUITE_ID)
        assert decrypt_value(row.ticket_encrypted) == "newest-provider-ticket"
        assert row.source_timestamp == first_timestamp + 1
        assert row.received_at != first_received_at
    assert "provider-ticket-sensitive" not in caplog.text
    assert "newest-provider-ticket" not in caplog.text


@pytest.mark.parametrize(
    ("suite_id", "receiver_id", "expected_detail"),
    [
        ("another-suite", _CORP_ID, "Suite ID mismatch"),
        (_SUITE_ID, "another-corp", "Corp ID mismatch"),
        (_SUITE_ID, _SUITE_ID, "Corp ID mismatch"),
    ],
)
def test_wrong_suite_id_or_corp_id_fails_closed(
    instruction_client, suite_id, receiver_id, expected_detail
):
    client, factory = instruction_client
    response = _post(client, suite_id=suite_id, receiver_id=receiver_id)
    assert response.status_code == 403
    assert response.json() == {"detail": expected_detail}
    with factory() as db:
        assert db.query(WecomSuiteTicketState).count() == 0


def test_get_url_verification_rejects_suite_id_as_receiver(instruction_client):
    client, _factory = instruction_client
    now = str(int(datetime.now(timezone.utc).timestamp()))
    nonce = "verify-nonce-wrong-receiver"
    encrypted = _encrypt_envelope(b"verification-plaintext", receiver_id=_SUITE_ID)
    response = client.get(
        _PATH,
        params={
            "msg_signature": _signature(now, nonce, encrypted),
            "timestamp": now,
            "nonce": nonce,
            "echostr": encrypted,
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Corp ID mismatch"}


def test_missing_corp_id_configuration_fails_closed_on_get_and_post(
    instruction_client, monkeypatch
):
    client, factory = instruction_client
    monkeypatch.delenv("WECOM_THIRD_PARTY_CORP_ID")

    now = str(int(datetime.now(timezone.utc).timestamp()))
    nonce = "verify-nonce-missing-corp"
    encrypted = _encrypt_envelope(b"verification-plaintext")
    get_response = client.get(
        _PATH,
        params={
            "msg_signature": _signature(now, nonce, encrypted),
            "timestamp": now,
            "nonce": nonce,
            "echostr": encrypted,
        },
    )
    assert get_response.status_code == 503
    assert get_response.json() == {"detail": "Callback unavailable"}

    post_response = _post(client)
    assert post_response.status_code == 503
    assert post_response.json() == {"detail": "Callback unavailable"}
    with factory() as db:
        assert db.query(WecomSuiteTicketState).count() == 0


def test_invalid_signature_stale_request_and_missing_key_never_write_or_leak(
    instruction_client, monkeypatch, caplog
):
    client, factory = instruction_client
    caplog.set_level(logging.INFO)
    invalid_signature = _post(client, signature="invalid")
    stale = _post(
        client,
        source_timestamp=int(datetime.now(timezone.utc).timestamp()) - 301,
    )
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY")
    missing_key = _post(client, ticket="never-log-this-ticket")

    assert invalid_signature.status_code == 403
    assert stale.status_code == 400
    assert missing_key.status_code == 503
    combined = missing_key.text + "\n" + caplog.text
    assert "never-log-this-ticket" not in combined
    assert "provider-ticket-sensitive" not in combined
    with factory() as db:
        assert db.query(WecomSuiteTicketState).count() == 0


def test_non_ticket_instruction_is_safely_acknowledged_without_storage(instruction_client):
    client, factory = instruction_client
    response = _post(client, info_type="change_auth")
    assert response.status_code == 200
    assert response.text == "success"
    with factory() as db:
        assert db.query(WecomSuiteTicketState).count() == 0


def test_archive_callback_credentials_cannot_configure_instruction_callback(
    instruction_client, monkeypatch
):
    client, factory = instruction_client
    monkeypatch.delenv("WECOM_THIRD_PARTY_INSTRUCTION_TOKEN")
    monkeypatch.delenv("WECOM_THIRD_PARTY_INSTRUCTION_ENCODING_AES_KEY")
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", _TOKEN)
    monkeypatch.setenv("WECOM_CALLBACK_ENCODING_AES_KEY", _AES_KEY)

    response = _post(client)
    assert response.status_code == 503
    assert response.json() == {"detail": "Callback unavailable"}
    with factory() as db:
        assert db.query(WecomSuiteTicketState).count() == 0


def test_oversized_ticket_is_request_error_not_storage_failure(instruction_client):
    client, factory = instruction_client
    response = _post(client, ticket="x" * 513)
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid callback request"}
    with factory() as db:
        assert db.query(WecomSuiteTicketState).count() == 0


def test_freshness_has_exact_warning_and_expiry_boundaries(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[WecomSuiteTicketState.__table__])
    factory = sessionmaker(bind=engine)
    observed = datetime(2026, 8, 13, tzinfo=timezone.utc)
    with factory() as db:
        assert store_suite_ticket(
            db,
            suite_id=_SUITE_ID,
            ticket="fresh-ticket",
            source_timestamp=1,
            received_at=observed,
        ) is SuiteTicketWriteOutcome.STORED
        assert suite_ticket_freshness(
            db, _SUITE_ID, now=observed + timedelta(seconds=1199)
        ).state == "fresh"
        warning = suite_ticket_freshness(
            db, _SUITE_ID, now=observed + timedelta(seconds=1200)
        )
        assert warning.state == "warning" and warning.requires_alert
        assert load_fresh_suite_ticket(
            db, _SUITE_ID, now=observed + timedelta(seconds=1799)
        ) == "fresh-ticket"
        expired = suite_ticket_freshness(
            db, _SUITE_ID, now=observed + timedelta(seconds=1800)
        )
        assert expired.state == "expired" and expired.age_seconds == 1800
        with pytest.raises(SuiteTicketUnavailable):
            load_fresh_suite_ticket(
                db,
                _SUITE_ID,
                now=observed + timedelta(seconds=1800),
            )


@pytest.mark.skipif(not _IS_POSTGRESQL, reason="PostgreSQL DATABASE_URL not set")
def test_concurrent_first_deliveries_keep_one_newest_postgresql_ticket(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine(_DATABASE_URL)
    if not inspect(engine).has_table("wecom_suite_ticket_states"):
        pytest.skip("Database is not migrated through RND-350")
    suite_id = f"ww-rnd350-{uuid.uuid4()}"

    def write(source_timestamp: int):
        with Session(engine) as db:
            return store_suite_ticket(
                db,
                suite_id=suite_id,
                ticket=f"ticket-{source_timestamp}",
                source_timestamp=source_timestamp,
            )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(write, range(1, 9)))
        assert len(outcomes) == 8
        with Session(engine) as db:
            rows = (
                db.query(WecomSuiteTicketState)
                .filter(WecomSuiteTicketState.suite_id == suite_id)
                .all()
            )
            assert len(rows) == 1
            assert rows[0].source_timestamp == 8
            assert decrypt_value(rows[0].ticket_encrypted) == "ticket-8"
    finally:
        with Session(engine) as db:
            db.query(WecomSuiteTicketState).filter(
                WecomSuiteTicketState.suite_id == suite_id
            ).delete(synchronize_session=False)
            db.commit()
        engine.dispose()


def test_suite_token_cache_is_single_flight_bounded_and_never_returns_expired(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(wecom_org_authorization.time, "monotonic", lambda: clock[0])
    cache = SuiteAccessTokenCache()
    refresh_started = Event()
    release_refresh = Event()
    count_lock = Lock()
    calls = 0

    def refresh():
        nonlocal calls
        with count_lock:
            calls += 1
        refresh_started.set()
        release_refresh.wait(timeout=2)
        return "token-one", 99999

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(cache.get, _SUITE_ID, refresh) for _ in range(8)]
        assert refresh_started.wait(timeout=2)
        release_refresh.set()
        assert [future.result(timeout=2) for future in futures] == ["token-one"] * 8
    assert calls == 1

    clock[0] = 7199.0
    assert cache.get(_SUITE_ID, lambda: ("token-two", 10)) == "token-two"
    clock[0] = 7208.0

    def failed_refresh():
        raise WecomAuthorizationError("provider unavailable")

    # One second remains, so a refresh failure may use the unexpired value.
    assert cache.get(_SUITE_ID, failed_refresh) == "token-two"
    clock[0] = 7210.0
    with pytest.raises(WecomAuthorizationError):
        cache.get(_SUITE_ID, failed_refresh)


def test_authorization_provider_ignores_static_ticket_environment(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("WECOM_THIRD_PARTY_SUITE_TICKET", "obsolete-static-ticket")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[WecomSuiteTicketState.__table__])
    factory = sessionmaker(bind=engine)
    settings = WecomThirdPartySettings(
        wecom_third_party_suite_id=_SUITE_ID,
        wecom_third_party_suite_secret="suite-secret",
        wecom_third_party_callback_url="https://console.example.test/callback",
    )
    with factory() as db:
        with pytest.raises(WecomAuthorizationError):
            OfficialWecomOrganizationAuthorizationProvider(settings, db)
        store_suite_ticket(
            db,
            suite_id=_SUITE_ID,
            ticket="database-ticket",
            source_timestamp=1,
        )
        provider = OfficialWecomOrganizationAuthorizationProvider(
            settings,
            db,
            token_cache=SuiteAccessTokenCache(),
        )
        assert load_fresh_suite_ticket(db, _SUITE_ID) == "database-ticket"
        assert not hasattr(provider.settings, "wecom_third_party_suite_ticket")


def test_provider_network_failure_does_not_chain_sensitive_request_url(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[WecomSuiteTicketState.__table__])
    factory = sessionmaker(bind=engine)
    settings = WecomThirdPartySettings(
        wecom_third_party_suite_id=_SUITE_ID,
        wecom_third_party_suite_secret="suite-secret",
        wecom_third_party_callback_url="https://console.example.test/callback",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_suite_token"):
            return httpx.Response(
                200,
                json={"suite_access_token": "sensitive-suite-token", "expires_in": 7200},
            )
        raise httpx.ConnectError("provider unavailable", request=request)

    with factory() as db:
        store_suite_ticket(
            db,
            suite_id=_SUITE_ID,
            ticket="database-ticket",
            source_timestamp=1,
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            provider = OfficialWecomOrganizationAuthorizationProvider(
                settings,
                db,
                http_client,
                SuiteAccessTokenCache(),
            )
            with pytest.raises(WecomAuthorizationError) as caught:
                provider.exchange("authorization-code")
    assert caught.value.__cause__ is None
    assert "sensitive-suite-token" not in str(caught.value)


def test_status_is_platform_protected_and_callback_query_is_redacted(instruction_client):
    client, _factory = instruction_client
    status = client.get("/api/platform/wecom/third-party/suite-ticket-status")
    assert status.status_code == 401
    assert _post(client).status_code == 200
    authorized = client.get(
        "/api/platform/wecom/third-party/suite-ticket-status",
        auth=("platform-rnd350@example.test", "platform-password"),
    )
    assert authorized.status_code == 200
    assert authorized.headers["cache-control"] == "no-store"
    assert authorized.json()["state"] == "fresh"
    for forbidden in (_SUITE_ID, "provider-ticket-sensitive", "ticket_digest"):
        assert forbidden not in authorized.text

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1",
            "POST",
            _PATH + "?msg_signature=secret&timestamp=1&nonce=2",
            "1.1",
            200,
        ),
        None,
    )
    _RedactOAuthCallbackQueryFilter().filter(record)
    assert record.args[2] == _PATH + "?[REDACTED]"


def test_routes_keep_instruction_oauth_and_archive_callbacks_distinct():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert {
        "/api/wecom/third-party/instructions",
        "/api/auth/wecom/third-party/callback",
        "/api/wecom/archive/events",
    } <= paths
