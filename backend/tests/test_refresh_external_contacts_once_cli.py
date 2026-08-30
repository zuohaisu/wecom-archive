"""Bootstrap/entrypoint contract for scripts/refresh_external_contacts_once.py
(GH-104 Follow-up A).

Production installed, enabled, and triggered `wecom-external-contact-
refresh.{service,path,timer}` correctly (GH-104 Phase B/C), but every one of
36 post-deploy executions still failed with
`ModuleNotFoundError: No module named 'app'` before this fix: the script
imported `app.services...` without the `sys.path.insert(...)` bootstrap
every other `backend/scripts/*.py` entrypoint uses when invoked the way
systemd invokes it — `python scripts/<name>.py` from
`WorkingDirectory=.../backend`, not `python -m ...` and not relying on an
ambient `PYTHONPATH`.

These tests spawn the REAL script as a subprocess, exactly the way
`wecom-external-contact-refresh.service`'s `ExecStart=` does (same relative
path, same cwd, `PYTHONPATH` explicitly stripped so no accidental host/CI
environment variable can mask a regression), and assert on its documented
[INFO]/[FAIL] contract. This is deliberately NOT a grep for
"sys.path.insert" in the source — a textual grep would not have caught the
production bug (the whole point is proving the import actually succeeds
under the real invocation shape), and stays correct even if a future change
migrates every script to a different bootstrap mechanism.

Never touches a real database or the WeCom API: DATABASE_URL is either
unset or a syntactically-valid-but-never-connected-to placeholder, and
WECOM_EXTERNAL_CONTACT_SECRET is left unset so the script's own early-exit
paths return before any network or database I/O.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = "scripts/refresh_external_contacts_once.py"


def _run(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("DATABASE_URL", None)
    env.pop("WECOM_EXTERNAL_CONTACT_SECRET", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, _SCRIPT],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_entrypoint_imports_app_without_database_url_configured() -> None:
    """Production invokes this script directly, without relying on
    PYTHONPATH -- reproduces the exact shape of the 36 failed post-deploy
    executions (DATABASE_URL absent from a broken/unset EnvironmentFile is
    not the case here; this specifically isolates the import step from any
    database concern)."""
    result = _run({})

    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert result.stdout.strip() == "[FAIL] external_contact_refresh configuration_missing"
    assert result.returncode == 1


def test_entrypoint_imports_app_and_reaches_the_secret_check() -> None:
    """A second, deeper checkpoint on the same import chain
    (app.services.external_contact_refresh_worker and
    app.services.tenant_credentials both have to import cleanly): with
    DATABASE_URL present but WECOM_EXTERNAL_CONTACT_SECRET absent, the
    script must reach its documented skip path -- never touching the
    database, since engine creation happens only after this check."""
    result = _run({"DATABASE_URL": "postgresql://unused:unused@localhost/unused"})

    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert (
        result.stdout.strip()
        == "[INFO] external_contact_refresh status=skipped reason=not_configured"
    )
    assert result.returncode == 0
