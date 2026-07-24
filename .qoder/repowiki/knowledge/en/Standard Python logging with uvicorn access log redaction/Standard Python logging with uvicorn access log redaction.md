---
kind: logging_system
name: Standard Python logging with uvicorn access log redaction
category: logging_system
scope:
    - '**'
source_files:
    - backend/app/main.py
    - backend/app/auth.py
    - backend/alembic/env.py
---

The WeCom Archive 365 backend uses Python's built-in `logging` module exclusively — no third-party logging framework (structlog, loguru, etc.) is installed or configured. Each module creates a logger via `logger = logging.getLogger(__name__)` and logs at `info`, `warning`, and `error` levels using positional `%s` formatting.

**Framework initialization**: There is no centralized logging configuration file or `basicConfig()` call in application code. The only explicit logging setup is in `backend/alembic/env.py`, which calls `logging.config.fileConfig(config.config_file_name)` to load Alembic's own logging config from `alembic.ini`. Application-level loggers therefore rely on Python's default root logger behavior (which typically writes to stderr) unless the process is started with an external logging configuration.

**Access log redaction**: The most significant logging customization lives in `backend/app/main.py`, where a custom `logging.Filter` (`_RedactOAuthCallbackQueryFilter`) is attached to the `uvicorn.access` logger. This filter redacts query-string parameters (`code`, `state`) on `/api/auth/wecom/callback?` requests before they are written to uvicorn's HTTP access log, preventing authorization codes and CSRF tokens from leaking into logs.

**Log level strategy**: The codebase uses three levels consistently:
- `logger.info(...)` for normal operational events (login attempts, successful token refreshes, callback receipts)
- `logger.warning(...)` for recoverable misconfigurations or invalid inputs (unrecognized `AUTH_MODE`, expired state tokens, malformed provider responses)
- `logger.error(...)` for failures that need attention (WeCom API errors, session/user lookup exceptions, missing credentials)

No `debug`-level logging is used anywhere in the application code.

**Structured fields**: Logging is unstructured text — there is no structured JSON logging, no correlation IDs injected into every log record, and no common set of contextual fields (tenant_id, user_id, request_id) attached to log records. Contextual information is embedded as formatted strings within the message itself.

**Security considerations**: The codebase is careful about what gets logged: passwords, hashes, session tokens, OAuth secrets, and signed URL query strings are explicitly never logged. A `safe_log_value()` helper in `app/auth.py` ensures externally-controlled numeric fields cannot forge log lines through control characters or unexpected types.

**Configuration**: There is no `LOG_LEVEL` environment variable or runtime log-level switching. Log output format and destination are controlled entirely by how the process is started (e.g., uvicorn flags like `--no-access-log`), not by application code.