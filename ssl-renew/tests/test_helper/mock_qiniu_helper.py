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
"""
import json
import os
import sys

EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_USAGE_ERROR = 2
EXIT_AUTH_FAILURE = 4


def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")


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
        emit({"ok": True})
        return EXIT_OK

    if command == "verify":
        expected = get_opt("--expected-cert-id")
        actual = os.environ.get("MOCK_QINIU_ACTUAL_CERT_ID", expected)
        emit({"ok": True, "certId": actual, "matches": actual == expected})
        return EXIT_OK

    emit({"ok": False, "stage": "usage", "error": f"unknown command: {command}"})
    return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
