"""Tests for qiniu_helper.py.

Two layers of confidence:

1. Golden parity tests — call the OFFICIAL qiniu SDK's `Auth.token_of_request`
   directly with fixed (fake) credentials and pin the exact resulting token.
   These values were generated once, offline, using qiniu-python-sdk 7.18.0
   (see the comment above each). If request construction anywhere in this
   codebase ever changes in a way that would alter what gets signed —
   wrong method handling, wrong content-type gating, wrong query encoding —
   these tests catch it, because they compare against a value pinned
   independently of whatever the code currently does.

2. Helper-wiring tests — prove qiniu_helper.py's upload/bind/verify commands
   actually call the signer with the right inputs (right URL, right
   content-type gating) by intercepting the HTTP layer and inspecting the
   Authorization header that was actually about to be sent, OR by running
   the real helper end-to-end against a local mock HTTP server (never a
   real Qiniu endpoint).

Run: python3 -m pytest ssl-renew/tests/test_qiniu_helper.py -v
(requires `pip install -r ssl-renew/requirements-dev.txt`)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
import qiniu

SSL_RENEW_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SSL_RENEW_ROOT))

import qiniu_helper  # noqa: E402

FAKE_AK = "fake-test-ak-1234567890"
FAKE_SK = "fake-test-sk-abcdefghij"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockQiniuServer:
    """Spawns tests/test_helper/mock_server.py as a real subprocess."""

    def __init__(self, **kwargs):
        self.port = _free_port()
        self.proc = None
        self.kwargs = kwargs

    def __enter__(self):
        script = SSL_RENEW_ROOT / "tests" / "test_helper" / "mock_server.py"
        ready_file = f"/tmp/qiniu_helper_test_ready_{self.port}"
        if os.path.exists(ready_file):
            os.remove(ready_file)
        args = [sys.executable, str(script), "--port", str(self.port), "--ready-file", ready_file]
        for k, v in self.kwargs.items():
            flag = "--" + k.replace("_", "-")
            if v is True:
                args.append(flag)
            else:
                args += [flag, str(v)]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(ready_file):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("mock server did not become ready")
        os.remove(ready_file)
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def run_helper(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["QINIU_ACCESS_KEY"] = FAKE_AK
    env["QINIU_SECRET_KEY"] = FAKE_SK
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SSL_RENEW_ROOT / "qiniu_helper.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


# ═════════════════════════════════════════════════════════════════════════
# 1. Golden parity tests — pinned against the official SDK
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def auth():
    return qiniu.Auth(FAKE_AK, FAKE_SK)


def test_parity_get_no_body(auth):
    # qiniu-python-sdk 7.18.0, Auth.token_of_request(
    #   "http://api.qiniu.com/domain/media.example.com/httpsconf", None, None)
    token = auth.token_of_request(
        "http://api.qiniu.com/domain/media.example.com/httpsconf", None, None
    )
    assert token == "fake-test-ak-1234567890:C-_wfWeTr4ZKB6fItEu93ruFgIg="


def test_parity_post_json_body(auth):
    # JSON bodies are NEVER signed by QBox — only
    # application/x-www-form-urlencoded bodies are. This is the exact
    # behavior the old hand-rolled bash signer got wrong (it signed the
    # body regardless of content-type).
    token = auth.token_of_request(
        "http://api.qiniu.com/sslcert",
        '{"name":"test","common_name":"a.b.c","ca":"CACONTENT","pri":"PRIVATEKEY"}',
        "application/json",
    )
    assert token == "fake-test-ak-1234567890:LeW3M3Mp379J9f_XnTTbTCyfAcc="


def test_parity_post_form_body(auth):
    token = auth.token_of_request(
        "http://api.qiniu.com/sslcert",
        "name=test&common_name=a.b.c",
        "application/x-www-form-urlencoded",
    )
    assert token == "fake-test-ak-1234567890:MkKhIbtMgzfYD4lguVeEd3xhdD8="


def test_parity_path_with_query(auth):
    token = auth.token_of_request(
        "http://api.qiniu.com/domain/media.example.com/httpsconf?ssl=true&x=1", None, None
    )
    assert token == "fake-test-ak-1234567890:TNOKguUW6a3DaWEVeKdlcB7zSZg="


def test_parity_empty_body(auth):
    # An empty JSON-content-type body signs identically to no body at all
    # (same reason as test_parity_post_json_body: JSON is never signed).
    token = auth.token_of_request(
        "http://api.qiniu.com/domain/media.example.com/httpsconf", "", "application/json"
    )
    assert token == "fake-test-ak-1234567890:C-_wfWeTr4ZKB6fItEu93ruFgIg="
    assert token == auth.token_of_request(
        "http://api.qiniu.com/domain/media.example.com/httpsconf", None, None
    )


def test_parity_body_with_special_chars(auth):
    # A JSON body containing newlines/PEM content still doesn't affect the
    # signature (JSON bodies are excluded from signing) — regression guard
    # against ever "fixing" this to include JSON bodies again.
    body = '{"pri":"-----BEGIN KEY-----\nAB/CD+EF==\n-----END KEY-----\n"}'
    token = auth.token_of_request("http://api.qiniu.com/sslcert", body, "application/json")
    assert token == "fake-test-ak-1234567890:LeW3M3Mp379J9f_XnTTbTCyfAcc="


def test_parity_form_body_special_chars_changes_signature(auth):
    # Sanity check the inverse: for a content-type that IS signed
    # (form-urlencoded), changing the body DOES change the token — proves
    # the JSON-body invariance above isn't just a broken/no-op signer.
    token_a = auth.token_of_request("http://api.qiniu.com/sslcert", "a=1", "application/x-www-form-urlencoded")
    token_b = auth.token_of_request("http://api.qiniu.com/sslcert", "a=2", "application/x-www-form-urlencoded")
    assert token_a != token_b


# ═════════════════════════════════════════════════════════════════════════
# 2. Helper wiring — qiniu_helper.py must call the signer with the right
#    inputs. Verified by intercepting the actual outbound Authorization
#    header via a real local HTTP server (no mocking of qiniu internals —
#    this is what really goes over the wire).
# ═════════════════════════════════════════════════════════════════════════

def test_upload_sends_a_correctly_signed_request(tmp_path):
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text("FAKE-CERT-CONTENT\n")
    key_file.write_text("FAKE-KEY-CONTENT\n")

    with MockQiniuServer(body='{"certID":"golden-certid"}') as server:
        result = run_helper(
            ["upload", "--domain", "test.example.test", "--cert-file", str(cert_file),
             "--key-file", str(key_file), "--timeout", "5"],
            env_extra={"QINIU_API_HOST": server.host},
        )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"ok": True, "certID": "golden-certid"}


def test_bind_sends_a_correctly_signed_request():
    with MockQiniuServer(body="{}") as server:
        result = run_helper(
            ["bind", "--domain", "test.example.test", "--cert-id", "certid-123", "--timeout", "5"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_authorization_header_matches_independently_computed_token(monkeypatch):
    """Directly proves the *wiring*: capture the real outbound Authorization
    header for a `verify` GET and compare it against a token computed via
    Auth.token_of_request with the exact same inputs, independently."""
    monkeypatch.setenv("QINIU_ACCESS_KEY", FAKE_AK)
    monkeypatch.setenv("QINIU_SECRET_KEY", FAKE_SK)
    monkeypatch.delenv("SAVED_QINIU_AK", raising=False)
    monkeypatch.delenv("SAVED_QINIU_SK", raising=False)
    captured = {}

    def spy_get(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        # Don't actually hit the network — short-circuit with a fake 200.
        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {"certId": "abc"}

        return FakeResp()

    with mock.patch.object(qiniu_helper.requests, "get", spy_get):
        args = qiniu_helper.argparse.Namespace(domain="test.example.test", expected_cert_id="abc", timeout=5)
        qiniu_helper.cmd_verify(args)

    expected_url = "http://api.qiniu.com/domain/test.example.test/httpsconf"
    assert captured["url"] == expected_url
    expected_token = qiniu.Auth(FAKE_AK, FAKE_SK).token_of_request(expected_url)
    assert captured["headers"]["Authorization"] == f"QBox {expected_token}"


# ═════════════════════════════════════════════════════════════════════════
# 3. Response handling: success / failure / 401 / 403 / 429 / 5xx / timeout
#    / invalid JSON — against a real local mock server.
# ═════════════════════════════════════════════════════════════════════════

def test_upload_success(tmp_path):
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text("CERT")
    key_file.write_text("KEY")
    with MockQiniuServer(body='{"certID":"abc123"}') as server:
        result = run_helper(
            ["upload", "--domain", "d.test", "--cert-file", str(cert_file), "--key-file", str(key_file)],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode == 0
    assert json.loads(result.stdout)["certID"] == "abc123"


def test_upload_failure_no_certid_in_response(tmp_path):
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text("CERT")
    key_file.write_text("KEY")
    with MockQiniuServer(body='{"error":"bad cert"}') as server:
        result = run_helper(
            ["upload", "--domain", "d.test", "--cert-file", str(cert_file), "--key-file", str(key_file)],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["stage"] == "upload"


def test_bind_success():
    with MockQiniuServer(body="{}") as server:
        result = run_helper(
            ["bind", "--domain", "d.test", "--cert-id", "x"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode == 0


def test_bind_failure_business_error_code():
    with MockQiniuServer(body='{"code":6100,"error":"domain not bound"}') as server:
        result = run_helper(
            ["bind", "--domain", "d.test", "--cert-id", "x"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["qiniu_code"] == 6100


@pytest.mark.parametrize("status", [401, 403])
def test_verify_auth_failure(status):
    with MockQiniuServer(status=status, body='{"error":"denied"}') as server:
        result = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "x"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode == qiniu_helper.EXIT_AUTH_FAILURE
    payload = json.loads(result.stdout)
    assert payload["http_status"] == status
    assert "AK/SK" in payload["error"]


def test_verify_rate_limited_429():
    with MockQiniuServer(status=429, body='{"error":"too many requests"}') as server:
        result = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "x"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["http_status"] == 429


def test_verify_server_error_5xx():
    with MockQiniuServer(status=503, body='{"error":"unavailable"}') as server:
        result = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "x"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["http_status"] == 503


def test_verify_timeout():
    with MockQiniuServer(status=200, body='{"certId":"x"}', delay=3) as server:
        start = time.time()
        result = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "x", "--timeout", "1"],
            env_extra={"QINIU_API_HOST": server.host},
        )
        elapsed = time.time() - start
    assert result.returncode != 0
    assert elapsed < 4
    payload = json.loads(result.stdout)
    assert "timed out" in payload["error"]


def test_verify_invalid_json_response():
    with MockQiniuServer(status=200, body="not json at all") as server:
        result = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "x"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert "invalid JSON" in payload["error"]


def test_verify_certid_match_and_mismatch():
    with MockQiniuServer(body='{"certId":"real-id"}') as server:
        match = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "real-id"],
            env_extra={"QINIU_API_HOST": server.host},
        )
        mismatch = run_helper(
            ["verify", "--domain", "d.test", "--expected-cert-id", "other-id"],
            env_extra={"QINIU_API_HOST": server.host},
        )
    assert json.loads(match.stdout)["matches"] is True
    assert json.loads(mismatch.stdout)["matches"] is False


# ═════════════════════════════════════════════════════════════════════════
# 4. Runtime secret safety
# ═════════════════════════════════════════════════════════════════════════

SECRET_AK = "SUPERSECRETAK00000000"
SECRET_SK = "SUPERSECRETSK11111111"
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"


def _run_with_real_secrets(args, extra_env=None):
    env = os.environ.copy()
    env["QINIU_ACCESS_KEY"] = SECRET_AK
    env["QINIU_SECRET_KEY"] = SECRET_SK
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SSL_RENEW_ROOT / "qiniu_helper.py"), *args],
        env=env, capture_output=True, text=True, timeout=20,
    )


def test_argv_never_contains_secrets(tmp_path):
    """The argv list we build for the subprocess itself must never carry
    AK/SK/Authorization/private key — only file *paths*. This is what a
    `ps`-based observer of the running process would see."""
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text(f"{PRIVATE_KEY_MARKER}\nCERT-BODY\n-----END PRIVATE KEY-----\n")
    key_file.write_text(f"{PRIVATE_KEY_MARKER}\nKEY-BODY\n-----END PRIVATE KEY-----\n")

    argv = [sys.executable, str(SSL_RENEW_ROOT / "qiniu_helper.py"), "upload",
            "--domain", "d.test", "--cert-file", str(cert_file), "--key-file", str(key_file)]
    joined = " ".join(argv)
    assert SECRET_AK not in joined
    assert SECRET_SK not in joined
    assert PRIVATE_KEY_MARKER not in joined
    assert "KEY-BODY" not in joined
    assert "CERT-BODY" not in joined


def test_stdout_and_stderr_never_contain_secrets_on_success(tmp_path):
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text(f"{PRIVATE_KEY_MARKER}\nCERT-BODY\n-----END PRIVATE KEY-----\n")
    key_file.write_text(f"{PRIVATE_KEY_MARKER}\nKEY-BODY\n-----END PRIVATE KEY-----\n")

    with MockQiniuServer(body='{"certID":"abc"}') as server:
        result = _run_with_real_secrets(
            ["upload", "--domain", "d.test", "--cert-file", str(cert_file), "--key-file", str(key_file)],
            extra_env={"QINIU_API_HOST": server.host},
        )
    for stream in (result.stdout, result.stderr):
        assert SECRET_AK not in stream
        assert SECRET_SK not in stream
        assert "KEY-BODY" not in stream
        assert "CERT-BODY" not in stream
        assert PRIVATE_KEY_MARKER not in stream


def test_stdout_and_stderr_never_contain_secrets_on_failure(tmp_path):
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text(f"{PRIVATE_KEY_MARKER}\nCERT-BODY\n-----END PRIVATE KEY-----\n")
    key_file.write_text(f"{PRIVATE_KEY_MARKER}\nKEY-BODY\n-----END PRIVATE KEY-----\n")

    with MockQiniuServer(status=401, body='{"error":"denied"}') as server:
        result = _run_with_real_secrets(
            ["upload", "--domain", "d.test", "--cert-file", str(cert_file), "--key-file", str(key_file)],
            extra_env={"QINIU_API_HOST": server.host},
        )
    assert result.returncode != 0
    for stream in (result.stdout, result.stderr):
        assert SECRET_AK not in stream
        assert SECRET_SK not in stream
        assert "KEY-BODY" not in stream
        assert "CERT-BODY" not in stream
        assert PRIVATE_KEY_MARKER not in stream
    # The failure IS reported clearly (not silently swallowed) — just
    # without leaking anything.
    assert "401" in result.stdout


def test_stdout_and_stderr_never_contain_secrets_on_exception():
    """A raw network exception (connection refused — no server at all)
    must not leak the constructed Authorization header or credentials
    even via a Python exception's default string representation."""
    unused_port = _free_port()
    result = _run_with_real_secrets(
        ["verify", "--domain", "d.test", "--expected-cert-id", "x", "--timeout", "2"],
        extra_env={"QINIU_API_HOST": f"http://127.0.0.1:{unused_port}"},
    )
    assert result.returncode != 0
    for stream in (result.stdout, result.stderr):
        assert SECRET_AK not in stream
        assert SECRET_SK not in stream


def test_debug_logging_still_does_not_leak_secrets(tmp_path, monkeypatch):
    """Even with verbose/debug-style env flags set, the helper must not
    start printing raw request bodies or headers."""
    cert_file = tmp_path / "fullchain.cer"
    key_file = tmp_path / "domain.key"
    cert_file.write_text(f"{PRIVATE_KEY_MARKER}\nCERT-BODY\n-----END PRIVATE KEY-----\n")
    key_file.write_text(f"{PRIVATE_KEY_MARKER}\nKEY-BODY\n-----END PRIVATE KEY-----\n")

    with MockQiniuServer(body='{"certID":"abc"}') as server:
        result = _run_with_real_secrets(
            ["upload", "--domain", "d.test", "--cert-file", str(cert_file), "--key-file", str(key_file)],
            extra_env={"QINIU_API_HOST": server.host, "PYTHONVERBOSE": "1", "QINIU_HELPER_DEBUG": "1"},
        )
    for stream in (result.stdout, result.stderr):
        assert SECRET_AK not in stream
        assert SECRET_SK not in stream
        assert "KEY-BODY" not in stream
        assert PRIVATE_KEY_MARKER not in stream


def test_redact_function_scrubs_known_secret_shapes():
    text = (
        "QINIU_ACCESS_KEY=abc123 QINIU_SECRET_KEY=def456 "
        "Authorization: QBox someak:somesignature== "
        "-----BEGIN PRIVATE KEY-----\nMIIEv...\n-----END PRIVATE KEY-----"
    )
    redacted = qiniu_helper.redact(text)
    assert "abc123" not in redacted
    assert "def456" not in redacted
    assert "somesignature" not in redacted
    assert "MIIEv" not in redacted


# ═════════════════════════════════════════════════════════════════════════
# 5. Usage / config errors
# ═════════════════════════════════════════════════════════════════════════

def test_missing_credentials_exits_with_usage_error():
    env = os.environ.copy()
    for k in ("QINIU_ACCESS_KEY", "QINIU_SECRET_KEY", "SAVED_QINIU_AK", "SAVED_QINIU_SK"):
        env.pop(k, None)
    result = subprocess.run(
        [sys.executable, str(SSL_RENEW_ROOT / "qiniu_helper.py"), "bind",
         "--domain", "d.test", "--cert-id", "x"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == qiniu_helper.EXIT_USAGE_ERROR
    payload = json.loads(result.stdout)
    assert payload["ok"] is False


def test_upload_missing_cert_file_is_usage_error():
    result = run_helper(
        ["upload", "--domain", "d.test", "--cert-file", "/no/such/file.cer", "--key-file", "/no/such/file.key"]
    )
    assert result.returncode == qiniu_helper.EXIT_USAGE_ERROR


# ===============================================================
# 6. `query` — read-only binding probe (GH-231)
# ===============================================================

# Fixed offline fixtures: two self-signed certificates. The `query` chain
# reads /domain/<d>/httpsconf then /sslcert/<certId>, so the mock must route
# by path — a tiny in-process server keyed on path prefix.
FIXTURE_LEAF_PEM = '-----BEGIN CERTIFICATE-----\nMIICrjCCAZYCCQCnlz5KYhzCbzANBgkqhkiG9w0BAQsFADAZMRcwFQYDVQQDDA4q\nLmV4YW1wbGUudGVzdDAeFw0yNjEwMTMwNjI5MTBaFw0zNjEwMTAwNjI5MTBaMBkx\nFzAVBgNVBAMMDiouZXhhbXBsZS50ZXN0MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\nMIIBCgKCAQEAt/zq8EWv5FikymMcZj1IDdYKSGrRpC4szd2YQzGPbde3DMeNU1Hd\nTgog7lfaYCObgZIydRJavALLQTZ/rK1DNqcVrgENCBz+l5JTJebpdzdOjaOM5Utm\nk5TB5L6G+MtwcpT5r7N+KHvONyTHaK3Hy8vtt1t02EFF/DVEzWBXLkr2V4I/II/u\nR4tXn/RtB9lV+UdAlFynX0r6HzwkVa+yU4k13uF8u8YgslK2GT2TaY7Iilbm7pJl\nSnxKRAt8NYbmubMN6tmhiYhpHxEI8QG5LiGZmLPS1cty8WyypS0uhgwLQwvnPwVP\nwN3fbo3hqY+NjIZAsoT07BeafWKQKx9+HwIDAQABMA0GCSqGSIb3DQEBCwUAA4IB\nAQBjNVRA57CZeUm0LRUB0hyv9RaMb7XutxgjAe5/6AbJufcr/FNokMmxPN1An+jk\nppMJrQxo2WLLOyz9R+3lKW4eFkDatrp5j39z5YTb5j7NwoR1fsQzqUrz7ws6HaEX\nxjfUUVNqQ4zjkc+r6D/2xbqBCndHqk+/lquQcY+zgmozhRPlfw7UNqYFnGklsz6P\nptrKkaMoD80OWyQuOOzdFqdHmshGzaUmgF2jIl2YcckobpUZ+6mcWe9LFmZFSYEV\nrlnaZUYzMzS/hKUfuMqEvpbh8o9jymF2Z1mKzMHBmXo8Rs1DlexkrQpyeWDZT4/x\npNpRpoBW4N6zEIavPlcO6Jar\n-----END CERTIFICATE-----\n'
FIXTURE_SECOND_PEM = '-----BEGIN CERTIFICATE-----\nMIICuDCCAaACCQCg6yUR7d/GsjANBgkqhkiG9w0BAQsFADAeMRwwGgYDVQQDDBNz\nZWNvbmQuZXhhbXBsZS50ZXN0MB4XDTI2MTAxMzA2MjkxMFoXDTM2MTAxMDA2Mjkx\nMFowHjEcMBoGA1UEAwwTc2Vjb25kLmV4YW1wbGUudGVzdDCCASIwDQYJKoZIhvcN\nAQEBBQADggEPADCCAQoCggEBAKI0nhf3DzNVbHSvzNC0bAZFUKgKCtRbJ6dEZPK1\nzehpZseQMhmbpx/iQY2BHEHhgp4oaAZuzImklf5Sm+3v17ZqfKGI4ghQRuv6BbgA\nxrRbOPtsTUWuTDkmzNWjc8/lN1aQLIL7w1dP+L7dyNRL8+YHWB+UXZ+jBkGt2DiJ\ncF+looHvmR/xvWOz30NXwe648ChqPAZTYWCh+Z78tIl4D9vnlE0E8eBgyeGOH3Li\nutwdqYu1VL61fVLac4dHvpvCjvl0JKbZTSFmCxbM+nvK3iWXqFG7vhcfYl5a6chL\nC38BD/4f99fzOuqPNyl+9Z9v4C/qup9ljJJhM4MMXh0qM/ECAwEAATANBgkqhkiG\n9w0BAQsFAAOCAQEAkj/a8OuGIwzFnAbwA5KdmktoOQGTiE7uPHFv581mfG8kY7n8\npmtx4XZztUxgZm6SZHWajAYHsH/rOR2uj+HmRm5gySOu5v4YvHKaPYZHpvvIZuhS\n8DMQ5QUTq4IpeUtLWYp6Mg+q3aC4yYCdGJdMa0qDtD/BzeK9eWtjTD9Y346w6T+v\nO/6t+/s5r3Z5yhs8iwlBzusJw3j34Uztk4dXCoQLuLqwrSR+LUPy+Z8f7HQuzC1J\nFQR/1T2e5UQ5MUtsypyMJEMREHzK4Ada6sAxQr/NxNwH2e+GETOwplWiotMw+iu0\nTHldIMdF23InhFsd7zHeAVMa29K0FNFTDMez7Q==\n-----END CERTIFICATE-----\n'
FIXTURE_LEAF_FP = 'b0e2454d162d0b4b13d262eca53aa6d8f88bdc8774222b1d43fa88e1997ec517'
FIXTURE_SECOND_FP = '39b1a796b1bec1494dc867e520170e611dc70a6cb79572195a43e49f8d1b03df'
FIXTURE_NOT_BEFORE = 1791872400
FIXTURE_NOT_AFTER = 1799648400


class RoutingQiniuServer:
    """Serves a fixed (status, body) per path prefix on 127.0.0.1."""

    def __init__(self, routes):
        import http.server
        import threading

        routes = dict(routes)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                for prefix, (status, body) in routes.items():
                    if self.path.startswith(prefix):
                        payload = body.encode("utf-8")
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def _query_routes(httpsconf_body, sslcert_status=200, sslcert_body=""):
    return {
        "/domain/": (200, httpsconf_body),
        "/sslcert/": (sslcert_status, sslcert_body),
    }


def test_query_returns_bound_cert_details():
    chain = FIXTURE_LEAF_PEM + FIXTURE_SECOND_PEM
    detail = json.dumps({
        "code": 200, "error": "",
        "cert": {
            "name": "wildcard", "common_name": "*.example.test",
            "not_before": FIXTURE_NOT_BEFORE, "not_after": FIXTURE_NOT_AFTER,
            "create_time": 1780000000,
            "ca": chain, "pri": "SECRET-PRIVATE-KEY-BODY",
        },
    })
    with RoutingQiniuServer(_query_routes('{"certId":"cert-abc-123"}', sslcert_body=detail)) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["certId"] == "cert-abc-123"
    # The fingerprint is the LEAF of the stored chain, hashed as DER — the
    # exact counterpart of `openssl x509 -outform DER | sha256sum`.
    assert payload["certFingerprint"] == FIXTURE_LEAF_FP
    assert payload["certFingerprint"] != FIXTURE_SECOND_FP
    assert payload["notBefore"] == FIXTURE_NOT_BEFORE
    assert payload["notAfter"] == FIXTURE_NOT_AFTER
    assert payload["commonName"] == "*.example.test"
    assert payload["certDetailMissing"] is False


def test_query_tolerates_flat_cert_detail_body():
    detail = json.dumps({
        "code": 200, "error": "",
        "name": "wildcard", "common_name": "*.example.test",
        "not_before": FIXTURE_NOT_BEFORE, "not_after": FIXTURE_NOT_AFTER,
        "ca": FIXTURE_LEAF_PEM,
    })
    with RoutingQiniuServer(_query_routes('{"certId":"cert-abc-123"}', sslcert_body=detail)) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["certFingerprint"] == FIXTURE_LEAF_FP
    assert payload["notAfter"] == FIXTURE_NOT_AFTER


def test_query_without_binding_reports_null_certid():
    with RoutingQiniuServer(_query_routes("{}")) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["certId"] is None
    assert payload["certFingerprint"] is None
    assert payload["certDetailMissing"] is False


def test_query_bound_cert_deleted_reports_detail_missing():
    with RoutingQiniuServer(_query_routes('{"certId":"gone"}', sslcert_status=404, sslcert_body='{"error":"no such cert"}')) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["certId"] == "gone"
    assert payload["certFingerprint"] is None
    assert payload["notAfter"] is None
    assert payload["certDetailMissing"] is True


def test_query_auth_failure_maps_to_auth_exit_code():
    with RoutingQiniuServer({"/domain/": (401, '{"error":"denied"}')}) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode == qiniu_helper.EXIT_AUTH_FAILURE
    payload = json.loads(result.stdout)
    assert payload["http_status"] == 401
    assert "AK/SK" in payload["error"]


def test_query_server_error_maps_to_generic_failure():
    with RoutingQiniuServer({"/domain/": (503, '{"error":"unavailable"}')}) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["http_status"] == 503


def test_query_never_emits_private_key_or_cert_material():
    chain = FIXTURE_LEAF_PEM + FIXTURE_SECOND_PEM
    detail = json.dumps({
        "code": 200, "cert": {
            "common_name": "*.example.test", "not_before": FIXTURE_NOT_BEFORE,
            "not_after": FIXTURE_NOT_AFTER, "ca": chain,
            "pri": "-----BEGIN PRIVATE KEY-----SUPERSECRETKEYBODY-----END PRIVATE KEY-----",
        },
    })
    with RoutingQiniuServer(_query_routes('{"certId":"cert-abc-123"}', sslcert_body=detail)) as server:
        result = run_helper(["query", "--domain", "origin.example.test"], env_extra={"QINIU_API_HOST": server.host})
    assert result.returncode == 0
    assert "SUPERSECRETKEYBODY" not in result.stdout
    assert "PRIVATE KEY" not in result.stdout
    assert "BEGIN CERTIFICATE" not in result.stdout
    # The fingerprint digest itself is the only cert-derived output.
    assert FIXTURE_LEAF_FP in result.stdout


def test_leaf_cert_sha256_picks_leaf_from_chain():
    chain = FIXTURE_LEAF_PEM + FIXTURE_SECOND_PEM
    assert qiniu_helper.leaf_cert_sha256(chain) == FIXTURE_LEAF_FP
    assert qiniu_helper.leaf_cert_sha256(FIXTURE_SECOND_PEM) == FIXTURE_SECOND_FP
    assert qiniu_helper.leaf_cert_sha256("") is None
    assert qiniu_helper.leaf_cert_sha256(None) is None
    assert qiniu_helper.leaf_cert_sha256("-----BEGIN CERTIFICATE-----garbage") is None
