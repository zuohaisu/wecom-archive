#!/usr/bin/env python3
"""qiniu_helper.py — Qiniu certificate management via the official SDK.

Replaces a hand-rolled QBox HMAC-SHA1 signer (which did not match the
official algorithm — it included the HTTP method in the signed data and
always signed the body regardless of Content-Type, when the real QBox
scheme only signs the body for `application/x-www-form-urlencoded`
requests) with direct calls into the official `qiniu` Python SDK
(`qiniu.Auth`, `qiniu.DomainManager`). No signing logic is reimplemented
here — see https://github.com/zuohaisu/wecom-archive/wiki/TLS-Renewal-Architecture section 6.1.

Commands (each prints one line of JSON to stdout and exits 0 on success,
non-zero on failure — see `Result.exit_code`):

    qiniu_helper.py upload --domain <domain> --cert-file <path> --key-file <path> [--timeout N]
    qiniu_helper.py bind   --domain <domain> --cert-id <id>                       [--timeout N]
    qiniu_helper.py verify --domain <domain> --expected-cert-id <id>              [--timeout N]

Credentials come ONLY from the environment (QINIU_ACCESS_KEY/QINIU_SECRET_KEY,
falling back to SAVED_QINIU_AK/SAVED_QINIU_SK) — never from argv. Certificate
and private key CONTENT is read from the files at --cert-file/--key-file
(only the *paths* appear in argv). Nothing this script prints — stdout,
stderr, or an exception message — ever contains the AK/SK, the constructed
Authorization header, or private key / certificate material. See
`redact()` and the "Runtime Secret Safety" section of README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import warnings
from typing import Any, Optional

# Environment-specific TLS-library warnings (e.g. urllib3 vs LibreSSL on
# some macOS Python builds) are noise, not signal — never let them land on
# stderr where a test or operator could mistake them for a real error.
warnings.filterwarnings("ignore")

try:
    import qiniu
    from qiniu import Auth, DomainManager
except ImportError:  # pragma: no cover - exercised via missing-dep test
    sys.stderr.write(
        "qiniu_helper.py: the official 'qiniu' package is not installed. "
        "Install it with: pip install -r ssl-renew/requirements.txt\n"
    )
    sys.exit(3)

try:
    import requests
except ImportError:  # pragma: no cover - qiniu SDK depends on requests already
    requests = None  # type: ignore[assignment]


EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_USAGE_ERROR = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_AUTH_FAILURE = 4

DEFAULT_API_HOST = "http://api.qiniu.com"
DEFAULT_TIMEOUT = 15

# Matches the redaction intent of ssl-renew/lib/common.sh::filter_secrets.
# Defense in depth only — normal Qiniu responses never contain these, and
# request bodies (which DO contain the private key) are never printed in
# the first place, redacted or not.
_SECRET_PATTERNS = [
    re.compile(r'"?(QINIU_ACCESS_KEY|QINIU_SECRET_KEY|SAVED_QINIU_AK|SAVED_QINIU_SK)"?\s*[:=]\s*"?[^\s",}]*"?', re.IGNORECASE),
    re.compile(r"(Authorization\s*:\s*QBox\s+)[A-Za-z0-9_\-:]+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]


def redact(text: Optional[str]) -> str:
    if not text:
        return ""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def emit(result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def load_credentials() -> tuple[str, str]:
    ak = os.environ.get("QINIU_ACCESS_KEY") or os.environ.get("SAVED_QINIU_AK") or ""
    sk = os.environ.get("QINIU_SECRET_KEY") or os.environ.get("SAVED_QINIU_SK") or ""
    if not ak or not sk:
        raise CredentialsMissing()
    return ak, sk


class CredentialsMissing(Exception):
    pass


class HelperError(Exception):
    """Carries a JSON-safe (already redacted) error payload + exit code."""

    def __init__(self, stage: str, message: str, exit_code: int = EXIT_GENERIC_FAILURE,
                 http_status: Optional[int] = None, qiniu_code: Optional[Any] = None):
        super().__init__(message)
        self.stage = stage
        self.message = redact(message)
        self.exit_code = exit_code
        self.http_status = http_status
        self.qiniu_code = qiniu_code

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": False, "stage": self.stage, "error": self.message}
        if self.http_status is not None:
            d["http_status"] = self.http_status
        if self.qiniu_code is not None:
            d["qiniu_code"] = self.qiniu_code
        return d


def _api_host() -> str:
    return os.environ.get("QINIU_API_HOST", DEFAULT_API_HOST)


def _apply_timeout(timeout: int) -> None:
    # The official SDK's http layer reads this global instead of taking a
    # per-call timeout kwarg for DomainManager methods.
    qiniu.config.set_default(connection_timeout=timeout)


def _domain_manager(ak: str, sk: str, timeout: int) -> DomainManager:
    _apply_timeout(timeout)
    auth = Auth(ak, sk)
    dm = DomainManager(auth)
    dm.server = _api_host()
    return dm


def _classify_response_error(stage: str, ret: Optional[dict], info: Any) -> HelperError:
    status = getattr(info, "status_code", None)

    # The SDK's http layer never raises on a network-level failure (DNS,
    # connection refused, timeout) — it catches the exception internally
    # and returns (None, ResponseInfo(None, exc)), i.e. status_code == -1.
    # Without this branch that would fall through to the generic "HTTP -1"
    # message below, which is technically correct but not clearly a
    # network/timeout problem versus an actual protocol-level response.
    if status is None or status < 0:
        exc = getattr(info, "exception", None)
        exc_desc = f": {type(exc).__name__}" if exc is not None else ""
        return HelperError(
            stage,
            f"Qiniu {stage} request failed (network error / timeout){exc_desc}",
        )

    # ResponseInfo.text_body / .error carry the (already-parsed-or-raw) body;
    # never forward it verbatim — only a redacted, length-capped snippet.
    raw_text = ""
    try:
        raw_text = info.text_body or ""
    except Exception:
        raw_text = ""
    snippet = redact(raw_text)[:300]

    qiniu_code = None
    if isinstance(ret, dict):
        qiniu_code = ret.get("code") or ret.get("error")

    if status in (401, 403):
        return HelperError(
            stage,
            f"Qiniu {stage} rejected — HTTP {status} (check AK/SK and permissions): {snippet}",
            exit_code=EXIT_AUTH_FAILURE,
            http_status=status,
            qiniu_code=qiniu_code,
        )
    if status == 429:
        return HelperError(
            stage,
            f"Qiniu {stage} rate-limited — HTTP 429: {snippet}",
            http_status=status,
            qiniu_code=qiniu_code,
        )
    if status is not None and status >= 500:
        return HelperError(
            stage,
            f"Qiniu {stage} server error — HTTP {status}: {snippet}",
            http_status=status,
            qiniu_code=qiniu_code,
        )
    # A 200 that still lands here (ret missing the expected field) usually
    # means the official SDK discarded the body — it treats any 200
    # response with no X-Reqid header as "not really Qiniu" (see
    # qiniu.http.response.ResponseInfo) and returns ret=None regardless of
    # what the body actually contained. That most often points at a proxy/
    # gateway between us and Qiniu stripping the header, not a bad request.
    return HelperError(
        stage,
        f"Qiniu {stage} failed — HTTP {status}: {snippet}",
        http_status=status,
        qiniu_code=qiniu_code,
    )


def cmd_upload(args: argparse.Namespace) -> dict[str, Any]:
    if not os.path.isfile(args.cert_file):
        raise HelperError("upload", f"fullchain.cer not found: {args.cert_file}", exit_code=EXIT_USAGE_ERROR)
    if not os.path.isfile(args.key_file):
        raise HelperError("upload", f"private key not found: {args.key_file}", exit_code=EXIT_USAGE_ERROR)

    with open(args.cert_file, "r", encoding="utf-8") as f:
        ca = f.read()
    with open(args.key_file, "r", encoding="utf-8") as f:
        pri = f.read()

    ak, sk = load_credentials()
    dm = _domain_manager(ak, sk, args.timeout)
    name = f"{args.domain}-{int(time.time())}"

    try:
        ret, info = dm.create_sslcert(name, args.domain, pri, ca)
    except Exception as exc:  # network error, timeout, etc.
        raise HelperError("upload", f"request failed: {redact(str(exc))}") from exc
    finally:
        # Best-effort: drop references to secret material as soon as possible.
        pri = "REDACTED"  # noqa: F841

    cert_id = (ret or {}).get("certID") if isinstance(ret, dict) else None
    if not ret or not cert_id:
        raise _classify_response_error("upload", ret, info)

    return {"ok": True, "certID": cert_id}


def cmd_bind(args: argparse.Namespace) -> dict[str, Any]:
    ak, sk = load_credentials()
    dm = _domain_manager(ak, sk, args.timeout)

    try:
        ret, info = dm.put_httpsconf(args.domain, args.cert_id, False)
    except Exception as exc:
        raise HelperError("bind", f"request failed: {redact(str(exc))}") from exc

    status = getattr(info, "status_code", None)
    if status != 200:
        raise _classify_response_error("bind", ret, info)
    # Qiniu returns {} on success; a 200 response carrying an error `code`
    # is still a failure.
    if isinstance(ret, dict) and ret.get("code") not in (None, 200):
        raise _classify_response_error("bind", ret, info)

    return {"ok": True}


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    ak, sk = load_credentials()
    _apply_timeout(args.timeout)
    auth = Auth(ak, sk)

    url = f"{_api_host()}/domain/{args.domain}/httpsconf"
    token = auth.token_of_request(url)  # official signer, GET has no body

    if requests is None:
        raise HelperError("verify", "the 'requests' package is not installed", exit_code=EXIT_MISSING_DEPENDENCY)

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"QBox {token}"},
            timeout=args.timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise HelperError("verify", "request timed out") from exc
    except requests.exceptions.RequestException as exc:
        raise HelperError("verify", f"request failed: {redact(str(exc))}") from exc

    if resp.status_code in (401, 403):
        raise HelperError(
            "verify",
            f"Qiniu verify rejected — HTTP {resp.status_code} (check AK/SK and permissions)",
            exit_code=EXIT_AUTH_FAILURE,
            http_status=resp.status_code,
        )
    if resp.status_code == 429:
        raise HelperError("verify", "Qiniu verify rate-limited — HTTP 429", http_status=429)
    if resp.status_code >= 500:
        raise HelperError("verify", f"Qiniu verify server error — HTTP {resp.status_code}", http_status=resp.status_code)
    if resp.status_code != 200:
        raise HelperError("verify", f"Qiniu verify failed — HTTP {resp.status_code}", http_status=resp.status_code)

    try:
        data = resp.json()
    except ValueError as exc:
        raise HelperError("verify", "invalid JSON in Qiniu response") from exc

    actual = data.get("certId") if isinstance(data, dict) else None
    matches = actual == args.expected_cert_id
    return {"ok": True, "certId": actual, "matches": matches}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="qiniu_helper.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_upload = sub.add_parser("upload")
    p_upload.add_argument("--domain", required=True)
    p_upload.add_argument("--cert-file", required=True)
    p_upload.add_argument("--key-file", required=True)
    p_upload.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p_upload.set_defaults(func=cmd_upload)

    p_bind = sub.add_parser("bind")
    p_bind.add_argument("--domain", required=True)
    p_bind.add_argument("--cert-id", required=True)
    p_bind.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p_bind.set_defaults(func=cmd_bind)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--domain", required=True)
    p_verify.add_argument("--expected-cert-id", required=True)
    p_verify.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p_verify.set_defaults(func=cmd_verify)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE_ERROR

    try:
        result = args.func(args)
    except CredentialsMissing:
        emit({
            "ok": False,
            "stage": "config",
            "error": "missing Qiniu credentials: set QINIU_ACCESS_KEY+QINIU_SECRET_KEY or SAVED_QINIU_AK+SAVED_QINIU_SK",
        })
        return EXIT_USAGE_ERROR
    except HelperError as exc:
        emit(exc.as_dict())
        return exc.exit_code
    except Exception as exc:  # last-resort safety net — still redact + never leak a raw traceback body
        emit({"ok": False, "stage": "unknown", "error": redact(f"{type(exc).__name__}: {exc}")})
        return EXIT_GENERIC_FAILURE

    emit(result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
