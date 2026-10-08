"""Manifest schema and loader for the AI support knowledge base (RND-355 / T1).

The manifest (`app/ai_kb/manifest.json`) is the *only* allowlist of documents
the RAG pipeline (RND-356 / T2) is permitted to ingest. Nothing is ever
ingested by scanning `docs/` or any other directory directly — a path only
becomes eligible by having an approved, valid entry here. See
`docs/ai/kb-governance.md` for the human-readable policy this schema
encodes.

Field semantics:
  source_id          Stable, human-assigned identifier. Never reused for a
                      different document even if the document is retired —
                      that would let an old citation silently point at new
                      content. Referenced by `topic_id` groups (see
                      `resolve_locale_fallback` in governance.py).
  topic_id            Groups the same logical document across locales (e.g.
                      the zh-CN source and a future en translation share one
                      topic_id but have distinct source_id / locale).
  path                Repo-relative path to the Markdown source file.
  access_level        "customer" | "internal" | "forbidden". "forbidden" is
                      a denylist marker: an entry may exist here purely to
                      document *why* a path must never be ingested, and the
                      loader still rejects it at ingestion-allowlist time.
  status               "draft" | "approved" | "deprecated". Only "approved"
                      entries are ever ingestable, regardless of access_level.
  source_of_truth     True for the canonical-locale entry within a topic_id
                      group; used as the fallback target when a requested
                      locale has no translated entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

_REQUIRED_FIELDS = (
    "source_id",
    "topic_id",
    "title",
    "path",
    "access_level",
    "locale",
    "translation_status",
    "audience",
    "version",
    "status",
    "source_of_truth",
    "owner",
    "last_updated",
)


class AccessLevel(str, Enum):
    PUBLIC = "public"  # approved for anonymous pre-sales visitors (RND-408)
    CUSTOMER = "customer"
    INTERNAL = "internal"
    FORBIDDEN = "forbidden"


class DocStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class TranslationStatus(str, Enum):
    SOURCE = "source"  # canonical-locale content, not a translation
    TRANSLATED = "translated"
    PENDING_FALLBACK = "pending_fallback"  # no translation yet; callers fall back


@dataclass(frozen=True)
class ManifestEntry:
    source_id: str
    topic_id: str
    title: str
    path: str
    access_level: AccessLevel
    locale: str
    translation_status: TranslationStatus
    audience: str
    version: str
    status: DocStatus
    source_of_truth: bool
    owner: str
    last_updated: str


class ManifestValidationError(Exception):
    """Raised with every validation issue found, not just the first."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


def _validate_raw_entry(raw: dict, index: int) -> list[str]:
    issues: list[str] = []
    prefix = f"manifest[{index}]"

    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        issues.append(f"{prefix}: missing required field(s) {missing}")
        return issues  # further checks would KeyError

    if not isinstance(raw["source_id"], str) or not raw["source_id"].strip():
        issues.append(f"{prefix}: source_id must be a non-empty string")

    try:
        AccessLevel(raw["access_level"])
    except ValueError:
        issues.append(f"{prefix} ({raw.get('source_id')}): invalid access_level {raw['access_level']!r}")

    try:
        DocStatus(raw["status"])
    except ValueError:
        issues.append(f"{prefix} ({raw.get('source_id')}): invalid status {raw['status']!r}")

    try:
        TranslationStatus(raw["translation_status"])
    except ValueError:
        issues.append(
            f"{prefix} ({raw.get('source_id')}): invalid translation_status {raw['translation_status']!r}"
        )

    if not isinstance(raw["source_of_truth"], bool):
        issues.append(f"{prefix} ({raw.get('source_id')}): source_of_truth must be a bool")

    if not isinstance(raw["path"], str) or not raw["path"].strip():
        issues.append(f"{prefix} ({raw.get('source_id')}): path must be a non-empty string")
    elif raw["path"].startswith("/") or ".." in raw["path"].split("/"):
        # Manifest paths are repo-relative by contract; an absolute path or a
        # `..` segment is exactly how an entry could point outside the
        # approved docs tree without the allowlist noticing.
        issues.append(f"{prefix} ({raw.get('source_id')}): path must be repo-relative with no '..' segments")

    return issues


def _repo_root() -> Path:
    # backend/app/ai_kb/manifest_schema.py -> repo root is 3 parents up.
    return Path(__file__).resolve().parents[3]


def load_manifest(
    path: Optional[Path] = None,
    *,
    check_files_exist: bool = True,
    repo_root: Optional[Path] = None,
) -> list[ManifestEntry]:
    """Load and validate the manifest. Raises ManifestValidationError with
    every problem found (not just the first) so a human can fix them all in
    one pass instead of playing whack-a-mole.

    repo_root anchors the manifest's repo-relative `path` fields for the
    file-existence check; it defaults to this repo's real root but callers
    testing against an isolated tmp_path manifest must override it —
    otherwise check_files_exist would validate paths against the wrong
    tree entirely."""
    manifest_path = path or _MANIFEST_PATH
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManifestValidationError([f"manifest file not found: {manifest_path}"]) from exc

    try:
        raw_entries = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError([f"manifest is not valid JSON: {exc}"]) from exc

    if not isinstance(raw_entries, list):
        raise ManifestValidationError(["manifest root must be a JSON array of entries"])

    issues: list[str] = []
    for index, raw in enumerate(raw_entries):
        issues.extend(_validate_raw_entry(raw, index))

    if issues:
        raise ManifestValidationError(issues)

    entries: list[ManifestEntry] = []
    seen_ids: dict[str, int] = {}
    root = repo_root if repo_root is not None else _repo_root()
    for index, raw in enumerate(raw_entries):
        source_id = raw["source_id"]
        if source_id in seen_ids:
            issues.append(f"manifest[{index}]: duplicate source_id {source_id!r} (first at index {seen_ids[source_id]})")
            continue
        seen_ids[source_id] = index

        if check_files_exist and not (root / raw["path"]).is_file():
            # Forbidden entries are denylist metadata, never read or indexed.
            # Their source may intentionally be absent from a public snapshot.
            if raw["access_level"] != AccessLevel.FORBIDDEN.value:
                issues.append(f"manifest[{index}] ({source_id}): path does not exist: {raw['path']}")
                continue

        entries.append(
            ManifestEntry(
                source_id=source_id,
                topic_id=raw["topic_id"],
                title=raw["title"],
                path=raw["path"],
                access_level=AccessLevel(raw["access_level"]),
                locale=raw["locale"],
                translation_status=TranslationStatus(raw["translation_status"]),
                audience=raw["audience"],
                version=raw["version"],
                status=DocStatus(raw["status"]),
                source_of_truth=raw["source_of_truth"],
                owner=raw["owner"],
                last_updated=raw["last_updated"],
            )
        )

    # A source_of_truth entry must exist (and be unique) per topic_id so
    # locale-fallback resolution always has exactly one canonical target.
    by_topic: dict[str, list[ManifestEntry]] = {}
    for entry in entries:
        by_topic.setdefault(entry.topic_id, []).append(entry)
    for topic_id, group in by_topic.items():
        truth_entries = [e for e in group if e.source_of_truth]
        if len(truth_entries) == 0:
            issues.append(f"topic_id {topic_id!r}: no source_of_truth entry")
        elif len(truth_entries) > 1:
            ids = [e.source_id for e in truth_entries]
            issues.append(f"topic_id {topic_id!r}: multiple source_of_truth entries {ids}")

    if issues:
        raise ManifestValidationError(issues)

    return sorted(entries, key=lambda e: e.source_id)


def compute_content_hash(path: Path) -> str:
    """SHA-256 of a source file's bytes, used by governance.compute_index_plan
    to detect content changes without re-embedding/re-indexing unchanged docs."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
