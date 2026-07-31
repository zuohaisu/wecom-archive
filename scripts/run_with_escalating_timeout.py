#!/usr/bin/env python3
"""Run a command with bounded, escalating timeouts.

A full test suite can occasionally exceed its usual duration on a busy
machine. Retrying with larger limits distinguishes that from a persistent
hang without retrying forever. Child output is streamed unchanged, while the
latest pytest-style percentage marker is retained for timeout diagnostics.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

TIMEOUTS_SECONDS = (300, 600, 1200)
_PROGRESS_PATTERN = re.compile(r"\[\s*(\d{1,3})%\]")


def _stream_output(process: subprocess.Popen[str], output: queue.Queue[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        output.put(line)
    process.stdout.close()


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the command and any children it started."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _run_attempt(command: Sequence[str], timeout: int, attempt: int) -> tuple[bool, int]:
    print(f"test: attempt {attempt}/{len(TIMEOUTS_SECONDS)} (timeout: {timeout}s)", flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    output: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_stream_output, args=(process, output), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    progress: str | None = None

    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            print(
                f"test: attempt {attempt} timed out after {timeout}s; "
                f"last observed progress: {progress or 'no percentage marker'}",
                flush=True,
            )
            return True, 1
        try:
            line = output.get(timeout=min(remaining, 0.25))
        except queue.Empty:
            continue
        print(line, end="", flush=True)
        match = _PROGRESS_PATTERN.search(line)
        if match:
            progress = f"{match.group(1)}%"

    reader.join()
    while not output.empty():
        line = output.get_nowait()
        print(line, end="", flush=True)
        match = _PROGRESS_PATTERN.search(line)
        if match:
            progress = f"{match.group(1)}%"
    return False, process.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run (prefix with --)")
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    for attempt, timeout in enumerate(TIMEOUTS_SECONDS, start=1):
        timed_out, returncode = _run_attempt(command, timeout, attempt)
        if not timed_out:
            return returncode
        if attempt < len(TIMEOUTS_SECONDS):
            print("test: retrying with an increased timeout.", flush=True)

    print("test: timed out after 1200s on the final attempt; likely real hang. Stopping.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
