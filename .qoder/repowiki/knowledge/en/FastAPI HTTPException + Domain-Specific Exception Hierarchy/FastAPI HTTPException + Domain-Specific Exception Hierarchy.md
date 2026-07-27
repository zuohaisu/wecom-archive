---
kind: error_handling
name: FastAPI HTTPException + Domain-Specific Exception Hierarchy
category: error_handling
scope:
    - '**'
source_files:
    - backend/app/main.py
    - backend/app/auth.py
    - backend/app/routers/auth.py
    - backend/app/routers/conversations.py
    - backend/app/media_storage.py
---

The WeCom Archive backend uses FastAPI's built-in `HTTPException` as the primary error signaling mechanism for HTTP endpoints, combined with a small domain-specific exception hierarchy in `app/media_storage.py` that subclasses Python builtins (`FileNotFoundError`, `OSError`, `ValueError`) so existing `except` clauses continue to work while new callers can distinguish between confirmed-not-found, provider-unavailable, and misconfiguration cases.

**How errors are raised and propagated**
- Routes raise `HTTPException(status_code=..., detail="...")` for client-facing errors (401 unauthenticated, 403 forbidden, 404 not found, 400 bad request, 500 server configuration error). The `get_current_user` dependency is the central auth gate — it raises 401 on missing/expired/invalid session or DB lookup failure, never leaking stack traces.
- External service calls (WeCom OAuth, Qiniu SDK) wrap failures in `RuntimeError` with descriptive messages; callers catch broadly and convert to appropriate redirects or HTTP responses. Sensitive fields (access tokens, secrets, passwords, session IDs) are never logged — only exception type names are recorded via `logger.error("...: %s", type(exc).__name__)`.
- The media storage layer defines four custom exceptions: `MediaObjectNotFound`, `MediaStorageUnavailable`, `MediaStorageConfigurationError`, `MediaStorageOperationError`. These subclass builtins so legacy code catching bare `FileNotFoundError`/`OSError`/`ValueError` keeps working, while new code can be precise about which class of failure occurred.

**Middleware and global handling**
- A Starlette `BaseHTTPMiddleware` (`MediaAccessNoStoreMiddleware`) enforces `Cache-Control: no-store` on every response for media-access descriptor endpoints — including error paths produced by FastAPI's own exception-to-response conversion, because setting headers inside the route body would miss dependency-resolution failures (e.g., 401 from `get_current_user`).
- Uvicorn access logs are filtered via a custom `logging.Filter` (`_RedactOAuthCallbackQueryFilter`) to redact OAuth callback query strings containing short-lived authorization codes and CSRF state tokens.

**Conventions developers should follow**
- Use `HTTPException` for all user-facing error responses; never return raw exceptions or stack traces.
- Wrap external API calls in try/except, log only the exception type name (never payloads), and re-raise or redirect with a safe message.
- For storage-layer failures, raise the specific `Media*` exception so callers can distinguish "not found" from "unavailable" from "misconfigured".
- Never log sensitive data: passwords, hashes, session tokens, access tokens, secrets, or user-controlled values — use `safe_log_value()` for numeric errcodes and `type(exc).__name__` for exception logging.
- When validating third-party responses, use `strict_int_equals()` instead of `== 0` to prevent boolean coercion attacks on errcode/status fields.