"""GH-107: httpx/httpcore must never emit access_token/corpsecret-bearing
request URLs at INFO level (the reconcile entrypoint's own
``logging.basicConfig(level=logging.INFO)`` previously let ~11.7k/24h of
those lines reach journald verbatim)."""

from __future__ import annotations

import logging

import pytest

from app.log_safety import RedactSecretQueryParamsFilter, configure_secret_safe_logging


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    import app.log_safety as log_safety

    monkeypatch.setattr(log_safety, "_configured", False)
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        for f in list(logger.filters):
            logger.removeFilter(f)
    yield
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        for f in list(logger.filters):
            logger.removeFilter(f)


def test_configure_suppresses_httpx_and_httpcore_info_logs() -> None:
    # Simulates a process that raised root/httpx logging to INFO, as
    # external_contact_sync.main()'s logging.basicConfig(level=logging.INFO)
    # used to do before this fix.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.INFO)

    configure_secret_safe_logging()

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_configure_is_idempotent_across_repeated_calls() -> None:
    configure_secret_safe_logging()
    configure_secret_safe_logging()

    assert len(logging.getLogger("httpx").filters) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "HTTP Request: GET https://qyapi.weixin.qq.com/cgi-bin/externalcontact/list"
        "?access_token=SECRETVALUE123&userid=abc \"HTTP/1.1 200 OK\"",
        "HTTP Request: GET https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        "?corpid=ww123&corpsecret=SUPERSECRET \"HTTP/1.1 200 OK\"",
        "HTTP Request: GET https://qyapi.weixin.qq.com/cgi-bin/service/get_suite_token"
        "?suite_access_token=abcxyz \"HTTP/1.1 200 OK\"",
    ],
)
def test_redact_filter_scrubs_known_secret_query_params(raw: str) -> None:
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg=raw, args=(), exc_info=None,
    )

    kept = RedactSecretQueryParamsFilter().filter(record)

    assert kept is True
    rendered = record.getMessage()
    assert "SECRETVALUE123" not in rendered
    assert "SUPERSECRET" not in rendered
    assert "abcxyz" not in rendered
    assert "[REDACTED]" in rendered


def test_redact_filter_leaves_ordinary_messages_untouched() -> None:
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="external_contact_reconcile status=completed total=42",
        args=(), exc_info=None,
    )

    RedactSecretQueryParamsFilter().filter(record)

    assert record.getMessage() == "external_contact_reconcile status=completed total=42"
