"""Retrieval boundary (RND-356 / T2).

v1 backend is Postgres pg_trgm word_similarity ranking over
kb_document_chunks, NOT to_tsvector/to_tsquery matching — verified
empirically that `to_tsvector('simple', ...)` tokenizes a whole run of
CJK characters as a single lexeme (Postgres ships no Chinese word
segmenter), so a multi-character Chinese query phrase can never match a
sub-phrase inside a longer stored chunk via `@@`. This is the exact same
gap migration 0013's own comment describes for archive_messages: 'simple'
FTS doesn't serve this codebase's real (Chinese-heavy) search traffic,
which is why search.py uses ILIKE + pg_trgm instead. word_similarity()
(vs plain similarity()) is used here specifically because it scores the
best-matching word-boundary run inside a long chunk, not the whole
chunk's overlap with a short query — empirically 0.0 for an unrelated
chunk vs 0.2-0.4 for a real match in this codebase's own content.

pg_trgm is already enabled repo-wide (alembic 0013). A future embedding-
based retriever can implement the same Retriever Protocol without
changing answer_service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: int
    source_id: str
    topic_id: str
    title: str
    heading_path: str
    doc_path: str
    doc_version: str
    content_text: str
    access_level: str
    locale: str
    rank: float


class Retriever(Protocol):
    def search(
        self,
        query: str,
        *,
        access_levels: list[str],
        locale: str,
        index_version_id: int,
        limit: int = 5,
    ) -> list[ChunkResult]:
        ...


class PostgresFtsRetriever:
    """Ranked full-text search scoped to a single index version, an
    explicit access-level allowlist, and a single locale. Callers (T4 role
    resolution, answer_service) decide `access_levels` — this class never
    widens it on its own."""

    def __init__(self, db: Session):
        self._db = db

    def search(
        self,
        query: str,
        *,
        access_levels: list[str],
        locale: str,
        index_version_id: int,
        limit: int = 5,
        min_similarity: float = 0.15,
    ) -> list[ChunkResult]:
        if not access_levels:
            return []
        if not query.strip():
            return []

        rows = self._db.execute(
            text(
                """
                SELECT
                    id, source_id, topic_id, title, heading_path, doc_path,
                    doc_version, content_text, access_level, locale,
                    word_similarity(:query, coalesce(content_text, '')) AS rank
                FROM kb_document_chunks
                WHERE index_version_id = :index_version_id
                  AND locale = :locale
                  AND access_level = ANY(:access_levels)
                  AND word_similarity(:query, coalesce(content_text, '')) > :min_similarity
                ORDER BY rank DESC, id ASC
                LIMIT :limit
                """
            ),
            {
                "query": query,
                "index_version_id": index_version_id,
                "locale": locale,
                "access_levels": access_levels,
                "limit": limit,
                "min_similarity": min_similarity,
            },
        ).fetchall()

        return [
            ChunkResult(
                chunk_id=row.id,
                source_id=row.source_id,
                topic_id=row.topic_id,
                title=row.title,
                heading_path=row.heading_path,
                doc_path=row.doc_path,
                doc_version=row.doc_version,
                content_text=row.content_text,
                access_level=row.access_level,
                locale=row.locale,
                rank=float(row.rank),
            )
            for row in rows
        ]


def get_active_index_version_id(db: Session) -> int | None:
    row = db.execute(
        text("SELECT id FROM kb_index_versions WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    ).fetchone()
    return row.id if row else None
