"""CLI contract for the RND-337 one-shot runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_reachability_check_once.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rnd337_runner", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_accepts_only_one_public_run_id(monkeypatch):
    script = _load_script()
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "public-id-only"])
    assert script._parse_args().public_run_id == "public-id-only"
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "public-id", "tenant-must-not-fit"])
    with pytest.raises(SystemExit):
        script._parse_args()


def test_cli_unknown_or_terminal_run_fails_closed_without_leaking(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:supersecretpassword@localhost/test_only")
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "public-id-only"])
    monkeypatch.setattr(script, "create_engine", lambda _url: object())

    class FakeSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(script, "Session", FakeSession)
    monkeypatch.setattr(script, "execute_run", lambda _db, _public_id: "not_active")
    assert script.main() == 1
    output = capsys.readouterr().out
    assert "supersecretpassword" not in output
    assert "tenant" not in output.lower()
