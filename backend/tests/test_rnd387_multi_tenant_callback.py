"""RND-387 multi-tenant callback resolution and dispatch contracts.

The archive callback authenticates against the env-scoped single-corp
candidate first (zero regression), then per-tenant stored callback
credentials resolved by the plaintext outer ToUserName, then any remaining
active configs as a defensive fallback.  The decrypted envelope's receiver
id is the authoritative corp match.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sqlalchemy.orm import Session

from app.routers import wecom_events
from app.services.tenant_callback_resolution import (
    CallbackCredentialCandidate,
    callback_candidates,
)
from app.services.wecom_callback_crypto import CallbackInputError, extract_outer_corp_id
from tests.fakes import (
    insert_tenant,
    insert_tenant_wecom_config,
    make_worker_engine,
    worker_db,  # noqa: F401 -- pytest fixture, must be imported to be discovered
)

_TOKEN_A = "token-a-sentinel"
_TOKEN_B = "token-b-sentinel"
_CORP_A = "corp-a-sentinel"
_CORP_B = "corp-b-sentinel"
_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"
_KEY = bytes(range(32))
_AES_KEY = base64.b64encode(_KEY).decode("ascii").rstrip("=")
_PATH = "/api/wecom/archive/events"


def _signature(token: str, timestamp: str, nonce: str, payload: str) -> str:
    return hashlib.sha1(
        "".join(sorted([token, timestamp, nonce, payload])).encode("utf-8")
    ).hexdigest()


def _encrypt(plaintext: bytes, aes_key: bytes = _KEY) -> str:
    pad_len = 32 - len(plaintext) % 32
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _envelope(message: bytes, corp_id: bytes) -> bytes:
    return b"r" * 16 + struct.pack("!I", len(message)) + message + corp_id


def _event_xml(corp_id: str = _CORP_B) -> bytes:
    return (
        f"<xml><ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        "<MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[change_archive]]></Event></xml>"
    ).encode()


def _post(client: TestClient, body: bytes, payload: str, *, token: str = _TOKEN_B):
    timestamp, nonce = "1700000100", "rnd387-nonce"
    return client.post(
        _PATH,
        params={
            "msg_signature": _signature(token, timestamp, nonce, payload),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=body,
        follow_redirects=False,
    )


def _post_encrypted(client: TestClient, message: bytes, *, token: str = _TOKEN_B, aes_key: bytes = _KEY):
    payload = _encrypt(_envelope(message, _CORP_B.encode()), aes_key)
    body = f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode()
    return _post(client, body, payload, token=token)


def _get(client: TestClient, echostr: str, *, token: str = _TOKEN_B):
    timestamp, nonce = "1700000101", "rnd387-get-nonce"
    return client.get(
        _PATH,
        params={
            "msg_signature": _signature(token, timestamp, nonce, echostr),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": echostr,
        },
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# extract_outer_corp_id — pure parsing
# ---------------------------------------------------------------------------


def test_extract_outer_corp_id_reads_plaintext_receiver() -> None:
    assert extract_outer_corp_id(_event_xml(_CORP_B)) == _CORP_B


def test_extract_outer_corp_id_requires_cdata_form() -> None:
    # Mirrors extract_encrypt's contract: WeCom always emits CDATA, so a
    # plain-text value is treated as absent rather than trusted.
    assert extract_outer_corp_id(b"<xml><ToUserName>corp-plain</ToUserName></xml>") is None


def test_extract_outer_corp_id_missing_returns_none() -> None:
    assert extract_outer_corp_id(b"<xml><MsgType>event</MsgType></xml>") is None


def test_extract_outer_corp_id_rejects_doctype_and_malformed() -> None:
    with pytest.raises(CallbackInputError):
        extract_outer_corp_id(b"<!DOCTYPE foo><xml><ToUserName>corp</ToUserName></xml>")
    with pytest.raises(CallbackInputError):
        extract_outer_corp_id(b"<xml><ToUserName>unterminated")


# ---------------------------------------------------------------------------
# callback_candidates — ordering and failure isolation
# ---------------------------------------------------------------------------


@pytest.fixture()
def field_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))


def _config_row(db, tenant_id: str, corp_id: str, token: str) -> None:
    config = insert_tenant_wecom_config(db, tenant_id, corp_id)
    config.set_callback_credentials(token, _AES_KEY)
    db.commit()


def test_candidates_env_first_then_corp_matched_then_defensive(
    worker_db, monkeypatch, field_encryption_key
) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_tenant(worker_db, _TENANT_B)
    _config_row(worker_db, _TENANT_A, _CORP_A, _TOKEN_A)
    _config_row(worker_db, _TENANT_B, _CORP_B, _TOKEN_B)

    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", "env-token")
    monkeypatch.setenv("WECOM_CALLBACK_ENCODING_AES_KEY", _AES_KEY)
    monkeypatch.setenv("WECOM_CORP_ID", "corp-env")

    resolution = callback_candidates(worker_db, _CORP_B)

    assert [c.tenant_id for c in resolution.candidates] == [None, _TENANT_B, _TENANT_A]
    assert resolution.candidates[0].token == "env-token"
    assert resolution.candidates[0].corp_id == "corp-env"
    assert resolution.candidates[1] == CallbackCredentialCandidate(
        token=_TOKEN_B, aes_key=_KEY, corp_id=_CORP_B, tenant_id=_TENANT_B
    )
    assert resolution.env_configuration_error is False


def test_candidates_no_env_uses_stored_rows_ordered_by_corp_match(
    worker_db, field_encryption_key,
) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_tenant(worker_db, _TENANT_B)
    _config_row(worker_db, _TENANT_A, _CORP_A, _TOKEN_A)
    _config_row(worker_db, _TENANT_B, _CORP_B, _TOKEN_B)

    resolution = callback_candidates(worker_db, _CORP_B)

    assert [c.tenant_id for c in resolution.candidates] == [_TENANT_B, _TENANT_A]
    assert resolution.env_configuration_error is False


def test_candidates_inactive_config_is_never_a_candidate(
    worker_db, field_encryption_key
) -> None:
    insert_tenant(worker_db, _TENANT_B)
    config = insert_tenant_wecom_config(worker_db, _TENANT_B, _CORP_B, is_active=False)
    config.set_callback_credentials(_TOKEN_B, _AES_KEY)
    worker_db.commit()

    resolution = callback_candidates(worker_db, _CORP_B)
    assert resolution.candidates == ()


def test_candidates_unreadable_credentials_are_skipped_not_fatal(
    worker_db, monkeypatch, field_encryption_key
) -> None:
    insert_tenant(worker_db, _TENANT_A)
    insert_tenant(worker_db, _TENANT_B)
    _config_row(worker_db, _TENANT_B, _CORP_B, _TOKEN_B)
    config_a = insert_tenant_wecom_config(worker_db, _TENANT_A, _CORP_A)
    # Plaintext "secret" app_secret plus a corrupt callback ciphertext: the
    # row has callback columns set, so has_callback_credentials is True, but
    # decryption fails and the row must be skipped without aborting.
    config_a.callback_token_encrypted = "not-valid-ciphertext"
    config_a.callback_encoding_aes_key_encrypted = "not-valid-ciphertext"
    worker_db.commit()

    resolution = callback_candidates(worker_db, _CORP_A)

    assert [c.tenant_id for c in resolution.candidates] == [_TENANT_B]
    assert resolution.env_configuration_error is False


def test_candidates_db_none_degrades_to_env_only(monkeypatch) -> None:
    resolution = callback_candidates(None, None)
    assert resolution.candidates == ()
    assert resolution.env_configuration_error is False


def test_candidates_partial_env_raises_configuration_error_signal(
    worker_db, monkeypatch, field_encryption_key
) -> None:
    insert_tenant(worker_db, _TENANT_B)
    _config_row(worker_db, _TENANT_B, _CORP_B, _TOKEN_B)
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", "env-token")
    monkeypatch.delenv("WECOM_CALLBACK_ENCODING_AES_KEY", raising=False)
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)

    resolution = callback_candidates(worker_db, _CORP_B)

    assert [c.tenant_id for c in resolution.candidates] == [_TENANT_B]
    assert resolution.env_configuration_error is True


# ---------------------------------------------------------------------------
# Route behavior: per-tenant stored credentials with no env configuration
# ---------------------------------------------------------------------------


@pytest.fixture()
def callback_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, list[tuple[str, str | None]]], None, None]:
    """Route client over stored per-tenant credentials, no env configuration.

    The tenants table DDL in fakes._SCHEMA_SQL defaults lifecycle_status to
    'active', so both tenants resolve as runtime tenants.
    """
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("WECOM_CALLBACK_TOKEN", raising=False)
    monkeypatch.delenv("WECOM_CALLBACK_ENCODING_AES_KEY", raising=False)
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    engine = make_worker_engine()
    db = Session(engine)
    insert_tenant(db, _TENANT_A)
    insert_tenant(db, _TENANT_B)
    _config_row(db, _TENANT_A, _CORP_A, _TOKEN_A)
    _config_row(db, _TENANT_B, _CORP_B, _TOKEN_B)
    db.close()
    monkeypatch.setattr(wecom_events, "get_engine", lambda: engine)
    monkeypatch.setattr(wecom_events, "_active_tenant_for_corp", lambda _corp_id: None)
    dispatches: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda *args, **kwargs: dispatches.append(
            (kwargs.get("trigger_source"), kwargs.get("tenant_id"))
        )
        or wecom_events.ArchiveWorkerDispatch.ACCEPTED,
    )
    app = FastAPI()
    app.include_router(wecom_events.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, dispatches
    engine.dispose()


# ---------------------------------------------------------------------------
# Route behavior: per-tenant stored credentials with no env configuration
# ---------------------------------------------------------------------------


def test_get_echostr_authenticates_against_stored_tenant_b_without_env(
    callback_client,
) -> None:
    client, dispatches = callback_client
    message = b"rnd387 verification plaintext"
    response = _get(
        client,
        _encrypt(_envelope(message, _CORP_B.encode())),
        token=_TOKEN_B,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == message
    assert dispatches == []


def test_get_foreign_key_never_leaks_echostr(callback_client) -> None:
    client, _dispatches = callback_client
    # Signed with a valid stored token but encrypted with a key no candidate
    # holds: neither candidate can decrypt, so the echostr is withheld.
    foreign_key = bytes(reversed(range(32)))
    response = _get(
        client,
        _encrypt(_envelope(b"secret-echostr", _CORP_B.encode()), foreign_key),
        token=_TOKEN_A,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid callback request"}


def test_get_receiver_id_mismatch_returns_403(callback_client) -> None:
    client, _dispatches = callback_client
    response = _get(
        client,
        _encrypt(_envelope(b"echostr", b"corp-unknown")),
        token=_TOKEN_A,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Corp ID mismatch"}


def test_post_outer_corp_id_resolves_stored_tenant_b_and_dispatches(callback_client) -> None:
    client, dispatches = callback_client
    response = _post_encrypted(client, _event_xml(_CORP_B), token=_TOKEN_B)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == b"ok"
    assert dispatches == [("callback", _TENANT_B)]


def test_post_receiver_id_is_authoritative_and_wrong_tenant_key_continues(
    callback_client,
) -> None:
    """Outer ToUserName names corp-b but the request is signed with tenant
    A's token and the envelope names corp-a as receiver: the corp-matched
    candidate (tenant B) fails signature, and the defensive candidate
    (tenant A) wins on the authoritative receiver-id check."""
    client, dispatches = callback_client
    message = _event_xml(_CORP_B)  # plaintext outer receiver
    payload = _encrypt(_envelope(message, _CORP_A.encode()))
    body = f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode()
    response = _post(client, body, payload, token=_TOKEN_A)

    assert response.status_code == 200
    assert response.content == b"ok"
    assert dispatches == [("callback", _TENANT_A)]


def test_post_no_candidate_matches_returns_403_and_never_dispatches(callback_client) -> None:
    client, dispatches = callback_client
    # Valid signature for tenant B, valid decryption, but the envelope names
    # an unknown receiver: signature matched yet no tenant is authoritative.
    payload = _encrypt(_envelope(_event_xml(_CORP_B), b"corp-unknown"))
    body = f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode()
    response = _post(client, body, payload, token=_TOKEN_B)

    assert response.status_code == 403
    assert response.json() == {"detail": "Corp ID mismatch"}
    assert dispatches == []


def test_post_env_candidate_still_wins_with_zero_regression(
    callback_client, monkeypatch
) -> None:
    """The env-scoped single-corp deployment stays candidate #1 even when
    stored per-tenant rows exist."""
    client, dispatches = callback_client
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", "env-token")
    monkeypatch.setenv("WECOM_CALLBACK_ENCODING_AES_KEY", _AES_KEY)
    monkeypatch.setenv("WECOM_CORP_ID", "corp-env")
    monkeypatch.setattr(wecom_events, "_active_tenant_for_corp", lambda _corp_id: "tenant-env")

    message = _event_xml("corp-env")
    payload = _encrypt(_envelope(message, b"corp-env"))
    body = f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode()
    timestamp, nonce = "1700000102", "rnd387-env-nonce"
    response = client.post(
        _PATH,
        params={
            "msg_signature": _signature("env-token", timestamp, nonce, payload),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=body,
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.content == b"ok"
    assert dispatches == [("callback", "tenant-env")]


def test_post_malformed_outer_xml_stays_400_and_never_dispatches(callback_client) -> None:
    client, dispatches = callback_client
    response = _post(
        client,
        b"<Encrypt><![CDATA[broken</Encrypt>",
        "unused",
        token=_TOKEN_B,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid callback request"}
    assert dispatches == []
