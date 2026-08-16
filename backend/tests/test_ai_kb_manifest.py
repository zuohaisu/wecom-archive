"""RND-355 (T1) — manifest schema loader tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai_kb.manifest_schema import (
    AccessLevel,
    DocStatus,
    ManifestValidationError,
    TranslationStatus,
    load_manifest,
)

_VALID_ENTRY = {
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


def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(entries), encoding="utf-8")
    return manifest_path


def test_real_manifest_loads_and_validates() -> None:
    """The manifest actually shipped in app/ai_kb/manifest.json must be
    loadable with its referenced files present on disk — this is the
    regression guard for the real first-batch knowledge source list."""
    entries = load_manifest()
    assert len(entries) >= 6
    customer_entries = [e for e in entries if e.access_level == AccessLevel.CUSTOMER]
    assert len(customer_entries) >= 6
    assert any(e.access_level == AccessLevel.INTERNAL for e in entries)
    assert any(e.access_level == AccessLevel.FORBIDDEN for e in entries)


def test_valid_minimal_manifest_loads(tmp_path: Path) -> None:
    (tmp_path / "doc-a.md").write_text("# Doc A\n\ncontent", encoding="utf-8")
    manifest_path = _write_manifest(tmp_path, [_VALID_ENTRY])

    entries = load_manifest(manifest_path, check_files_exist=False)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.source_id == "doc-a-zh-cn"
    assert entry.access_level == AccessLevel.CUSTOMER
    assert entry.status == DocStatus.APPROVED
    assert entry.translation_status == TranslationStatus.SOURCE
    assert entry.source_of_truth is True


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    bad_entry = dict(_VALID_ENTRY)
    del bad_entry["owner"]
    manifest_path = _write_manifest(tmp_path, [bad_entry])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("owner" in issue for issue in exc_info.value.issues)


def test_invalid_access_level_rejected(tmp_path: Path) -> None:
    bad_entry = dict(_VALID_ENTRY)
    bad_entry["access_level"] = "public"  # not a real level
    manifest_path = _write_manifest(tmp_path, [bad_entry])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("access_level" in issue for issue in exc_info.value.issues)


def test_duplicate_source_id_rejected(tmp_path: Path) -> None:
    entry_two = dict(_VALID_ENTRY)
    manifest_path = _write_manifest(tmp_path, [_VALID_ENTRY, entry_two])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("duplicate source_id" in issue for issue in exc_info.value.issues)


def test_path_traversal_rejected(tmp_path: Path) -> None:
    bad_entry = dict(_VALID_ENTRY)
    bad_entry["path"] = "../../etc/passwd"
    manifest_path = _write_manifest(tmp_path, [bad_entry])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("repo-relative" in issue for issue in exc_info.value.issues)


def test_missing_source_of_truth_rejected(tmp_path: Path) -> None:
    bad_entry = dict(_VALID_ENTRY)
    bad_entry["source_of_truth"] = False
    manifest_path = _write_manifest(tmp_path, [bad_entry])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("no source_of_truth entry" in issue for issue in exc_info.value.issues)


def test_multiple_source_of_truth_in_same_topic_rejected(tmp_path: Path) -> None:
    entry_two = dict(_VALID_ENTRY)
    entry_two["source_id"] = "doc-a-en"
    entry_two["locale"] = "en"
    manifest_path = _write_manifest(tmp_path, [_VALID_ENTRY, entry_two])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("multiple source_of_truth entries" in issue for issue in exc_info.value.issues)


def test_missing_file_rejected_when_check_files_exist(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, [_VALID_ENTRY])  # doc-a.md never written

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=True)

    assert any("does not exist" in issue for issue in exc_info.value.issues)


def test_all_issues_reported_not_just_first(tmp_path: Path) -> None:
    bad_entry_one = dict(_VALID_ENTRY)
    bad_entry_one["access_level"] = "public"
    bad_entry_two = dict(_VALID_ENTRY)
    bad_entry_two["status"] = "archived"
    manifest_path = _write_manifest(tmp_path, [bad_entry_one, bad_entry_two])

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert len(exc_info.value.issues) >= 2


def test_manifest_root_must_be_array(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")

    with pytest.raises(ManifestValidationError) as exc_info:
        load_manifest(manifest_path, check_files_exist=False)

    assert any("array" in issue for issue in exc_info.value.issues)


def test_manifest_not_found() -> None:
    with pytest.raises(ManifestValidationError):
        load_manifest(Path("/nonexistent/manifest.json"), check_files_exist=False)
