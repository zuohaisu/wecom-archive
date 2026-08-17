"""RND-359 (T5) — scripts/run_ai_kb_eval.py entrypoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_ai_kb_eval  # noqa: E402


def test_main_fails_closed_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run_ai_kb_eval.main() == 1


def test_main_runs_and_records_eval_run() -> None:
    exit_code = run_ai_kb_eval.main()
    assert exit_code == 0

    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as db:
        row = db.execute(
            text("SELECT dataset_version, passed, metrics FROM ai_eval_runs ORDER BY id DESC LIMIT 1")
        ).fetchone()
        assert row is not None
        assert row.passed is True
        assert row.metrics["over_permission_count"] == 0
