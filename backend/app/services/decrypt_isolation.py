"""
Subprocess isolation for the WeCom C SDK's DecryptData call (RND-231).

RND-208 root-caused a small, reproducible set of historical WeCom archive
payloads to SIGSEGV (exit code 139) inside libWeWorkFinanceSdkC.so's
DecryptData — a defect internal to the vendored SDK binary, not something
this codebase can fix or work around by avoiding some usage pattern (SDK
cannot be replaced — see docs/ai/known-pitfalls.md #4). Historically that
crash took down the whole Python process running
scripts/decrypt_wecom_messages_once.py or
scripts/backfill_revoke_associations_once.py, aborting an entire batch
over one bad row.

This module contains the blast radius: DecryptData is invoked in a fresh
child process (multiprocessing "spawn" — never "fork", so the child never
inherits this process's already-open DB session/SQLAlchemy engine/SDK
state). When the SDK crashes, only that child process dies; the parent
observes the child's exit status and reports the crash back as an
IsolatedDecryptResult outcome ("sigsegv"), never a fatal signal that kills
the caller. app.services.decrypt_worker._decrypt_message() is the single
call site that wires this in — see its docstring — so both
scripts/decrypt_wecom_messages_once.py's live loop and
scripts/backfill_revoke_associations_once.py's historical-recovery pass
benefit with no change to their own business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import get_context

# RND-231 correction: do NOT reject on encrypt_key being "too short" (e.g.
# < 32 bytes). An 88-byte encrypt_key (after RSA-decrypt) is WeCom's normal
# SDK-internal protocol format — production's 3,538 ver=4 rows decrypt
# successfully with exactly that 88-byte format. A length-floor rejection
# here would reject normal data wholesale and manufacture a new outage.
# These ceilings exist only to bound the child process's memory / catch
# input that is unambiguously not a real WeCom payload; they are
# deliberately generous.
_MAX_ENCRYPT_KEY_LEN = 4096
_MAX_ENCRYPT_MSG_LEN = 50 * 1024 * 1024

_DEFAULT_TIMEOUT_SECONDS = 15.0

# Sentinel return codes (never produced by the real WeCom SDK, which
# returns 0 on success and small positive error codes otherwise) used by
# app.services.decrypt_worker._decrypt_message() to fold isolation
# outcomes back into its existing (return_code, decrypted_json) contract
# without changing that contract's shape for its other caller
# (scripts/backfill_revoke_associations_once.py's
# recover_historical_revoke_structured_content).
SIGSEGV_SENTINEL = -11
OTHER_SENTINEL = -3
MALFORMED_INPUT_SENTINEL = -4


class MalformedDecryptInput(Exception):
    """Raised by validate_decrypt_inputs() for input that must never reach
    the C SDK. Caller must count this as a rejected/failed row and must
    NEVER pass the offending value into ctypes."""


def validate_decrypt_inputs(encrypt_key: str, encrypt_msg: str) -> None:
    """Pre-flight rejection of input that must never reach the C SDK.

    Deliberately does NOT reject on length being "too short" — see the
    module docstring's RND-231 correction. Only rejects:
      - empty / None / non-str values
      - embedded NUL bytes (a ctypes c_char_p silently truncates at the
        first NUL, which can desync the SDK's internal length bookkeeping
        and is a plausible crash contributor)
      - length far beyond any plausible real WeCom payload (guards the
        isolated child's memory, not a "this looks suspicious" heuristic)
    """
    for name, value, max_len in (
        ("encrypt_key", encrypt_key, _MAX_ENCRYPT_KEY_LEN),
        ("encrypt_msg", encrypt_msg, _MAX_ENCRYPT_MSG_LEN),
    ):
        if not isinstance(value, str) or not value:
            raise MalformedDecryptInput(f"{name} is empty or not a string")
        if "\x00" in value:
            raise MalformedDecryptInput(f"{name} contains an embedded NUL byte")
        if len(value) > max_len:
            raise MalformedDecryptInput(f"{name} exceeds max length ({max_len})")


@dataclass
class IsolatedDecryptResult:
    outcome: str  # "success" | "sdk_decrypt_failed" | "sigsegv" | "other"
    return_code: "int | None"
    decrypted_json: "str | None"
    detail: str = ""  # short, safe diagnostic — never key/msg content


def _decrypt_child_target(conn, lib_path: str, encrypt_key: str, encrypt_msg: str) -> None:
    """Entry point for the isolated child process (spawn context).

    Loads the SDK fresh in this process, calls DecryptData exactly once,
    and sends the result back over `conn`. Any ordinary Python-level
    exception here is caught and reported as outcome="other" — only a
    crash inside the C call itself (DecryptData) can escape this function
    without sending a result, which the parent detects via the child's
    exit code (see decrypt_message_isolated).
    """
    try:
        from app.sdk import wecom_sdk

        lib = wecom_sdk.load_sdk(lib_path)
        wecom_sdk.configure_sdk_decrypt_data(lib)

        slice_ptr = wecom_sdk.new_slice(lib)
        if not slice_ptr:
            conn.send(("other", None, None, "slice allocation failed"))
            return
        try:
            ret = wecom_sdk.decrypt_data(lib, encrypt_key, encrypt_msg, slice_ptr)
            if ret != 0:
                conn.send(("sdk_decrypt_failed", ret, None, ""))
                return
            slice_len = wecom_sdk.get_slice_len(lib, slice_ptr)
            if slice_len <= 0:
                conn.send(("sdk_decrypt_failed", ret, None, "empty result"))
                return
            raw = wecom_sdk.get_content_from_slice(lib, slice_ptr)
            if raw is None:
                conn.send(("sdk_decrypt_failed", ret, None, "null content"))
                return
            conn.send(("success", ret, raw.decode("utf-8"), ""))
        finally:
            try:
                wecom_sdk.free_slice(lib, slice_ptr)
            except Exception:
                pass
    except Exception as exc:
        try:
            conn.send(("other", None, None, type(exc).__name__))
        except Exception:
            pass


def run_isolated(target, args: tuple, timeout: float) -> "tuple[tuple | None, int | None]":
    """Generic subprocess-isolation runner (RND-231 mechanism, decoupled
    from the WeCom SDK so it can be exercised in tests without a real
    libWeWorkFinanceSdkC.so — see tests/test_decrypt_isolation.py).

    Spawns `target(conn, *args)` in a fresh "spawn"-context child process.
    Returns (message, exitcode):
      - message is the tuple the child sent via conn.send(...), or None if
        the child crashed / timed out before sending anything.
      - exitcode is the child's multiprocessing.Process.exitcode: 0 on a
        normal return, a small positive int if the child called
        sys.exit(n) or raised past _decrypt_child_target's own try/except
        (should not happen in practice), or a *negative* value -N if the
        child was killed by signal N (POSIX) — e.g. -11 for SIGSEGV.

    Never raises for a child-side failure of any kind (bad input inside
    the child, SIGSEGV, timeout) — always returns, so a caller looping
    over many rows can classify the outcome and continue unconditionally.
    """
    ctx = get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=target, args=(child_conn, *args), daemon=True)
    process.start()
    child_conn.close()  # parent never writes

    message = None
    try:
        if parent_conn.poll(timeout):
            try:
                message = parent_conn.recv()
            except EOFError:
                message = None
        else:
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()
    finally:
        parent_conn.close()

    process.join(timeout=5)
    return message, process.exitcode


def decrypt_message_isolated(
    lib_path: str,
    encrypt_key: str,
    encrypt_msg: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> IsolatedDecryptResult:
    """Run one DecryptData call in an isolated child process.

    Raises MalformedDecryptInput before ever spawning a child if the input
    fails validate_decrypt_inputs() — the SDK must never see unvalidated
    input. Otherwise never raises: a bad SDK return code, a SIGSEGV, or a
    timeout are all reported as an IsolatedDecryptResult so a caller
    looping over many historical rows can classify the outcome for one row
    and unconditionally continue to the next (the core RND-231 guarantee).
    """
    validate_decrypt_inputs(encrypt_key, encrypt_msg)

    message, exitcode = run_isolated(
        _decrypt_child_target, (lib_path, encrypt_key, encrypt_msg), timeout
    )

    if message is not None:
        outcome, return_code, decrypted_json, detail = message
        return IsolatedDecryptResult(outcome, return_code, decrypted_json, detail)

    # No result was sent — the child crashed or timed out.
    if exitcode is not None and exitcode < 0:
        # POSIX: a negative exitcode -N means the child was killed by
        # signal N. Signal 11 (SIGSEGV) is exactly the "exit code 139"
        # (128 + 11) RND-208 root-caused to DecryptData; any other signal
        # is reported as "other" rather than mis-labelled sigsegv.
        signum = -exitcode
        if signum == 11:
            return IsolatedDecryptResult("sigsegv", None, None, f"signal {signum}")
        return IsolatedDecryptResult("other", None, None, f"signal {signum}")

    return IsolatedDecryptResult("other", None, None, f"exitcode {exitcode}")
