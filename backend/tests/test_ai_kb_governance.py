"""RND-355 (T1) — allowlist enforcement, sensitive-content scanning, locale
fallback, and index-plan diffing tests."""

from __future__ import annotations

from pathlib import Path

from app.ai_kb.governance import (
    IndexPlanAction,
    LocaleResolution,
    PreviousIndexState,
    compute_index_plan,
    resolve_ingestable_sources,
    resolve_locale_fallback,
    scan_for_sensitive_content,
)
from app.ai_kb.manifest_schema import AccessLevel, DocStatus, ManifestEntry, TranslationStatus, compute_content_hash


def _entry(**overrides) -> ManifestEntry:
    base = dict(
        source_id="doc-a-zh-cn",
        topic_id="doc-a",
        title="Doc A",
        path="doc-a.md",
        access_level=AccessLevel.CUSTOMER,
        locale="zh-CN",
        translation_status=TranslationStatus.SOURCE,
        audience="tenant_admin",
        version="1.0.0",
        status=DocStatus.APPROVED,
        source_of_truth=True,
        owner="tester",
        last_updated="2026-08-17",
    )
    base.update(overrides)
    return ManifestEntry(**base)


# ---------------------------------------------------------------------------
# Sensitive-content scanning
# ---------------------------------------------------------------------------


def test_scan_detects_generic_secret_assignment() -> None:
    text = "some intro\napi_secret: 'sk_live_abcdef1234567890'\nmore text"
    findings = scan_for_sensitive_content(text)
    assert any(f.pattern_name == "generic_secret_assignment" for f in findings)
    assert findings[0].line_number == 2


def test_scan_detects_connection_string() -> None:
    text = "postgresql://appuser:hunter2pass@db.internal:5432/archive"
    findings = scan_for_sensitive_content(text)
    assert any(f.pattern_name == "connection_string" for f in findings)


def test_scan_detects_private_key_block() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----"
    findings = scan_for_sensitive_content(text)
    assert any(f.pattern_name == "private_key_block" for f in findings)


def test_scan_clean_customer_doc_has_no_findings() -> None:
    text = "# 用户指南\n\n本产品帮助你归档企业微信会话。请在设置页面配置存储配额。"
    assert scan_for_sensitive_content(text) == []


def test_scan_findings_never_include_matched_value() -> None:
    text = "access_token=abcdef0123456789ZZZZ"
    findings = scan_for_sensitive_content(text)
    for f in findings:
        # dataclass repr must never leak the token itself
        assert "abcdef0123456789ZZZZ" not in repr(f)


# ---------------------------------------------------------------------------
# Ingestion allowlist
# ---------------------------------------------------------------------------


def test_forbidden_entry_always_rejected(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("harmless content", encoding="utf-8")
    entry = _entry(access_level=AccessLevel.FORBIDDEN, path="doc.md")

    resolution = resolve_ingestable_sources([entry], repo_root=tmp_path)

    assert resolution.ingestable == []
    assert resolution.rejected[0].reason == "access_level=forbidden"


def test_draft_status_rejected(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("harmless content", encoding="utf-8")
    entry = _entry(status=DocStatus.DRAFT, path="doc.md")

    resolution = resolve_ingestable_sources([entry], repo_root=tmp_path)

    assert resolution.ingestable == []
    assert "draft" in resolution.rejected[0].reason


def test_deprecated_status_rejected(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("harmless content", encoding="utf-8")
    entry = _entry(status=DocStatus.DEPRECATED, path="doc.md")

    resolution = resolve_ingestable_sources([entry], repo_root=tmp_path)

    assert resolution.ingestable == []


def test_approved_customer_doc_with_clean_content_is_ingestable(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("# 用户指南\n\n没有敏感信息。", encoding="utf-8")
    entry = _entry(path="doc.md")

    resolution = resolve_ingestable_sources([entry], repo_root=tmp_path)

    assert resolution.ingestable == [entry]
    assert resolution.rejected == []


def test_sensitive_content_blocks_otherwise_valid_entry(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "# 配置说明\n\ncorpsecret: abcd1234efgh5678\n", encoding="utf-8"
    )
    entry = _entry(path="doc.md")

    resolution = resolve_ingestable_sources([entry], repo_root=tmp_path)

    assert resolution.ingestable == []
    assert "sensitive content" in resolution.rejected[0].reason
    assert "wecom_corp_secret" in resolution.rejected[0].reason


def test_internal_access_level_is_ingestable_but_distinct(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("内部架构说明", encoding="utf-8")
    entry = _entry(access_level=AccessLevel.INTERNAL, path="doc.md")

    resolution = resolve_ingestable_sources([entry], repo_root=tmp_path)

    assert resolution.ingestable == [entry]


# ---------------------------------------------------------------------------
# Locale fallback
# ---------------------------------------------------------------------------


def test_exact_locale_match_is_not_a_fallback() -> None:
    zh = _entry(source_id="a-zh", topic_id="a", locale="zh-CN", source_of_truth=True)
    en = _entry(source_id="a-en", topic_id="a", locale="en", translation_status=TranslationStatus.TRANSLATED, source_of_truth=False)

    result = resolve_locale_fallback([zh, en], topic_id="a", requested_locale="en")

    assert isinstance(result, LocaleResolution)
    assert result.entry.source_id == "a-en"
    assert result.is_fallback is False


def test_missing_locale_falls_back_to_source_of_truth() -> None:
    zh = _entry(source_id="a-zh", topic_id="a", locale="zh-CN", source_of_truth=True)

    result = resolve_locale_fallback([zh], topic_id="a", requested_locale="en")

    assert result.entry.source_id == "a-zh"
    assert result.is_fallback is True


def test_unknown_topic_returns_none() -> None:
    zh = _entry(source_id="a-zh", topic_id="a", locale="zh-CN")

    result = resolve_locale_fallback([zh], topic_id="nonexistent", requested_locale="zh-CN")

    assert result is None


def test_unapproved_entries_excluded_from_locale_resolution() -> None:
    draft = _entry(source_id="a-zh", topic_id="a", locale="zh-CN", status=DocStatus.DRAFT)

    result = resolve_locale_fallback([draft], topic_id="a", requested_locale="zh-CN")

    assert result is None


# ---------------------------------------------------------------------------
# Index-plan diffing
# ---------------------------------------------------------------------------


def test_new_source_produces_add_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    entry = _entry(path="doc.md")

    plan = compute_index_plan(previous=[], current_entries=[entry], repo_root=tmp_path)

    assert len(plan) == 1
    assert plan[0].source_id == entry.source_id
    assert plan[0].action == IndexPlanAction.ADD


def test_unchanged_source_produces_noop_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    entry = _entry(path="doc.md")
    previous = [
        PreviousIndexState(
            source_id=entry.source_id,
            content_hash=compute_content_hash(tmp_path / "doc.md"),
            access_level=entry.access_level,
            status=entry.status,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[entry], repo_root=tmp_path)

    assert len(plan) == 1
    assert plan[0].action == IndexPlanAction.NOOP


def test_content_change_produces_update_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    entry = _entry(path="doc.md")
    previous = [
        PreviousIndexState(
            source_id=entry.source_id,
            content_hash="stale-hash-does-not-match",
            access_level=entry.access_level,
            status=entry.status,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[entry], repo_root=tmp_path)

    assert plan[0].action == IndexPlanAction.UPDATE
    assert "content changed" in plan[0].reason


def test_deletion_from_manifest_produces_remove_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    previous = [
        PreviousIndexState(
            source_id="doc-a-zh-cn",
            content_hash=compute_content_hash(tmp_path / "doc.md"),
            access_level=AccessLevel.CUSTOMER,
            status=DocStatus.APPROVED,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[], repo_root=tmp_path)

    assert len(plan) == 1
    assert plan[0].action == IndexPlanAction.REMOVE
    assert plan[0].source_id == "doc-a-zh-cn"


def test_deprecation_produces_remove_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    entry = _entry(path="doc.md", status=DocStatus.DEPRECATED)
    previous = [
        PreviousIndexState(
            source_id=entry.source_id,
            content_hash=compute_content_hash(tmp_path / "doc.md"),
            access_level=entry.access_level,
            status=DocStatus.APPROVED,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[entry], repo_root=tmp_path)

    assert plan[0].action == IndexPlanAction.REMOVE
    assert "deprecated" in plan[0].reason or "status=" in plan[0].reason


def test_access_level_downgrade_to_forbidden_produces_remove_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    entry = _entry(path="doc.md", access_level=AccessLevel.FORBIDDEN)
    previous = [
        PreviousIndexState(
            source_id=entry.source_id,
            content_hash=compute_content_hash(tmp_path / "doc.md"),
            access_level=AccessLevel.CUSTOMER,
            status=DocStatus.APPROVED,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[entry], repo_root=tmp_path)

    assert plan[0].action == IndexPlanAction.REMOVE
    assert "forbidden" in plan[0].reason


def test_access_level_change_between_ingestable_levels_produces_update_plan(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("content v1", encoding="utf-8")
    entry = _entry(path="doc.md", access_level=AccessLevel.INTERNAL)
    previous = [
        PreviousIndexState(
            source_id=entry.source_id,
            content_hash=compute_content_hash(tmp_path / "doc.md"),
            access_level=AccessLevel.CUSTOMER,
            status=DocStatus.APPROVED,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[entry], repo_root=tmp_path)

    assert plan[0].action == IndexPlanAction.UPDATE
    assert "access_level changed" in plan[0].reason


def test_sensitive_content_regression_produces_remove_plan(tmp_path: Path) -> None:
    """A document that was clean and indexed, then edited to include a
    secret, must be REMOVEd from the index — not silently left in place."""
    (tmp_path / "doc.md").write_text("password: hunter2example\n", encoding="utf-8")
    entry = _entry(path="doc.md")
    previous = [
        PreviousIndexState(
            source_id=entry.source_id,
            content_hash="hash-from-before-the-secret-was-added",
            access_level=entry.access_level,
            status=entry.status,
        )
    ]

    plan = compute_index_plan(previous=previous, current_entries=[entry], repo_root=tmp_path)

    assert plan[0].action == IndexPlanAction.REMOVE
    assert "sensitive content" in plan[0].reason
