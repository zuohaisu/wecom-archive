"""Shared Node execution helper for tests that execute embedded console/
search/diagnostics JS under Node (see e.g. test_rnd_204_incremental_refresh.py,
test_archive_console_v2.py, and most other `*_frontend.py`/`test_rnd_*.py`
files in this directory).

CI fix: every one of these tests used to run `node -e "<harness>"`, passing
the entire (often 100KB+) harness string as a single argv entry. Linux caps
any SINGLE execve() argument at MAX_ARG_STRLEN (128 KiB, see
include/uapi/linux/binfmts.h) regardless of overall ARG_MAX headroom --
macOS has no equivalent per-argument cap, which is why this only ever
surfaced in Linux CI ("OSError: [Errno 7] Argument list too long") and
never locally. Writing the harness to a temp file and running
`node <path>` instead has no such limit -- only the file PATH is an
argument, not its content.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def run_node(harness: str, *, timeout: float = 30) -> subprocess.CompletedProcess:
    """Execute `harness` (a complete Node script) via a temp file and
    return the same subprocess.CompletedProcess shape `subprocess.run([NODE,
    "-e", harness], ...)` used to -- callers' existing `result.returncode`/
    `result.stdout`/`result.stderr` assertions are unchanged."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as f:
        f.write(harness)
        path = f.name
    try:
        return subprocess.run(
            ["node", path], capture_output=True, text=True, timeout=timeout
        )
    finally:
        Path(path).unlink(missing_ok=True)
