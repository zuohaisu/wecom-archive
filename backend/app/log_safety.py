"""Centralized secret redaction for outbound HTTP diagnostics.

Some providers put credentials in request query strings.  ``httpx`` emits a
useful INFO-level request summary which includes the fully rendered URL, and
processes such as the external-contact reconcile worker send those records to
journald through the root logger.  Redact query secrets when each
``LogRecord`` is created, before any logger handler or formatter can persist
it.  This preserves method, host, path, status, and error diagnostics without
suppressing HTTP observability.

``configure_secret_safe_logging`` is installed from ``app.__init__`` so every
application entrypoint gets the boundary before it imports an HTTP client.  It
is intentionally idempotent for explicit callers and embedding processes.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from collections.abc import Callable, Mapping
from typing import Any

# Query keys carrying credentials in outbound URLs that this repository builds:
# WeCom access tokens, the WeCom corpsecret used to mint them, third-party suite
# access tokens, and Qiniu's short-lived signed-download token.  The Qiniu
# ``e`` expiry parameter is not itself a credential and remains observable.
_SECRET_QUERY_KEYS = (
    "access_token",
    "corpsecret",
    "suite_access_token",
    "token",
)

_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_QUERY_KEYS) + r")=([^&\s\"'>#;]*)"
)


# Keep the primitive public for handler-level integrations and focused tests.
def redact_secret_query_params(value: str) -> str:
    """Return *value* with known credential query values replaced safely."""
    return _SECRET_QUERY_PATTERN.sub(r"\1=[REDACTED]", value)


def _redact_value(value: Any) -> Any:
    """Best-effort redaction for malformed deferred-formatting arguments."""
    if isinstance(value, str):
        return redact_secret_query_params(value)
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _redact_value(item) for key, item in value.items()}
    try:
        rendered = str(value)
    except Exception:  # noqa: BLE001 -- logging must never raise
        return value
    redacted = redact_secret_query_params(rendered)
    return redacted if redacted != rendered else value


def _redact_exception(record: logging.LogRecord) -> None:
    """Remove a URL-bearing exception from a record before a formatter sees it."""
    if not record.exc_info:
        return
    try:
        rendered = "".join(traceback.format_exception(*record.exc_info))
    except Exception:  # noqa: BLE001 -- logging must never raise
        return
    redacted = redact_secret_query_params(rendered)
    if redacted != rendered:
        # Formatter.format() uses exc_text when present.  Clearing exc_info
        # prevents another handler from independently rendering the raw
        # exception after this centralized boundary has scrubbed it.
        record.exc_text = redacted
        record.exc_info = None


def redact_log_record(record: logging.LogRecord) -> None:
    """Scrub query secrets from a record's message, exception, and stack info."""
    try:
        rendered = record.getMessage()
    except Exception:  # noqa: BLE001 -- logging must never raise
        record.msg = _redact_value(record.msg)
        record.args = _redact_value(record.args)
    else:
        redacted = redact_secret_query_params(rendered)
        if redacted != rendered:
            # Resolve delayed ``%s``/mapping formatting before replacing the
            # source values, so URL objects and string arguments are covered.
            record.msg = redacted
            record.args = ()

    if record.stack_info:
        record.stack_info = redact_secret_query_params(record.stack_info)
    _redact_exception(record)


class RedactSecretQueryParamsFilter(logging.Filter):
    """Handler-compatible adapter for the shared LogRecord redaction primitive."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_log_record(record)
        return True


_configured = False
_config_lock = threading.Lock()
_record_factory: Callable[..., logging.LogRecord] | None = None


def configure_secret_safe_logging() -> None:
    """Install one process-wide pre-sink LogRecord redaction boundary.

    ``logging`` invokes its record factory for application, ``httpx``,
    ``httpcore``, and other third-party logger records before propagation and
    before a handler formats or writes them.  The existing factory is composed
    rather than replaced, so an embedding process's custom record fields are
    preserved.
    """
    global _configured, _record_factory
    with _config_lock:
        if _configured and logging.getLogRecordFactory() is _record_factory:
            return

        previous_factory = logging.getLogRecordFactory()

        def secret_safe_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous_factory(*args, **kwargs)
            redact_log_record(record)
            return record

        logging.setLogRecordFactory(secret_safe_record_factory)
        _record_factory = secret_safe_record_factory
        _configured = True
