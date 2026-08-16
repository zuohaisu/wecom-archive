"""RND-356 (T2) — scripts/run_ai_kb_reindex.py entrypoint."""

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
import run_ai_kb_reindex  # noqa: E402


def test_main_fails_closed_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run_ai_kb_reindex.main() == 1


def test_main_skips_without_writing_when_ai_support_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_SUPPORT_ENABLED", raising=False)

    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as db:
        before = db.execute(text("SELECT count(*) FROM kb_index_versions")).scalar()

    assert run_ai_kb_reindex.main() == 0

    with Session(engine) as db:
        after = db.execute(text("SELECT count(*) FROM kb_index_versions")).scalar()
    assert after == before


def test_main_rebuilds_the_real_manifest_into_a_new_active_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_SUPPORT_ENABLED", "true")
    exit_code = run_ai_kb_reindex.main()
    assert exit_code == 0

    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as db:
        active_id = db.execute(
            text("SELECT id FROM kb_index_versions WHERE status = 'active' ORDER BY id DESC LIMIT 1")
        ).scalar()
        assert active_id is not None
        source_ids = {
            row.source_id
            for row in db.execute(
                text("SELECT DISTINCT source_id FROM kb_document_chunks WHERE index_version_id = :v"),
                {"v": active_id},
            ).fetchall()
        }
        assert "user-guide-zh-cn" in source_ids
        assert "worker-runbook-forbidden" not in source_ids
