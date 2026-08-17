"""Markdown ingestion + indexing (RND-356 / T2).

Reads only what backend/app/ai_kb (RND-355) says is currently ingestable —
never scans docs/ directly — chunks each approved document by heading, and
writes a new KbIndexVersion + KbDocumentChunk rows. The previous active
version is marked "superseded" (not deleted), so a bad build can always be
rolled back by re-activating it (see roll_back_to).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.ai_kb.governance import PreviousIndexState, compute_index_plan, resolve_ingestable_sources
from app.ai_kb.manifest_schema import ManifestEntry, compute_content_hash, load_manifest
from app.db.models import KbDocumentChunk, KbIndexVersion

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)")


@dataclass(frozen=True)
class ChunkDraft:
    heading_path: str
    content_text: str
    chunk_index: int


def chunk_markdown(text: str, *, title: str) -> list[ChunkDraft]:
    """Split by H1-H3 headings, keeping a '>'-joined heading path per chunk
    so a citation can say "配置说明 > 存储配额" instead of just a doc title.
    Deliberately simple for v1: no sub-splitting of very long sections —
    the current knowledge-base docs are short enough (≈400-1200 words) that
    one chunk per heading section stays well under any reasonable context
    budget. Revisit if/when longer documents are approved."""
    lines = text.splitlines()
    chunks: list[ChunkDraft] = []
    heading_stack: list[str] = []
    current_lines: list[str] = []
    current_heading_path = title
    chunk_index = 0

    def flush() -> None:
        nonlocal chunk_index
        content = "\n".join(current_lines).strip()
        if content:
            chunks.append(ChunkDraft(heading_path=current_heading_path, content_text=content, chunk_index=chunk_index))
            chunk_index += 1

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            flush()
            current_lines = []
            level = len(match.group(1))
            heading_title = match.group(2).strip()
            heading_stack = heading_stack[: level - 1] + [heading_title]
            current_heading_path = " > ".join(heading_stack) if heading_stack else title
        else:
            current_lines.append(line)
    flush()

    return chunks


@dataclass(frozen=True)
class IndexBuildResult:
    index_version_id: int
    chunk_count: int
    ingested_source_ids: list[str]
    rejected: list[tuple[str, str]]  # (source_id, reason)


def _load_previous_states(db: Session, previous_version_id: Optional[int]) -> list[PreviousIndexState]:
    if previous_version_id is None:
        return []
    rows = db.execute(
        sa_text(
            "SELECT DISTINCT ON (source_id) source_id, content_hash, access_level "
            "FROM kb_document_chunks WHERE index_version_id = :vid ORDER BY source_id"
        ),
        {"vid": previous_version_id},
    ).fetchall()
    from app.ai_kb.manifest_schema import AccessLevel, DocStatus

    return [
        PreviousIndexState(
            source_id=row.source_id,
            content_hash=row.content_hash,
            access_level=AccessLevel(row.access_level),
            status=DocStatus.APPROVED,  # only approved docs are ever indexed
        )
        for row in rows
    ]


def get_active_index_version_id(db: Session) -> Optional[int]:
    row = db.execute(
        sa_text("SELECT id FROM kb_index_versions WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    ).fetchone()
    return row.id if row else None


def build_index(
    db: Session,
    *,
    triggered_by: str,
    repo_root: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> IndexBuildResult:
    """Full rebuild: re-chunk every currently-ingestable manifest entry into
    a brand new index version, then flip the version pointer. Incremental
    behavior comes from compute_index_plan feeding the plan_summary (for
    audit/reporting) — the write itself is always a full rebuild for v1,
    which is simple and correct at this corpus size (a handful of docs);
    revisit if/when the corpus grows large enough for rebuild cost to
    matter."""
    root = repo_root or Path(__file__).resolve().parents[4]
    entries = load_manifest(manifest_path, check_files_exist=True, repo_root=root)

    previous_version_id = get_active_index_version_id(db)
    previous_states = _load_previous_states(db, previous_version_id)
    plan = compute_index_plan(previous_states, entries, repo_root=root)
    resolution = resolve_ingestable_sources(entries, repo_root=root)

    plan_summary = {
        "add": sum(1 for p in plan if p.action.value == "add"),
        "update": sum(1 for p in plan if p.action.value == "update"),
        "remove": sum(1 for p in plan if p.action.value == "remove"),
        "noop": sum(1 for p in plan if p.action.value == "noop"),
    }

    new_version = KbIndexVersion(status="active", triggered_by=triggered_by, plan_summary=plan_summary)
    db.add(new_version)
    db.flush()

    chunk_count = 0
    ingested_source_ids: list[str] = []
    for entry in resolution.ingestable:
        chunk_count += _write_chunks(db, entry, root, new_version.id)
        ingested_source_ids.append(entry.source_id)

    if previous_version_id is not None:
        db.execute(
            sa_text("UPDATE kb_index_versions SET status = 'superseded' WHERE id = :vid"),
            {"vid": previous_version_id},
        )

    db.commit()

    return IndexBuildResult(
        index_version_id=new_version.id,
        chunk_count=chunk_count,
        ingested_source_ids=ingested_source_ids,
        rejected=[(r.entry.source_id, r.reason) for r in resolution.rejected],
    )


def _write_chunks(db: Session, entry: ManifestEntry, repo_root: Path, index_version_id: int) -> int:
    file_path = repo_root / entry.path
    content = file_path.read_text(encoding="utf-8")
    content_hash = compute_content_hash(file_path)
    drafts = chunk_markdown(content, title=entry.title)

    for draft in drafts:
        db.add(
            KbDocumentChunk(
                index_version_id=index_version_id,
                source_id=entry.source_id,
                topic_id=entry.topic_id,
                title=entry.title,
                heading_path=draft.heading_path,
                chunk_index=draft.chunk_index,
                content_text=draft.content_text,
                access_level=entry.access_level.value,
                locale=entry.locale,
                doc_version=entry.version,
                doc_path=entry.path,
                content_hash=content_hash,
            )
        )
    return len(drafts)


def roll_back_to(db: Session, target_version_id: int, *, bad_version_id: int) -> None:
    """Reactivate a prior index version and mark the bad one rolled_back.
    Both rows must already exist — this never fabricates chunks, it only
    flips which existing version answer_service/retriever read from."""
    db.execute(
        sa_text("UPDATE kb_index_versions SET status = 'rolled_back' WHERE id = :vid"),
        {"vid": bad_version_id},
    )
    db.execute(
        sa_text("UPDATE kb_index_versions SET status = 'active' WHERE id = :vid"),
        {"vid": target_version_id},
    )
    db.commit()
