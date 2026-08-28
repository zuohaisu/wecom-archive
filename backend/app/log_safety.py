"""Secret-safe defaults for third-party HTTP client logging (GH-107).

WeCom's (and related payment provider) APIs take ``access_token`` /
``corpsecret`` / ``suite_access_token`` as query-string parameters rather
than headers. httpx/httpcore's own request/response logging prints the full
request URL at INFO level, so any process that runs with INFO-level logging
enabled — as the external-contact reconcile entrypoint did via
``logging.basicConfig(level=logging.INFO)`` — would otherwise write those
secrets to journald verbatim (GH-107: ~11.7k occurrences/24h observed there).

Call :func:`configure_secret_safe_logging` once, before any HTTP traffic,
from every process that talks to those APIs over httpx: the web app and any
CLI/timer entrypoint. It is idempotent and safe to call from multiple
modules regardless of import order.
"""

from __future__ import annotations

import logging
import re

_SUPPRESSED_LOGGERS = ("httpx", "httpcore")

# Keys observed carrying secrets in this codebase's outbound query strings:
# WeCom access tokens (app/wecom_contacts.py, app/routers/auth.py), the app
# corpsecret used to mint them (app/auth.py get_wecom_token), and third-party
# suite tokens (app/services/wecom_org_authorization.py).
_SECRET_QUERY_KEYS = (
    "access_token",
    "corpsecret",
    "suite_access_token",
    "provider_access_token",
)

_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_QUERY_KEYS) + r")=[^&\s\"'>]+"
)


class RedactSecretQueryParamsFilter(logging.Filter):
    """Defense-in-depth: scrub known secret query params from any record
    emitted by a suppressed logger, in case its level is later raised again
    (e.g. for local debugging) and the suppression above is bypassed."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 -- logging must never raise
            return True
        if "=" not in message:
            return True
        redacted = _SECRET_QUERY_PATTERN.sub(r"\1=[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_configured = False


def configure_secret_safe_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    redact_filter = RedactSecretQueryParamsFilter()
    for name in _SUPPRESSED_LOGGERS:
        target = logging.getLogger(name)
        target.setLevel(logging.WARNING)
        target.addFilter(redact_filter)
