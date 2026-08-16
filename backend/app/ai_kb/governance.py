"""Ingestion allowlist enforcement, sensitive-content gating, and index-plan
diffing for the AI support knowledge base (RND-355 / T1).

Nothing here calls an LLM or writes a search index — this module only
decides *what a downstream indexer (RND-356 / T2) would be allowed to do*,
so its decisions can be unit-tested without a database or a model provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

from app.ai_kb.manifest_schema import AccessLevel, DocStatus, ManifestEntry, compute_content_hash

# ---------------------------------------------------------------------------
# Sensitive-content scanning
# ---------------------------------------------------------------------------
#
# These patterns exist to catch a document that was approved in the manifest
# but whose *content* leaked something that should never reach a customer
# model context (secrets, tokens, connection strings, unmasked log lines).
# Matched line numbers are reported; matched VALUES are never included in
# the finding, so a scan result is itself safe to log or display.

@dataclass(frozen=True)
class SensitivePattern:
    name: str
    regex: re.Pattern


_SENSITIVE_PATTERNS: tuple[SensitivePattern, ...] = (
    SensitivePattern("wecom_corp_secret", re.compile(r"\bcorpsecret\b\s*[:=]\s*\S+", re.IGNORECASE)),
    SensitivePattern("generic_secret_assignment", re.compile(r"\b\w*secret\w*\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", re.IGNORECASE)),
    SensitivePattern("generic_api_key", re.compile(r"\b\w*(api[_-]?key|apikey)\w*\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", re.IGNORECASE)),
    SensitivePattern("access_token", re.compile(r"\baccess_token\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE)),
    SensitivePattern("suite_ticket", re.compile(r"\bsuite_ticket\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", re.IGNORECASE)),
    SensitivePattern("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE)),
    SensitivePattern("password_assignment", re.compile(r"\bpassword\b\s*[:=]\s*['\"]?\S{4,}", re.IGNORECASE)),
    SensitivePattern(
        "connection_string",
        re.compile(r"\b(postgres(?:ql)?|mysql|redis|amqp)://[^\s'\"]+:[^\s'\"@]+@", re.IGNORECASE),
    ),
    SensitivePattern("private_key_block", re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----")),
    SensitivePattern("aws_style_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Unmasked-log heuristic: a line that looks like a structured log entry
    # (level + timestamp-ish token) mixed with what looks like a raw token —
    # deliberately narrow to avoid false-positiving on ordinary prose that
    # merely mentions "error" or "token".
    SensitivePattern(
        "raw_log_line_with_token",
        re.compile(r"\b(ERROR|WARN|DEBUG)\b.{0,80}\btoken\b\s*[:=]\s*[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class SensitiveFinding:
    pattern_name: str
    line_number: int


def scan_for_sensitive_content(text: str) -> list[SensitiveFinding]:
    """Return every sensitive-pattern match by (pattern name, line number).
    Never includes the matched substring — callers must treat a non-empty
    result as "block ingestion", not as something safe to display verbatim."""
    findings: list[SensitiveFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.regex.search(line):
                findings.append(SensitiveFinding(pattern_name=pattern.name, line_number=line_number))
    return findings


# ---------------------------------------------------------------------------
# Ingestion allowlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedEntry:
    entry: ManifestEntry
    reason: str


@dataclass(frozen=True)
class IngestionResolution:
    ingestable: list[ManifestEntry]
    rejected: list[RejectedEntry]


def resolve_ingestable_sources(
    entries: Iterable[ManifestEntry],
    *,
    repo_root: Optional[Path] = None,
) -> IngestionResolution:
    """The single allowlist gate a downstream indexer must call before
    ingesting anything. An entry is ingestable only if:
      1. status == APPROVED (draft/deprecated are never a current answer basis)
      2. access_level in {CUSTOMER, INTERNAL} (FORBIDDEN is always rejected)
      3. its file content has no sensitive-pattern finding

    Rejections are returned, not silently dropped, so governance tests and
    the reindex script can both assert on *why* something didn't make it in.
    """
    root = repo_root or Path(__file__).resolve().parents[3]
    ingestable: list[ManifestEntry] = []
    rejected: list[RejectedEntry] = []

    for entry in entries:
        if entry.access_level == AccessLevel.FORBIDDEN:
            rejected.append(RejectedEntry(entry, "access_level=forbidden"))
            continue
        if entry.status != DocStatus.APPROVED:
            rejected.append(RejectedEntry(entry, f"status={entry.status.value} (only 'approved' is ingestable)"))
            continue

        content = (root / entry.path).read_text(encoding="utf-8")
        findings = scan_for_sensitive_content(content)
        if findings:
            pattern_names = sorted({f.pattern_name for f in findings})
            rejected.append(RejectedEntry(entry, f"sensitive content detected: {pattern_names}"))
            continue

        ingestable.append(entry)

    return IngestionResolution(ingestable=ingestable, rejected=rejected)


# ---------------------------------------------------------------------------
# Locale fallback resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocaleResolution:
    entry: ManifestEntry
    is_fallback: bool


def resolve_locale_fallback(
    entries: Iterable[ManifestEntry], topic_id: str, requested_locale: str
) -> Optional[LocaleResolution]:
    """Pick the entry to serve for (topic_id, requested_locale).

    Missing-translation policy (RND-355 AC "缺失翻译策略"): if an approved
    entry exists for the exact requested locale, use it. Otherwise fall back
    to the topic's source_of_truth entry and mark the result as a fallback,
    so callers (T2/T3) can show a "translated content unavailable, showing
    zh-CN" notice instead of silently serving the wrong language as if it
    were native.
    """
    topic_entries = [e for e in entries if e.topic_id == topic_id and e.status == DocStatus.APPROVED]
    if not topic_entries:
        return None

    for entry in topic_entries:
        if entry.locale == requested_locale:
            return LocaleResolution(entry=entry, is_fallback=False)

    truth_entries = [e for e in topic_entries if e.source_of_truth]
    if not truth_entries:
        return None
    return LocaleResolution(entry=truth_entries[0], is_fallback=True)


# ---------------------------------------------------------------------------
# Index-plan diffing
# ---------------------------------------------------------------------------


class IndexPlanAction(str, Enum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"
    NOOP = "noop"


@dataclass(frozen=True)
class PreviousIndexState:
    source_id: str
    content_hash: str
    access_level: AccessLevel
    status: DocStatus


@dataclass(frozen=True)
class IndexPlanEntry:
    source_id: str
    action: IndexPlanAction
    reason: str


def compute_index_plan(
    previous: Iterable[PreviousIndexState],
    current_entries: Iterable[ManifestEntry],
    *,
    repo_root: Optional[Path] = None,
) -> list[IndexPlanEntry]:
    """Diff the last-indexed state against the current manifest + ingestion
    allowlist, producing the plan a real indexer (T2) would execute. This
    lets governance changes (deletion, rename, access-level downgrade,
    deprecation, content edit) be verified end-to-end — "does it show up as
    the right plan action" — without a real search index in this ticket.

    A source_id is REMOVEd whenever it drops out of the *ingestable* set,
    regardless of why (deleted from manifest, denylisted, deprecated, or
    reclassified as forbidden) — the reason string records which.
    """
    root = repo_root or Path(__file__).resolve().parents[3]
    previous_by_id = {p.source_id: p for p in previous}
    resolution = resolve_ingestable_sources(current_entries, repo_root=root)
    current_ingestable_by_id = {e.source_id: e for e in resolution.ingestable}
    rejected_by_id = {r.entry.source_id: r.reason for r in resolution.rejected}

    plan: list[IndexPlanEntry] = []

    for source_id, entry in current_ingestable_by_id.items():
        prev = previous_by_id.get(source_id)
        if prev is None:
            plan.append(IndexPlanEntry(source_id, IndexPlanAction.ADD, "new ingestable source"))
            continue
        new_hash = compute_content_hash(root / entry.path)
        if new_hash != prev.content_hash:
            plan.append(IndexPlanEntry(source_id, IndexPlanAction.UPDATE, "content changed"))
        elif entry.access_level != prev.access_level:
            plan.append(IndexPlanEntry(source_id, IndexPlanAction.UPDATE, "access_level changed"))
        else:
            plan.append(IndexPlanEntry(source_id, IndexPlanAction.NOOP, "unchanged"))

    for source_id, prev in previous_by_id.items():
        if source_id in current_ingestable_by_id:
            continue
        reason = rejected_by_id.get(source_id, "removed from manifest")
        plan.append(IndexPlanEntry(source_id, IndexPlanAction.REMOVE, reason))

    return sorted(plan, key=lambda p: p.source_id)
