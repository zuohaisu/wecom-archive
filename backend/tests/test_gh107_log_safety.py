"""GH-107/GH-139 regression coverage for centralized HTTP log redaction."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from io import StringIO
from typing import Iterator

import httpx
import pytest

from app.log_safety import (
    RedactSecretQueryParamsFilter,
    configure_secret_safe_logging,
)

_SENTINEL = "GH139_SENTINEL_SECRET_DO_NOT_PERSIST"


@contextmanager
def _capture_logger(logger_name: str) -> Iterator[StringIO]:
    """Capture one logger's formatted sink output without root propagation."""
    logger = logging.getLogger(logger_name)
    old_level = logger.level
    old_propagate = logger.propagate
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_configure_is_idempotent_and_preserves_httpx_info_observability() -> None:
    logger = logging.getLogger("httpx")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        configure_secret_safe_logging()
        factory = logging.getLogRecordFactory()
        configure_secret_safe_logging()

        assert logging.getLogRecordFactory() is factory
        assert logger.isEnabledFor(logging.INFO)
    finally:
        logger.setLevel(old_level)


@pytest.mark.parametrize("logger_name", ("httpx", "httpcore"))
def test_third_party_http_logger_redacts_delayed_url_args_at_the_sink(
    logger_name: str,
) -> None:
    configure_secret_safe_logging()
    url = httpx.URL(
        f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/list?access_token={_SENTINEL}"
    )

    with _capture_logger(logger_name) as stream:
        logging.getLogger(logger_name).info(
            'HTTP Request: %s %s "%s %s %s"',
            "GET",
            url,
            "HTTP/1.1",
            200,
            "OK",
        )

    persisted = stream.getvalue()
    assert _SENTINEL not in persisted
    assert "access_token=[REDACTED]" in persisted
    assert "GET" in persisted
    assert "qyapi.weixin.qq.com/cgi-bin/externalcontact/list" in persisted
    assert "200" in persisted


def test_application_logger_remains_redacted() -> None:
    configure_secret_safe_logging()

    with _capture_logger("app.wecom_contacts") as stream:
        logging.getLogger("app.wecom_contacts").info(
            "outbound request url=%s",
            f"https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token={_SENTINEL}",
        )

    persisted = stream.getvalue()
    assert _SENTINEL not in persisted
    assert "access_token=[REDACTED]" in persisted
    assert "cgi-bin/user/get" in persisted


def test_multiple_query_parameters_preserve_safe_diagnostics() -> None:
    configure_secret_safe_logging()
    url = (
        "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/list?foo=bar&"
        f"access_token={_SENTINEL}&x=1"
    )

    with _capture_logger("httpx") as stream:
        logging.getLogger("httpx").info("HTTP Request: GET %s \"HTTP/1.1 200 OK\"", url)

    persisted = stream.getvalue()
    assert _SENTINEL not in persisted
    assert "foo=bar" in persisted
    assert "x=1" in persisted
    assert "access_token=[REDACTED]" in persisted


def test_realistic_encoded_httpx_url_form_is_redacted_case_insensitively() -> None:
    configure_secret_safe_logging()
    url = httpx.URL(
        "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get?foo=bar&"
        f"AcCeSs_ToKeN={_SENTINEL}%2Fencoded&x=1"
    )

    with _capture_logger("httpx") as stream:
        logging.getLogger("httpx").info("HTTP Request: GET %s \"HTTP/1.1 200 OK\"", url)

    persisted = stream.getvalue()
    assert _SENTINEL not in persisted
    assert "AcCeSs_ToKeN=[REDACTED]" in persisted
    assert "foo=bar" in persisted
    assert "x=1" in persisted


def test_url_bearing_exception_is_redacted_before_formatter_output() -> None:
    configure_secret_safe_logging()

    with _capture_logger("httpx") as stream:
        try:
            raise RuntimeError(
                f"request failed for https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token={_SENTINEL}"
            )
        except RuntimeError:
            logging.getLogger("httpx").exception("outbound request failed")

    persisted = stream.getvalue()
    assert _SENTINEL not in persisted
    assert "access_token=[REDACTED]" in persisted
    assert "RuntimeError" in persisted


@pytest.mark.parametrize(
    "raw, secret",
    [
        ("https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpsecret=CORPSECRET_SENTINEL", "CORPSECRET_SENTINEL"),
        ("https://qyapi.weixin.qq.com/cgi-bin/service/get_auth_info?suite_access_token=SUITE_SENTINEL", "SUITE_SENTINEL"),
        ("https://media-origin.crowntime.cn/object?e=123&token=QINIU_SENTINEL", "QINIU_SENTINEL"),
    ],
)
def test_handler_filter_reuses_the_same_query_secret_primitive(raw: str, secret: str) -> None:
    record = logging.LogRecord(
        name="third_party",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="outbound request %s",
        args=(raw,),
        exc_info=None,
    )

    assert RedactSecretQueryParamsFilter().filter(record) is True
    assert secret not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
