"""
Tests for RND-180 — Message Reachability diagnostics page client-side
rendering (ReachabilityStatusRegistry, renderReport(), renderStatusTable(),
renderTypeTable()).

Scope: the diagnostics page's embedded JS (_DIAGNOSTICS_JS) only. It
consumes the aggregate report object exactly as returned by the real
GET /api/admin/reachability-audit endpoint (RND-178) and must never compute
reachability itself — these tests feed it hand-built report objects (the
same shape build_message_reachability_report() returns) and assert on the
rendered HTML.

These tests execute the real embedded JS under Node (same technique as
test_message_type_registry.py / test_admin_group_participant_overflow.py),
with a minimal DOM stub, so assertions exercise real rendering behavior
rather than only pattern-matching the source text.

Run (from backend/):
    pytest tests/test_reachability_diagnostics_render.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests._rnd216_web_shims import diagnostics_js_source

_DIAGNOSTICS_JS = diagnostics_js_source()

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _DIAGNOSTICS_JS, re.S)
    assert match is not None, f"{label} not found in _DIAGNOSTICS_JS"
    return match.group(0)


# ---------------------------------------------------------------------------
# Static presence checks
# ---------------------------------------------------------------------------


def test_reachability_status_registry_exists_in_page() -> None:
    assert "var ReachabilityStatusRegistry=" in _DIAGNOSTICS_JS


def test_render_report_references_the_registry() -> None:
    src = _extract(r"function renderStatusTable\(countsByStatus,unreachableTotal\)\{.*?\n\}", "renderStatusTable()")
    assert "ReachabilityStatusRegistry" in src


# ---------------------------------------------------------------------------
# Bundle assembly for Node execution
# ---------------------------------------------------------------------------


def _bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        # Pin English so this file's literal-English assertions mean what
        # they say — the app's default locale is zh-CN.
        "I18N.setLocale('en');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"var ReachabilityStatusRegistry=\(function\(\)\{.*?\n\}\)\(\);", "ReachabilityStatusRegistry"),
        _extract(r"function pct\(n,d\)\{.*?\n\}", "pct()"),
        _extract(r"function fmtInt\(n\)\{.*?\n\}", "fmtInt()"),
        _extract(r"function cardHtml\(labelKey,value\)\{.*?\n\}", "cardHtml()"),
        _extract(r"function metaHtml\(labelKey,value\)\{.*?\n\}", "metaHtml()"),
        _extract(r"function renderStatusTable\(countsByStatus,unreachableTotal\)\{.*?\n\}", "renderStatusTable()"),
        _extract(r"function renderTypeTable\(countsByType\)\{.*?\n\}", "renderTypeTable()"),
        _extract(r"function renderReport\(data\)\{.*?\n\}", "renderReport()"),
    ]
    return "\n".join(parts)


def _run_render_report(data: dict) -> str:
    """Execute the real renderReport() under Node against `data` and return
    the resulting innerHTML of #diag-root."""
    assert NODE, "node executable not found"
    bundle = _bundle()
    harness = f"""
{bundle}

var capturedHtml = null;
var rootEl = {{
  get innerHTML() {{ return capturedHtml; }},
  set innerHTML(v) {{ capturedHtml = v; }}
}};
var document = {{
  getElementById: function(id) {{
    if (id === 'diag-root') return rootEl;
    throw new Error('unexpected getElementById(' + id + ')');
  }}
}};

renderReport({json.dumps(data)});
process.stdout.write(capturedHtml);
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def _base_report(**overrides) -> dict:
    report = {
        "scanned_count": 10,
        "matching_total": 10,
        "limit": 500,
        "offset": 0,
        "has_more": False,
        "max_scan_limit": 2000,
        "reachable_count": 7,
        "unreachable_count": 3,
        "counts_by_status": {
            "reachable_direct": 5,
            "reachable_group": 2,
            "unreachable_missing_recipient": 2,
            "unreachable_missing_room": 0,
            "unreachable_missing_sender": 1,
            "unreachable_membership": 0,
            "unreachable_other": 0,
        },
        "counts_by_message_type": {"text": 8, "image": 2},
        "counts_by_conversation_type": {"direct": 6, "group": 4},
    }
    report.update(overrides)
    return report


# ---------------------------------------------------------------------------
# Summary cards
# ---------------------------------------------------------------------------


def test_summary_cards_render_expected_counts() -> None:
    html = _run_render_report(_base_report())
    assert "Successful decrypted messages" in html
    assert "Reachable messages" in html
    assert "Unreachable messages" in html
    assert "Reachability rate" in html
    assert ">10<" in html  # matching_total
    assert ">7<" in html  # reachable_count
    assert ">3<" in html  # unreachable_count


def test_reachability_rate_computed_correctly() -> None:
    html = _run_render_report(_base_report(reachable_count=3, unreachable_count=1))
    assert "75.0%" in html


def test_reachability_rate_zero_denominator_does_not_crash() -> None:
    html = _run_render_report(_base_report(reachable_count=0, unreachable_count=0, scanned_count=0))
    assert "0.0%" in html
    assert "NaN" not in html
    assert "Infinity" not in html


# ---------------------------------------------------------------------------
# Scan metadata / partial-scan (has_more) warning
# ---------------------------------------------------------------------------


def test_has_more_shows_partial_scan_note() -> None:
    html = _run_render_report(_base_report(has_more=True))
    assert "This report is based on the scanned page. More matching messages exist." in html


def test_no_partial_scan_note_when_has_more_false() -> None:
    html = _run_render_report(_base_report(has_more=False))
    assert "More matching messages exist" not in html


def test_scan_metadata_fields_rendered() -> None:
    html = _run_render_report(_base_report(scanned_count=10, matching_total=42, limit=500, offset=0))
    assert "Scanned messages" in html
    assert "Matching total" in html
    assert ">42<" in html


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_state_when_no_matching_messages() -> None:
    html = _run_render_report(_base_report(matching_total=0, scanned_count=0, reachable_count=0, unreachable_count=0))
    assert "No diagnostic data available" in html
    assert "diag-card" not in html


# ---------------------------------------------------------------------------
# Unreachable reason distribution
# ---------------------------------------------------------------------------


def test_unreachable_reason_table_excludes_reachable_statuses() -> None:
    html = _run_render_report(_base_report())
    assert "Reachable (direct)" not in html
    assert "Reachable (group)" not in html
    assert "Unreachable: missing recipient record" in html
    assert "Unreachable: missing sender" in html


def test_unreachable_reason_table_omits_zero_count_statuses() -> None:
    html = _run_render_report(_base_report())
    # unreachable_missing_room and unreachable_membership are 0 in _base_report
    assert "Unreachable: missing room id" not in html
    assert "Unreachable: conversation membership miss" not in html


def test_unreachable_reason_table_percentage_relative_to_unreachable_total() -> None:
    # unreachable_missing_recipient=2 of unreachable_count=3 -> 66.7%
    html = _run_render_report(_base_report())
    assert "66.7%" in html


def test_unreachable_reason_table_handles_unknown_future_status_gracefully() -> None:
    report = _base_report()
    report["counts_by_status"]["unreachable_some_future_reason"] = 4
    report["unreachable_count"] = 7
    html = _run_render_report(report)
    assert "Unknown status" in html
    assert ">4<" in html


# ---------------------------------------------------------------------------
# Message type statistics
# ---------------------------------------------------------------------------


def test_message_type_table_shows_type_and_total() -> None:
    html = _run_render_report(_base_report())
    assert "By message type" in html
    assert ">text<" in html
    assert ">8<" in html
    assert ">image<" in html
    assert ">2<" in html


def test_message_type_table_has_no_reachable_unreachable_breakdown_columns() -> None:
    # Backend only provides total counts by message type — the ticket
    # requires displaying only what's available, not inventing data.
    html = _run_render_report(_base_report())
    type_table_match = re.search(r"By message type</h2>(.*?)</table>", html, re.S)
    assert type_table_match is not None
    type_table_html = type_table_match.group(1)
    assert type_table_html.count("<th>") == 2  # Message type, Total — nothing else


def test_message_type_values_are_escaped() -> None:
    report = _base_report()
    report["counts_by_message_type"] = {"<img src=x onerror=alert(1)>": 1}
    html = _run_render_report(report)
    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img" in html


# ---------------------------------------------------------------------------
# Safety: no message-level / raw identifier fields anywhere in output
# ---------------------------------------------------------------------------


def test_rendered_output_never_contains_sample_or_raw_id_markers() -> None:
    html = _run_render_report(_base_report())
    for banned in ("msgid", "sdkfileid", "message_db_id", "content_text", "local_path", "oss_key"):
        assert banned not in html


# ---------------------------------------------------------------------------
# Browser tab title: set via i18n, not hardcoded (QA fix)
# ---------------------------------------------------------------------------


def _run_apply_static_i18n(locale: str) -> str:
    """Execute the real applyStaticI18n() under Node with `locale` selected
    and return the resulting document.title."""
    assert NODE, "node executable not found"
    i18n_core_src = _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core")
    apply_static_i18n_src = _extract(r"function applyStaticI18n\(\)\{.*?\n\}", "applyStaticI18n()")
    harness = f"""
{i18n_core_src}
I18N.setLocale({json.dumps(locale)});
{apply_static_i18n_src}

var capturedTitle = null;
var document = {{
  documentElement: {{ lang: null }},
  set title(v) {{ capturedTitle = v; }},
  get title() {{ return capturedTitle; }},
  querySelectorAll: function() {{ return []; }}
}};

applyStaticI18n();
process.stdout.write(capturedTitle);
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def test_diagnostics_page_document_title_localized_english() -> None:
    assert _run_apply_static_i18n("en") == "Message Reachability Diagnostics"


def test_diagnostics_page_document_title_localized_zh_cn() -> None:
    assert _run_apply_static_i18n("zh-CN") == "消息可达性诊断"


def test_diagnostics_page_document_title_localized_zh_tw() -> None:
    assert _run_apply_static_i18n("zh-TW") == "訊息可達性診斷"
