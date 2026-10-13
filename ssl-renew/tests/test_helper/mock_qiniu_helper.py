#!/usr/bin/env python3
"""Mock replacement for qiniu_helper.py — used by fast, network-free bats
tests of lib/qiniu.sh's own argv construction and response parsing. Mimics
the real helper's CLI surface and JSON/exit-code contract exactly, but
never imports the qiniu SDK or touches the network.

Real crypto/network correctness (does our request actually match what the
official SDK would send/sign, does a real HTTP round-trip work) is proven
separately by tests/test_qiniu_helper.py (golden parity + mocked-transport
tests) and the real-helper-against-a-local-server integration tests.

Controlled via env vars:
    MOCK_QINIU_MODE       success | http_error | network_error (default: success)
    MOCK_QINIU_STATUS     HTTP status to report for http_error mode (default: 500)
    MOCK_QINIU_CERT_ID    certID to return for `upload` success (default: mock-certid)
    MOCK_QINIU_ACTUAL_CERT_ID  certId to return for `verify` (default: same as expected)
    MOCK_QINIU_ERROR      error message override
    MOCK_QINIU_LOG        if set, the full argv is appended here (one line per call)

`query` (GH-231) additionally honors:
    MOCK_QINIU_BOUND_CERT_FP   leaf fingerprint of the bound cert, either a
                               bare fingerprint (all domains) or
                               comma-separated domain=fingerprint pairs.
                               Empty result ⇒ no binding (certId null).
    MOCK_QINIU_BOUND_CERT_ID   certId to report alongside the fingerprint
                               (default: mock-bound-certid)
    MOCK_QINIU_NOT_AFTER       notAfter unix timestamp to report
                               (default: now + 90 days)
    MOCK_QINIU_CERT_DETAIL_MISSING  "1" ⇒ bound certId no longer resolves
                               (certDetailMissing: true, no fingerprint)
"""
import json
import os
import sys
import time

EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_USAGE_ERROR = 2
EXIT_AUTH_FAILURE = 4


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")


def _bound_fp_for(domain):
    """Resolve the bound-certificate fingerprint for a domain from
    MOCK_QINIU_BOUND_CERT_FP. Accepts either a bare fingerprint (applies to
    every domain) or comma-separated domain=fingerprint pairs, e.g.
        media.crowntime.cn=<fp>,media-origin.crowntime.cn=<fp2>
    An empty pair value means "no binding reported for that domain"."""
    raw = os.environ.get("MOCK_QINIU_BOUND_CERT_FP", "")
    for entry in raw.split(","):
        if "=" in entry:
            entry_domain, fp = entry.split("=", 1)
            if entry_domain == domain:
                return fp
        elif entry:
            return entry
    return ""


def main():
    if os.environ.get("MOCK_QINIU_LOG"):
        with open(os.environ["MOCK_QINIU_LOG"], "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\n")

    if len(sys.argv) < 2:
        emit({"ok": False, "stage": "usage", "error": "no command"})
        return EXIT_USAGE_ERROR

    command = sys.argv[1]
    args = sys.argv[2:]

    def get_opt(name, default=None):
        if name in args:
            return args[args.index(name) + 1]
        return default

    mode = os.environ.get("MOCK_QINIU_MODE", "success")
    status = int(os.environ.get("MOCK_QINIU_STATUS", "500"))
    error_override = os.environ.get("MOCK_QINIU_ERROR")

    if mode == "network_error":
        emit({"ok": False, "stage": command, "error": error_override or f"Qiniu {command} request failed (network error / timeout): mock"})
        return EXIT_GENERIC_FAILURE

    if mode == "http_error":
        auth_related = status in (401, 403)
        msg = error_override or f"Qiniu {command} rejected — HTTP {status} (check AK/SK and permissions): mock" \
            if auth_related else (error_override or f"Qiniu {command} failed — HTTP {status}: mock")
        emit({"ok": False, "stage": command, "error": msg, "http_status": status})
        return EXIT_AUTH_FAILURE if auth_related else EXIT_GENERIC_FAILURE

    # mode == success
    if command == "upload":
        cert_file = get_opt("--cert-file")
        key_file = get_opt("--key-file")
        if not cert_file or not os.path.isfile(cert_file):
            emit({"ok": False, "stage": "upload", "error": f"fullchain.cer not found: {cert_file}"})
            return EXIT_USAGE_ERROR
        if not key_file or not os.path.isfile(key_file):
            emit({"ok": False, "stage": "upload", "error": f"private key not found: {key_file}"})
            return EXIT_USAGE_ERROR
        emit({"ok": True, "certID": os.environ.get("MOCK_QINIU_CERT_ID", "mock-certid")})
        return EXIT_OK

    if command == "bind":
        domain = get_opt("--domain", "")
        if domain == os.environ.get("MOCK_QINIU_FAIL_BIND_DOMAIN"):
            emit({"ok": False, "stage": "bind", "error": "mock bind rejected"})
            return EXIT_GENERIC_FAILURE
        emit({"ok": True})
        return EXIT_OK

    if command == "verify":
        expected = get_opt("--expected-cert-id")
        actual = os.environ.get("MOCK_QINIU_ACTUAL_CERT_ID", expected)
        emit({"ok": True, "certId": actual, "matches": actual == expected})
        return EXIT_OK

    if command == "query":
        domain = get_opt("--domain", "")
        bound_fp = _bound_fp_for(domain)
        detail_missing = os.environ.get("MOCK_QINIU_CERT_DETAIL_MISSING", "") == "1"
        if not bound_fp and not detail_missing:
            # No HTTPS binding reported for the domain (empty httpsconf).
            emit({
                "ok": True, "certId": None, "certFingerprint": None,
                "notBefore": None, "notAfter": None, "commonName": None,
                "certDetailMissing": False,
            })
            return EXIT_OK
        not_after = int(os.environ.get("MOCK_QINIU_NOT_AFTER", "0")) or int(time.time()) + 90 * 86400
        emit({
            "ok": True,
            "certId": os.environ.get("MOCK_QINIU_BOUND_CERT_ID", "mock-bound-certid"),
            "certFingerprint": None if detail_missing else bound_fp,
            "notBefore": not_after - 90 * 86400,
            "notAfter": None if detail_missing else not_after,
            "commonName": "*.mock.invalid",
            "certDetailMissing": detail_missing,
        })
        return EXIT_OK

    emit({"ok": False, "stage": "usage", "error": f"unknown command: {command}"})
    return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
