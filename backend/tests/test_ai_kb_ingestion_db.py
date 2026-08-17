"""RND-356 (T2) — build_index / roll_back_to against real Postgres.

Gated on DATABASE_URL per docs/agent-test-database.md: JSONB and real
GIN/tsvector behavior don't exist on SQLite, so these must run against a
real Postgres schema (migrated via `alembic upgrade head`), not a faked one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.ai.ingestion import build_index, get_active_index_version_id, roll_back_to

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        yield session


def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(entries), encoding="utf-8")
    return manifest_path


def _entry(**overrides) -> dict:
    base = {
        "source_id": "doc-a-zh-cn",
        "topic_id": "doc-a",
        "title": "Doc A",
        "path": "doc-a.md",
        "access_level": "customer",
        "locale": "zh-CN",
        "translation_status": "source",
        "audience": "tenant_admin",
        "version": "1.0.0",
        "status": "approved",
        "source_of_truth": True,
        "owner": "tester",
        "last_updated": "2026-08-17",
    }
    base.update(overrides)
    return base


def test_build_index_creates_version_and_chunks(tmp_path: Path, db: Session) -> None:
    (tmp_path / "doc-a.md").write_text(
        "# Doc A\n\nroot content\n\n## Section One\n\ncontent one", encoding="utf-8"
    )
    manifest_path = _write_manifest(tmp_path, [_entry()])

    result = build_index(db, triggered_by="test", repo_root=tmp_path, manifest_path=manifest_path)

    assert result.chunk_count == 2  # "Doc A" root chunk + "Doc A > Section One"
    assert result.ingested_source_ids == ["doc-a-zh-cn"]
    assert get_active_index_version_id(db) == result.index_version_id

    rows = db.execute(
        text("SELECT source_id, heading_path FROM kb_document_chunks WHERE index_version_id = :v"),
        {"v": result.index_version_id},
    ).fetchall()
    headings = {row.heading_path for row in rows}
    assert "Doc A > Section One" in headings


def test_build_index_excludes_forbidden_and_draft(tmp_path: Path, db: Session) -> None:
    (tmp_path / "doc-a.md").write_text("# Doc A\n\ncontent", encoding="utf-8")
    (tmp_path / "doc-b.md").write_text("# Doc B\n\ncontent", encoding="utf-8")
    (tmp_path / "doc-c.md").write_text("# Doc C\n\ncontent", encoding="utf-8")
    entries = [
        _entry(source_id="a", topic_id="a", path="doc-a.md", access_level="forbidden"),
        _entry(source_id="b", topic_id="b", path="doc-b.md", status="draft"),
        _entry(source_id="c", topic_id="c", path="doc-c.md"),
    ]
    manifest_path = _write_manifest(tmp_path, entries)

    result = build_index(db, triggered_by="test", repo_root=tmp_path, manifest_path=manifest_path)

    assert result.ingested_source_ids == ["c"]
    rejected_ids = {source_id for source_id, _reason in result.rejected}
    assert rejected_ids == {"a", "b"}


def test_build_index_supersedes_previous_version(tmp_path: Path, db: Session) -> None:
    (tmp_path / "doc-a.md").write_text("# Doc A\n\ncontent v1", encoding="utf-8")
    manifest_path = _write_manifest(tmp_path, [_entry()])

    first = build_index(db, triggered_by="test-1", repo_root=tmp_path, manifest_path=manifest_path)

    (tmp_path / "doc-a.md").write_text("# Doc A\n\ncontent v2", encoding="utf-8")
    second = build_index(db, triggered_by="test-2", repo_root=tmp_path, manifest_path=manifest_path)

    first_status = db.execute(
        text("SELECT status FROM kb_index_versions WHERE id = :v"), {"v": first.index_version_id}
    ).scalar()
    second_status = db.execute(
        text("SELECT status FROM kb_index_versions WHERE id = :v"), {"v": second.index_version_id}
    ).scalar()

    assert first_status == "superseded"
    assert second_status == "active"
    assert get_active_index_version_id(db) == second.index_version_id


def test_roll_back_to_reactivates_previous_version(tmp_path: Path, db: Session) -> None:
    (tmp_path / "doc-a.md").write_text("# Doc A\n\ngood content", encoding="utf-8")
    manifest_path = _write_manifest(tmp_path, [_entry()])
    good = build_index(db, triggered_by="test-good", repo_root=tmp_path, manifest_path=manifest_path)

    (tmp_path / "doc-a.md").write_text("# Doc A\n\nbad content", encoding="utf-8")
    bad = build_index(db, triggered_by="test-bad", repo_root=tmp_path, manifest_path=manifest_path)
    assert get_active_index_version_id(db) == bad.index_version_id

    roll_back_to(db, target_version_id=good.index_version_id, bad_version_id=bad.index_version_id)

    assert get_active_index_version_id(db) == good.index_version_id
    bad_status = db.execute(
        text("SELECT status FROM kb_index_versions WHERE id = :v"), {"v": bad.index_version_id}
    ).scalar()
    assert bad_status == "rolled_back"


def test_build_index_records_content_hash_per_chunk(tmp_path: Path, db: Session) -> None:
    (tmp_path / "doc-a.md").write_text("# Doc A\n\nsome content", encoding="utf-8")
    manifest_path = _write_manifest(tmp_path, [_entry()])

    result = build_index(db, triggered_by="test", repo_root=tmp_path, manifest_path=manifest_path)

    hashes = db.execute(
        text("SELECT DISTINCT content_hash FROM kb_document_chunks WHERE index_version_id = :v"),
        {"v": result.index_version_id},
    ).fetchall()
    assert len(hashes) == 1
    assert len(hashes[0].content_hash) == 64  # sha256 hex digest length
