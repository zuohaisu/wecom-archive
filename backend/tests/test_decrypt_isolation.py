"""
Tests for RND-231 — app.services.decrypt_isolation, the subprocess
isolation layer around the WeCom C SDK's DecryptData call.

RND-208 root-caused a small set of historical WeCom archive payloads to
SIGSEGV (exit code 139) inside libWeWorkFinanceSdkC.so's DecryptData — an
SDK-internal defect, not fixable or avoidable in this codebase (SDK cannot
be replaced). Before RND-231, that crash took down the whole Python
process running scripts/decrypt_wecom_messages_once.py or
scripts/backfill_revoke_associations_once.py mid-batch. This file proves
the isolation mechanism actually contains a real SIGSEGV to one child
process (no real WeCom SDK involved — a self-inflicted `os.kill(getpid(),
SIGSEGV)` in a throwaway child reproduces the exact signal/exit-code shape
RND-208 observed), and that malformed input is rejected before ever
reaching ctypes — with an explicit regression guard proving a normal
88-byte encrypt_key (WeCom's real SDK-internal protocol format for
ver=4 payloads — see RND-231's correction) is never rejected by length.

Run (from backend/):
    pytest tests/test_decrypt_isolation.py -v

On macOS, the three tests that intentionally crash a child process are
skipped by default (CrashReporter dialogs). Re-enable with
WECOM_RUN_CRASH_SIGNAL_TESTS=1.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from app.services.decrypt_isolation import (
    MALFORMED_INPUT_SENTINEL,
    OTHER_SENTINEL,
    SIGSEGV_SENTINEL,
    IsolatedDecryptResult,
    MalformedDecryptInput,
    decrypt_message_isolated,
    run_isolated,
    validate_decrypt_inputs,
)

# On macOS, a child Python process that exits on a real signal triggers
# CrashReporter dialogs during `make verify`. These three tests exist
# precisely to exercise REAL SIGSEGV/SIGABRT semantics, for which there is
# no lightweight way to keep the signal shape while suppressing the macOS
# crash UI. Skip them locally by default; run them explicitly to validate
# the isolation mechanism on macOS:
#   WECOM_RUN_CRASH_SIGNAL_TESTS=1 pytest tests/test_decrypt_isolation.py -v
_SKIP_REAL_CRASH_TESTS_ON_MACOS = pytest.mark.skipif(
    sys.platform == "darwin"
    and os.environ.get("WECOM_RUN_CRASH_SIGNAL_TESTS") != "1",
    reason=(
        "macOS displays CrashReporter dialogs for these intentionally "
        "crashed Python child processes; set WECOM_RUN_CRASH_SIGNAL_TESTS=1 "
        "to run them explicitly."
    ),
)

# ---------------------------------------------------------------------------
# validate_decrypt_inputs — malformed-input pre-flight rejection
# ---------------------------------------------------------------------------


def test_rejects_empty_encrypt_key():
    with pytest.raises(MalformedDecryptInput):
        validate_decrypt_inputs("", "msg")


def test_rejects_empty_encrypt_msg():
    with pytest.raises(MalformedDecryptInput):
        validate_decrypt_inputs("key", "")


def test_rejects_embedded_nul_byte_in_key():
    with pytest.raises(MalformedDecryptInput):
        validate_decrypt_inputs("bad\x00key", "msg")


def test_rejects_embedded_nul_byte_in_msg():
    with pytest.raises(MalformedDecryptInput):
        validate_decrypt_inputs("key", "bad\x00msg")


def test_rejects_excessively_long_encrypt_key():
    with pytest.raises(MalformedDecryptInput):
        validate_decrypt_inputs("k" * 100_000, "msg")


def test_rejects_non_string_input():
    with pytest.raises(MalformedDecryptInput):
        validate_decrypt_inputs(None, "msg")  # type: ignore[arg-type]


def test_accepts_normal_88_byte_encrypt_key():
    """RND-231 correction — regression guard: an 88-byte encrypt_key is
    WeCom's normal SDK-internal protocol format (production's 3,538
    ver=4 rows decrypt successfully with exactly this format). This must
    NEVER be rejected on length grounds — only genuinely malformed input
    (NUL bytes, non-str, absurd length) is rejected. If this test ever
    fails because someone reintroduces a length-floor check, that check
    is wrong and must be removed."""
    validate_decrypt_inputs("k" * 88, "some-encrypted-message-blob")


def test_accepts_short_encrypt_key():
    """Equally, a short key (e.g. the 16-byte AES-key convention used
    elsewhere in WeCom's docs) must not be rejected either — there is no
    length floor at all, only a ceiling."""
    validate_decrypt_inputs("k" * 16, "msg")


# ---------------------------------------------------------------------------
# run_isolated — generic subprocess-isolation mechanism (no real SDK)
# ---------------------------------------------------------------------------


def _crash_with_sigsegv(conn):
    os.kill(os.getpid(), signal.SIGSEGV)


def _crash_with_sigabrt(conn):
    os.kill(os.getpid(), signal.SIGABRT)


def _send_success(conn):
    conn.send(("success", 0, '{"ok": true}', ""))


def _sleep_forever(conn):
    time.sleep(120)


def test_run_isolated_returns_child_result_on_success():
    message, exitcode = run_isolated(_send_success, (), timeout=10)
    assert message == ("success", 0, '{"ok": true}', "")
    assert exitcode == 0


@_SKIP_REAL_CRASH_TESTS_ON_MACOS
def test_run_isolated_classifies_sigsegv():
    """The core RND-231 guarantee: a real SIGSEGV (signal 11 — exactly
    what RND-208 observed as exit code 139 = 128 + 11) in the child is
    detected via its exit code, not propagated to this test process."""
    message, exitcode = run_isolated(_crash_with_sigsegv, (), timeout=10)
    assert message is None
    assert exitcode == -11


@_SKIP_REAL_CRASH_TESTS_ON_MACOS
def test_run_isolated_classifies_other_signal_distinctly_from_sigsegv():
    message, exitcode = run_isolated(_crash_with_sigabrt, (), timeout=10)
    assert message is None
    assert exitcode == -6  # SIGABRT, must not be mislabelled as sigsegv


def test_run_isolated_times_out_and_kills_child():
    started = time.monotonic()
    message, exitcode = run_isolated(_sleep_forever, (), timeout=1)
    elapsed = time.monotonic() - started
    assert message is None
    assert elapsed < 10  # killed promptly, never waited for the 120s sleep


# ---------------------------------------------------------------------------
# decrypt_message_isolated — production-facing wrapper
# ---------------------------------------------------------------------------


def test_decrypt_message_isolated_rejects_malformed_input_before_spawning():
    """Malformed input must never reach the SDK -- raises synchronously,
    proving no child was ever spawned (a bogus lib_path would otherwise
    surface as an "other" IsolatedDecryptResult, not an exception)."""
    with pytest.raises(MalformedDecryptInput):
        decrypt_message_isolated("/nonexistent/lib.so", "bad\x00key", "msg")


def test_decrypt_message_isolated_reports_other_for_missing_sdk_library():
    """A lib_path that doesn't exist on disk fails inside the child
    (wecom_sdk.load_sdk raises FileNotFoundError) -- caught there and
    reported as outcome="other", never raised in the parent and never a
    crash. No real WeCom .so is needed for this assertion."""
    result = decrypt_message_isolated("/definitely/not/a/real/path.so", "k" * 88, "msg")
    assert isinstance(result, IsolatedDecryptResult)
    assert result.outcome == "other"


def _crash_decrypt_child_target(conn, lib_path, encrypt_key, encrypt_msg):
    """Module-level (not nested) so multiprocessing's "spawn" context can
    pickle it by module+qualname reference when substituted for the real
    _decrypt_child_target below."""
    os.kill(os.getpid(), signal.SIGSEGV)


@_SKIP_REAL_CRASH_TESTS_ON_MACOS
def test_decrypt_message_isolated_classifies_real_sigsegv_via_child_crash(monkeypatch):
    """End-to-end: force the isolated child to SIGSEGV instead of running
    the real (unavailable-in-CI) SDK, and confirm decrypt_message_isolated
    reports outcome="sigsegv" rather than raising or hanging."""
    import app.services.decrypt_isolation as isolation_module

    monkeypatch.setattr(
        isolation_module, "_decrypt_child_target", _crash_decrypt_child_target
    )
    result = decrypt_message_isolated("/fake/lib.so", "k" * 88, "msg")
    assert result.outcome == "sigsegv"
    assert result.decrypted_json is None


# ---------------------------------------------------------------------------
# Sentinel return codes — used by app.services.decrypt_worker to fold
# isolation outcomes back into its (return_code, decrypted_json) contract
# ---------------------------------------------------------------------------


def test_sentinels_are_distinct_and_negative():
    sentinels = {SIGSEGV_SENTINEL, OTHER_SENTINEL, MALFORMED_INPUT_SENTINEL}
    assert len(sentinels) == 3
    assert all(s < 0 for s in sentinels)
