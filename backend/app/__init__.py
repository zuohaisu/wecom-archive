"""Application package initialization for process-wide safety boundaries."""

from app.log_safety import configure_secret_safe_logging

# Every application CLI, worker, and ASGI entrypoint imports an ``app.*``
# module before it can create an outbound HTTP client.  Install the LogRecord
# factory at that shared boundary so third-party records are safe before any
# logger handler formats or persists them.
configure_secret_safe_logging()
