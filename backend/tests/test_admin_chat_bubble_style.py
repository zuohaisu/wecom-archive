"""
Tests for RND-154 — optimize admin timeline chat bubble style to match
WeCom's real measured colors.

History:
  - Originally `.tl-bubble-self` (staff/self/outgoing messages) used a green
    background (#d9f7be), which didn't match WeCom's outgoing-bubble style.
  - A first pass switched it to a saturated blue (#2563eb) with white text,
    but that was darker than the real WeCom client. This revision matches
    WeCom's actual measured colors: a light blue fill (#CFE6FD) with black
    text, plus a distinct hyperlink color (#417CE8) and a WeCom-like
    ~11px bubble radius.

`.tl-bubble-other` (incoming) stays white with black text.  Message
direction classification (isSelf/isStaff -> row/bubble class selection)
and row alignment are unchanged.

These tests inspect the embedded CSS/JS source directly (same technique
used in test_admin_group_participant_overflow.py), so they don't need a
browser.

Run (from backend/):
    pytest tests/test_admin_chat_bubble_style.py -v
"""

from __future__ import annotations

import re

from app.main import _REVIEW_CONSOLE_HTML


def _extract_style_block() -> str:
    match = re.search(r"<style>(.*?)</style>", _REVIEW_CONSOLE_HTML, re.S)
    assert match is not None, "expected an embedded <style> block"
    return match.group(1)


def _extract_rule(css: str, selector: str) -> str:
    # Anchor to start-of-line so a bare selector (e.g. ".tl-bubble")
    # doesn't accidentally match inside a longer descendant/related
    # selector (e.g. ".tl-bubble-self").
    pattern = r"(?:^|\n)" + re.escape(selector) + r"\{([^}]*)\}"
    match = re.search(pattern, css)
    assert match is not None, f"expected a CSS rule for {selector!r}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Outgoing (self) bubble: WeCom's measured light-blue fill, black text
# ---------------------------------------------------------------------------


def test_self_bubble_is_not_green() -> None:
    css = _extract_style_block()
    self_rule = _extract_rule(css, ".tl-bubble-self")
    assert "#d9f7be" not in self_rule
    assert "#b7eb8f" not in self_rule


def test_self_bubble_uses_wecom_measured_light_blue() -> None:
    css = _extract_style_block()
    assert re.search(r":root\{[^}]*--bubble-self-bg\s*:\s*#cfe6fd\b", css, re.I), (
        "expected --bubble-self-bg: #cfe6fd defined on :root"
    )
    self_rule = _extract_rule(css, ".tl-bubble-self")
    assert "var(--bubble-self-bg)" in self_rule


def test_self_bubble_text_is_black_not_white() -> None:
    css = _extract_style_block()
    assert re.search(r":root\{[^}]*--bubble-self-text\s*:\s*#000\b", css, re.I), (
        "expected --bubble-self-text: #000 defined on :root"
    )
    self_rule = _extract_rule(css, ".tl-bubble-self")
    assert "var(--bubble-self-text)" in self_rule
    assert "#fff" not in self_rule and "white" not in self_rule


# ---------------------------------------------------------------------------
# Incoming (other) bubble: stays white with black text
# ---------------------------------------------------------------------------


def test_other_bubble_stays_white_with_black_text() -> None:
    css = _extract_style_block()
    other_rule = _extract_rule(css, ".tl-bubble-other")
    assert "var(--bubble-other-bg)" in other_rule
    assert "var(--bubble-other-text)" in other_rule
    assert re.search(r":root\{[^}]*--bubble-other-bg\s*:\s*#fff\b", css, re.I)
    assert re.search(r":root\{[^}]*--bubble-other-text\s*:\s*#000\b", css, re.I)


# ---------------------------------------------------------------------------
# Hyperlinks inside bubbles use the WeCom-measured link color
# ---------------------------------------------------------------------------


def test_bubble_links_use_wecom_link_color() -> None:
    css = _extract_style_block()
    assert re.search(r":root\{[^}]*--link-color\s*:\s*#417ce8\b", css, re.I), (
        "expected --link-color: #417ce8 defined on :root"
    )
    link_rule = _extract_rule(css, ".tl-bubble a")
    assert "var(--link-color)" in link_rule


# ---------------------------------------------------------------------------
# Bubble shape: rounded rectangle close to WeCom, not an oversized pill
# ---------------------------------------------------------------------------


def test_bubble_radius_matches_wecom_range() -> None:
    css = _extract_style_block()
    base_rule = _extract_rule(css, ".tl-bubble")
    match = re.search(r"border-radius\s*:\s*(\d+)px", base_rule)
    assert match is not None, "expected a px border-radius on .tl-bubble"
    radius = int(match.group(1))
    assert 8 <= radius <= 14, f"expected a WeCom-like 10-12px radius, got {radius}px"


# ---------------------------------------------------------------------------
# Media / placeholder styling: unaffected by this recolor
# ---------------------------------------------------------------------------


def test_base_media_placeholder_style_unchanged() -> None:
    css = _extract_style_block()
    base_rule = _extract_rule(css, ".media-placeholder")
    assert "#fafafa" in base_rule
    assert "#d9d9d9" in base_rule


# ---------------------------------------------------------------------------
# Direction classification / alignment logic: unchanged by this style fix
# ---------------------------------------------------------------------------


def test_row_alignment_classes_unchanged() -> None:
    css = _extract_style_block()
    assert "flex-end" in _extract_rule(css, ".tl-row-self")
    assert "flex-start" in _extract_rule(css, ".tl-row-other")


def test_render_timeline_direction_logic_unchanged() -> None:
    # RND-204: per-row direction/bubble logic now lives in timelineRowHtml(),
    # which renderTimeline() delegates to.
    match = re.search(
        r"function timelineRowHtml\(m\)\{.*?\n\}", _REVIEW_CONSOLE_HTML, re.S
    )
    assert match is not None
    src = match.group(0)
    assert "var isSelf=mode==='staff'&&selEntityId&&m.sender===selEntityId;" in src
    assert "var isStaff=m.sender&&m.sender.indexOf('staff_')===0;" in src
    assert (
        "var bc='tl-bubble '+(isSelf?'tl-bubble-self':"
        "(mode==='staff'?'tl-bubble-other':(isStaff?'tl-bubble-staff':'')));" in src
    )


# ---------------------------------------------------------------------------
# No tenant-specific staff IDs introduced
# ---------------------------------------------------------------------------


def test_no_hardcoded_tenant_staff_id_in_bubble_style_area() -> None:
    css = _extract_style_block()
    # The only staff-specific token allowed is the generic 'staff_' prefix
    # check already present in renderTimeline(); the CSS itself must not
    # reference any concrete tenant/staff identifier.
    assert not re.search(r"staff_[a-zA-Z0-9]+", css)
