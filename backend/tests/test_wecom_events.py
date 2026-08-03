"""Focused security contract tests for the public WeCom callback (RND-105)."""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
from threading import Event

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import wecom_events
from app.services import archive_worker_trigger

_TOKEN = "callback-token-sentinel"
_CORP_ID = "corp-sentinel"
_KEY = bytes(range(32))
_AES_KEY = base64.b64encode(_KEY).decode("ascii").rstrip("=")
_PATH = "/api/wecom/archive/events"
_SENTINELS = (
    "SENTINEL_MESSAGE_BODY",
    "SENTINEL_STRUCTURED_CONTENT",
    "SENTINEL_PASSWORD",
    "SENTINEL_PASSWORD_HASH",
    "SENTINEL_TOKEN",
    "SENTINEL_SECRET",
    "SENTINEL_SIGNED_URL",
    "SENTINEL_STORAGE_KEY",
    "/sentinel/fs/path",
    "SENTINEL_SEARCH_TEXT",
    "SENTINEL_TRACEBACK",
    "SENTINEL_SENDER",
    "SENTINEL_RECIPIENT",
    "SENTINEL_ROOM",
    "SENTINEL_RAW_MSGID",
)


def _assert_no_sentinels(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_sentinels(key)
            _assert_no_sentinels(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_sentinels(item)
    else:
        assert not any(sentinel in str(value) for sentinel in _SENTINELS)


def _signature(timestamp: str, nonce: str, payload: str) -> str:
    return hashlib.sha1("".join(sorted([_TOKEN, timestamp, nonce, payload])).encode()).hexdigest()


def _encrypt(plaintext: bytes) -> str:
    pad_len = 32 - len(plaintext) % 32
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_KEY[:16])).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def _encrypt_unpadded_block(plaintext: bytes) -> str:
    assert len(plaintext) % 16 == 0
    encryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_KEY[:16])).encryptor()
    return base64.b64encode(encryptor.update(plaintext) + encryptor.finalize()).decode("ascii")


def _envelope(message: bytes = b"verify-ok", corp_id: bytes = _CORP_ID.encode()) -> bytes:
    return b"r" * 16 + struct.pack("!I", len(message)) + message + corp_id


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", _TOKEN)
    monkeypatch.setenv("WECOM_CALLBACK_ENCODING_AES_KEY", _AES_KEY)
    monkeypatch.setenv("WECOM_CORP_ID", _CORP_ID)
    monkeypatch.setattr(wecom_events, "_active_tenant_for_corp", lambda _corp_id: "tenant-sentinel")
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: wecom_events.ArchiveWorkerDispatch.ACCEPTED,
    )
    app = FastAPI()
    app.include_router(wecom_events.router)
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


def _get(client: TestClient, echostr: str, *, signature: str | None = None):
    timestamp, nonce = "1700000000", "nonce-sentinel"
    return client.get(
        _PATH,
        params={
            "msg_signature": signature or _signature(timestamp, nonce, echostr),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": echostr,
        },
        follow_redirects=False,
    )


def test_get_validates_and_decrypts_echostr(client: TestClient) -> None:
    message = b"verification plaintext"
    response = _get(client, _encrypt(_envelope(message)))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == message
    assert response.history == []


@pytest.mark.parametrize("padding_length", range(17, 33))
def test_get_accepts_wecom_padding_larger_than_an_aes_block(
    client: TestClient, padding_length: int
) -> None:
    # With the fixed 33-byte envelope portion, these message lengths produce
    # every valid WeCom padding value from 17 through 32.
    message_length = 31 if padding_length == 32 else 31 - padding_length
    message = b"x" * message_length
    plaintext = _envelope(message)
    assert 32 - len(plaintext) % 32 == padding_length
    response = _get(client, _encrypt(plaintext))

    assert response.status_code == 200
    assert response.content == message


@pytest.mark.parametrize(
    "payload",
    [
        "%%%not-base64%%%",
        "",
        base64.b64encode(b"short").decode(),
        _encrypt_unpadded_block(b"p" * 15 + b"\x00"),
        _encrypt_unpadded_block(b"p" * 14 + b"\x01\x02"),
        _encrypt(b"x" * 19),
        _encrypt(b"r" * 16 + struct.pack("!I", 99) + b"tiny"),
        _encrypt(_envelope(corp_id=b"\xff")),
        _encrypt(_envelope(message=b"\xff")),
    ],
    ids=[
        "invalid_base64",
        "empty_ciphertext",
        "non_block_ciphertext",
        "invalid_padding_byte",
        "invalid_padding_block",
        "short_envelope",
        "message_length_overflow",
        "invalid_corp_utf8",
        "invalid_message_utf8",
    ],
)
def test_get_malformed_request_data_is_always_safe_400(client: TestClient, payload: str) -> None:
    response = _get(client, payload)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid callback request"}
    assert "500" not in response.text


def test_get_preserves_signature_and_corp_rejections(client: TestClient) -> None:
    payload = _encrypt(_envelope())
    assert _get(client, payload, signature="not-a-valid-signature").status_code == 403

    wrong_corp_payload = _encrypt(_envelope(corp_id=b"other-corp"))
    response = _get(client, wrong_corp_payload)
    assert response.status_code == 403
    assert response.json() == {"detail": "Corp ID mismatch"}


def test_get_missing_parameters_still_returns_422_without_redirect(client: TestClient) -> None:
    response = client.get(_PATH, params={"timestamp": "1"}, follow_redirects=False)
    assert response.status_code == 422
    assert response.history == []


@pytest.mark.parametrize(
    "name,value",
    [
        ("WECOM_CALLBACK_TOKEN", ""),
        ("WECOM_CALLBACK_ENCODING_AES_KEY", ""),
        ("WECOM_CALLBACK_ENCODING_AES_KEY", "not-a-valid-encoding-aes-key"),
    ],
)
def test_server_callback_configuration_errors_remain_safe_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, name: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=wecom_events.__name__)
    monkeypatch.setenv(name, value)
    response = _get(client, _encrypt(_envelope()))

    assert response.status_code == 500
    assert response.json() == {"detail": "Callback configuration error"}
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "configuration_error" in log_text
    assert _TOKEN not in log_text
    assert _AES_KEY not in log_text


def _post(client: TestClient, body: bytes, payload: str, *, signature: str | None = None):
    timestamp, nonce = "1700000001", "post-nonce-sentinel"
    return client.post(
        _PATH,
        params={
            "msg_signature": signature or _signature(timestamp, nonce, payload),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=body,
        follow_redirects=False,
    )


def test_post_valid_signature_acknowledges_and_dispatches_worker_and_media(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched: list[tuple[str, str]] = []
    worker_dispatches: list[object] = []
    monkeypatch.setattr(wecom_events, "_active_tenant_for_corp", lambda _corp_id: "tenant-sentinel")
    monkeypatch.setattr(
        wecom_events,
        "trigger_recent_image_download",
        lambda tenant_id, triggered_by: dispatched.append((tenant_id, triggered_by)),
    )
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: worker_dispatches.append(1) or wecom_events.ArchiveWorkerDispatch.ACCEPTED,
    )
    payload = "post-encrypt-sentinel"
    response = _post(client, f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode(), payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == b"ok"
    assert worker_dispatches == [1]
    assert dispatched == [("tenant-sentinel", "callback")]


@pytest.mark.parametrize(
    "body,payload,signature",
    [
        (b"<xml></xml>", "unused", None),
        (b"<Encrypt><![CDATA[broken</Encrypt>", "unused", None),
        (b"<xml><Encrypt><![CDATA[post-encrypt-sentinel]]></Encrypt>", "post-encrypt-sentinel", None),
        (b"<Encrypt><![CDATA[\xff]]></Encrypt>", "unused", None),
        (b"<Encrypt><![CDATA[post-encrypt-sentinel]]></Encrypt>", "post-encrypt-sentinel", "invalid"),
    ],
    ids=["missing_encrypt", "malformed_cdata", "malformed_xml", "non_utf8_encrypt", "invalid_signature"],
)
def test_post_malformed_or_unsigned_input_never_dispatches(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    payload: str,
    signature: str | None,
) -> None:
    dispatched: list[object] = []
    worker_dispatches: list[object] = []
    monkeypatch.setattr(wecom_events, "trigger_recent_image_download", lambda *_args, **_kwargs: dispatched.append(1))
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: worker_dispatches.append(1) or wecom_events.ArchiveWorkerDispatch.ACCEPTED,
    )
    response = _post(client, body, payload, signature=signature)

    assert response.status_code in {400, 403}
    assert response.status_code != 500
    assert dispatched == []
    assert worker_dispatches == []


def test_post_missing_required_query_parameters_never_dispatches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_dispatches: list[object] = []
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: worker_dispatches.append(1) or wecom_events.ArchiveWorkerDispatch.ACCEPTED,
    )

    response = client.post(_PATH, content=b"<xml><Encrypt><![CDATA[ignored]]></Encrypt></xml>")

    assert response.status_code == 422
    assert worker_dispatches == []


def test_callback_business_logs_are_fixed_info_events_without_request_data(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=wecom_events.__name__)
    message = b"message-secret-sentinel"
    payload = _encrypt(_envelope(message))
    post_payload = "post-encrypt-secret-sentinel"
    assert _get(client, payload).status_code == 200
    assert _post(
        client,
        f"<xml><Encrypt><![CDATA[{post_payload}]]></Encrypt></xml>".encode(),
        post_payload,
    ).status_code == 200
    assert _get(client, "bad-input-sentinel").status_code == 400

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "wecom_callback accepted method=GET" in log_text
    assert "wecom_callback accepted method=POST" in log_text
    assert "wecom_callback rejected reason=invalid_callback_payload" in log_text
    for forbidden in (
        _TOKEN,
        _AES_KEY,
        _CORP_ID,
        message.decode(),
        payload,
        post_payload,
        "bad-input-sentinel",
    ):
        assert forbidden not in log_text


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_access_log_filter_redacts_wecom_event_queries_and_preserves_other_paths() -> None:
    from app.main import _RedactOAuthCallbackQueryFilter

    event = _access_record(
        "/api/wecom/archive/events?msg_signature=signature&timestamp=123&nonce=value&echostr=ciphertext"
    )
    oauth = _access_record("/api/auth/wecom/callback?code=oauth-code&state=csrf-state")
    ordinary = _access_record("/api/conversations?sender=person")
    filter_ = _RedactOAuthCallbackQueryFilter()
    for record in (event, oauth, ordinary):
        assert filter_.filter(record)

    event_text, oauth_text, ordinary_text = (record.getMessage() for record in (event, oauth, ordinary))
    assert "/api/wecom/archive/events?[REDACTED]" in event_text
    for forbidden in ("msg_signature", "timestamp", "nonce", "echostr", "signature", "ciphertext"):
        assert forbidden not in event_text
    assert "oauth-code" not in oauth_text
    assert "csrf-state" not in oauth_text
    assert "/api/conversations?sender=person" in ordinary_text


def test_post_returns_before_the_shared_worker_finishes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = Event()
    release = Event()
    finished = Event()

    def _blocking_worker() -> bool:
        started.set()
        assert release.wait(timeout=1)
        finished.set()
        return True

    monkeypatch.setattr(archive_worker_trigger, "run_archive_worker_once", _blocking_worker)
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        archive_worker_trigger.dispatch_archive_worker,
    )
    payload = "post-encrypt-sentinel"
    response = _post(client, f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode(), payload)

    try:
        assert response.status_code == 200
        assert started.wait(timeout=1)
        assert not finished.is_set()
    finally:
        release.set()
    assert finished.wait(timeout=1)


def test_post_dispatch_failure_is_not_acknowledged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_dispatches: list[object] = []
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: wecom_events.ArchiveWorkerDispatch.FAILED,
    )
    monkeypatch.setattr(
        wecom_events,
        "trigger_recent_image_download",
        lambda *_args, **_kwargs: media_dispatches.append(1),
    )
    payload = "post-encrypt-sentinel"
    response = _post(client, f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode(), payload)

    assert response.status_code == 503
    assert response.json() == {"detail": "Callback worker unavailable"}
    assert media_dispatches == []


def test_dispatch_failure_does_not_leak_request_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: wecom_events.ArchiveWorkerDispatch.FAILED,
    )
    payload = "SENTINEL_SECRET"
    response = _post(client, f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode(), payload)

    assert response.status_code == 503
    _assert_no_sentinels(response.json())
    _assert_no_sentinels([record.getMessage() for record in caplog.records])


def test_post_does_not_dispatch_without_an_active_callback_tenant(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_dispatches: list[object] = []
    monkeypatch.setattr(wecom_events, "_active_tenant_for_corp", lambda _corp_id: None)
    monkeypatch.setattr(
        wecom_events,
        "dispatch_archive_worker",
        lambda: worker_dispatches.append(1) or wecom_events.ArchiveWorkerDispatch.ACCEPTED,
    )
    payload = "post-encrypt-sentinel"
    response = _post(client, f"<xml><Encrypt><![CDATA[{payload}]]></Encrypt></xml>".encode(), payload)

    assert response.status_code == 503
    assert response.json() == {"detail": "Callback worker unavailable"}
    assert worker_dispatches == []
