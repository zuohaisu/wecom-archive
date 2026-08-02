"""Static rendering and lifecycle contracts for the RND-338 page script."""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
_JS = (_BACKEND / "app" / "web" / "static" / "diagnostics.js").read_text(encoding="utf-8")
_CSS = (_BACKEND / "app" / "web" / "static" / "diagnostics.css").read_text(encoding="utf-8")


def test_all_six_api_states_have_a_distinct_human_presentation() -> None:
    for state in ("healthy", "attention", "checking", "no_data", "incomplete"):
        assert f"snapshot.state==='{state}'" in _JS
    assert "diagnostics.state.error.title" in _JS
    for key in (
        "diagnostics.state.healthy.copy",
        "diagnostics.state.attention.copy",
        "diagnostics.state.checking.copy",
        "diagnostics.state.noData.copy",
        "diagnostics.state.incomplete.none",
        "diagnostics.state.incomplete.interrupted",
        "diagnostics.error.generic",
    ):
        assert key in _JS


def test_health_is_fail_closed_when_the_api_marks_a_terminal_result_incomplete() -> None:
    assert "(state==='healthy'||state==='attention')&&!complete" in _JS
    assert "state='incomplete'" in _JS


def test_manual_check_is_bounded_and_reuses_an_active_run() -> None:
    assert "method:'POST'" in _JS
    assert "response.status===409" in _JS
    assert "MAX_POLL_ATTEMPTS=12" in _JS
    assert "POLL_DELAYS=[1000,1500,2500,4000,6000]" in _JS
    assert "window.clearTimeout(pollTimer)" in _JS
    assert "window.addEventListener('pagehide'" in _JS
    assert "if(startRequest)return" in _JS


def test_api_data_is_not_used_with_html_or_browser_storage_sinks() -> None:
    for forbidden in ("innerHTML", "console.", "localStorage", "sessionStorage"):
        assert forbidden not in _JS
    assert "document.createElement" in _JS
    assert "textContent=" in _JS


def test_technical_information_is_default_collapsed_and_reason_codes_are_mapped() -> None:
    assert "append(card,'details','diag-details')" in _JS
    assert "unreachable_missing_recipient:'diagnostics.reason.missingRecipient'" in _JS
    assert "unreachable_missing_room:'diagnostics.reason.missingRoom'" in _JS
    assert "unreachable_missing_sender:'diagnostics.reason.missingSender'" in _JS
    assert "unreachable_membership:'diagnostics.reason.membership'" in _JS
    assert "unreachable_other:'diagnostics.reason.other'" in _JS
    assert "diagnostics.reason.unknown" in _JS


def test_narrow_screen_layout_has_no_fixed_width_or_table_first_ui() -> None:
    assert "@media(max-width:480px)" in _CSS
    assert ".diag-health-meta{grid-template-columns:1fr}" in _CSS
    assert ".diag-technical{grid-template-columns:1fr}" in _CSS
    assert "overflow-wrap:anywhere" in _CSS
    assert "table" not in _CSS
