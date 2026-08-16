"""RND-356 (T2) — PostgresFtsRetriever access-level isolation and ranking.

Gated on DATABASE_URL: exercises the real to_tsvector/GIN full-text
behavior, which SQLite cannot provide (see docs/agent-test-database.md).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.ai.retriever import PostgresFtsRetriever, get_active_index_version_id

_DB_AVAILABLE = bool(os.environ.get("DATABASE_URL", "").strip())
pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DATABASE_URL not set")


@pytest.fixture()
def db():
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        yield session


def _make_version(db: Session, *, status: str = "active") -> int:
    row = db.execute(
        text(
            "INSERT INTO kb_index_versions (status, triggered_by) "
            "VALUES (:status, 'test') RETURNING id"
        ),
        {"status": status},
    ).fetchone()
    db.commit()
    return row.id


def _insert_chunk(
    db: Session,
    *,
    index_version_id: int,
    source_id: str,
    content_text: str,
    access_level: str = "customer",
    locale: str = "zh-CN",
    title: str = "Doc",
    heading_path: str = "Doc",
) -> None:
    db.execute(
        text(
            """
            INSERT INTO kb_document_chunks
                (index_version_id, source_id, topic_id, title, heading_path,
                 chunk_index, content_text, access_level, locale, doc_version,
                 doc_path, content_hash)
            VALUES
                (:index_version_id, :source_id, :source_id, :title, :heading_path,
                 0, :content_text, :access_level, :locale, '1.0.0',
                 'doc.md', 'deadbeef')
            """
        ),
        {
            "index_version_id": index_version_id,
            "source_id": source_id,
            "title": title,
            "heading_path": heading_path,
            "content_text": content_text,
            "access_level": access_level,
            "locale": locale,
        },
    )
    db.commit()


def test_customer_request_cannot_retrieve_internal_chunk(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="public-doc", content_text="存储配额如何设置", access_level="customer")
    _insert_chunk(db, index_version_id=version_id, source_id="internal-doc", content_text="存储配额内部运维细节", access_level="internal")

    retriever = PostgresFtsRetriever(db)
    results = retriever.search(
        "存储配额", access_levels=["customer"], locale="zh-CN", index_version_id=version_id, limit=5
    )

    source_ids = {r.source_id for r in results}
    assert "public-doc" in source_ids
    assert "internal-doc" not in source_ids


def test_internal_role_can_retrieve_both_levels(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="public-doc", content_text="存储配额如何设置", access_level="customer")
    _insert_chunk(db, index_version_id=version_id, source_id="internal-doc", content_text="存储配额内部运维细节", access_level="internal")

    retriever = PostgresFtsRetriever(db)
    results = retriever.search(
        "存储配额", access_levels=["customer", "internal"], locale="zh-CN", index_version_id=version_id, limit=5
    )

    source_ids = {r.source_id for r in results}
    assert source_ids == {"public-doc", "internal-doc"}


def test_locale_filter_excludes_other_locales(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="zh-doc", content_text="存储配额说明", locale="zh-CN")
    _insert_chunk(db, index_version_id=version_id, source_id="en-doc", content_text="storage quota explanation", locale="en")

    retriever = PostgresFtsRetriever(db)
    results = retriever.search(
        "存储配额", access_levels=["customer"], locale="zh-CN", index_version_id=version_id, limit=5
    )

    assert {r.source_id for r in results} == {"zh-doc"}


def test_only_matches_within_requested_index_version(db: Session) -> None:
    old_version = _make_version(db, status="superseded")
    new_version = _make_version(db)
    _insert_chunk(db, index_version_id=old_version, source_id="old-doc", content_text="存储配额旧版本说明")
    _insert_chunk(db, index_version_id=new_version, source_id="new-doc", content_text="存储配额新版本说明")

    retriever = PostgresFtsRetriever(db)
    results = retriever.search(
        "存储配额", access_levels=["customer"], locale="zh-CN", index_version_id=new_version, limit=5
    )

    assert {r.source_id for r in results} == {"new-doc"}


def test_no_match_returns_empty_list(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="doc", content_text="存储配额说明")

    retriever = PostgresFtsRetriever(db)
    results = retriever.search(
        "完全不相关的问题about widgets", access_levels=["customer"], locale="zh-CN", index_version_id=version_id
    )

    assert results == []


def test_empty_access_levels_returns_empty_without_query(db: Session) -> None:
    version_id = _make_version(db)
    _insert_chunk(db, index_version_id=version_id, source_id="doc", content_text="存储配额说明")

    retriever = PostgresFtsRetriever(db)
    results = retriever.search("存储配额", access_levels=[], locale="zh-CN", index_version_id=version_id)

    assert results == []


def test_get_active_index_version_id_returns_latest_active(db: Session) -> None:
    _make_version(db, status="superseded")
    active_version = _make_version(db, status="active")

    assert get_active_index_version_id(db) == active_version
