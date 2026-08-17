"""RND-356 (T2) — Markdown heading-based chunking (no DB required)."""

from __future__ import annotations

from app.services.ai.ingestion import chunk_markdown


def test_single_section_uses_title_as_heading_path() -> None:
    text = "intro line one\nintro line two"
    chunks = chunk_markdown(text, title="用户指南")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "用户指南"
    assert "intro line one" in chunks[0].content_text


def test_splits_on_headings_and_builds_path() -> None:
    text = "# 配置说明\n\n顶层介绍\n\n## 存储配额\n\n配额相关内容\n\n### 修改配额\n\n修改步骤"
    chunks = chunk_markdown(text, title="配置说明")

    paths = [c.heading_path for c in chunks]
    assert "配置说明" in paths
    assert "配置说明 > 存储配额" in paths
    assert "配置说明 > 存储配额 > 修改配额" in paths


def test_chunk_index_increments_in_order() -> None:
    text = "# A\n\ncontent a\n\n# B\n\ncontent b\n\n# C\n\ncontent c"
    chunks = chunk_markdown(text, title="doc")
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


def test_heading_stack_pops_back_to_shallower_level() -> None:
    text = "# A\n\na\n\n## A1\n\na1\n\n# B\n\nb"
    chunks = chunk_markdown(text, title="doc")
    paths = [c.heading_path for c in chunks]
    assert paths == ["A", "A > A1", "B"]


def test_empty_sections_are_dropped() -> None:
    text = "# A\n\n\n\n# B\n\nreal content"
    chunks = chunk_markdown(text, title="doc")
    assert len(chunks) == 1
    assert chunks[0].heading_path == "B"


def test_h4_and_deeper_are_not_treated_as_headings() -> None:
    text = "# A\n\n#### not a heading, still content\n\nmore"
    chunks = chunk_markdown(text, title="doc")
    assert len(chunks) == 1
    assert "#### not a heading" in chunks[0].content_text


def test_real_customer_docs_chunk_without_error() -> None:
    from pathlib import Path

    from app.ai_kb.manifest_schema import load_manifest

    repo_root = Path(__file__).resolve().parents[2]
    entries = load_manifest()
    customer_entries = [e for e in entries if e.access_level.value == "customer"]
    assert customer_entries

    for entry in customer_entries:
        text = (repo_root / entry.path).read_text(encoding="utf-8")
        chunks = chunk_markdown(text, title=entry.title)
        assert len(chunks) >= 1, f"{entry.source_id} produced no chunks"
