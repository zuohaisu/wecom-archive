"""RND-322 regression coverage for the review-console language menu."""
from __future__ import annotations

import re
from pathlib import Path

from tests._rnd216_web_shims import review_console_html


def test_language_menu_opens_upward_inside_clipping_side_nav() -> None:
    """Keep the bottom navigation menu within `.side-nav`'s clipped bounds.
    
    Note (RND-324): Menu should use left:0 alignment (not right:0) to prevent
    overflow off the left edge of narrow viewports.
    """
    html = review_console_html()
    # GH-100: the console carries no local .lang-menu override at all —
    # the shared rule from design-system.css is the single source.
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert ".lang-menu" not in css

    ds = (
        Path(__file__).resolve().parents[1]
        / "app" / "web" / "static" / "design-system.css"
    ).read_text(encoding="utf-8")
    rule = re.search(r"\.lang-menu\{([^}]*)\}", ds)
    assert rule is not None

    declarations = re.sub(r"\s+", "", rule.group(1))
    assert "position:absolute" in declarations
    # bottom:100% alone keeps it opening upward (top defaults to auto).
    assert "bottom:100%" in declarations
    # RND-324 fix: use left alignment instead of right to prevent viewport
    # overflow (right defaults to auto once left:0 pins the edge).
    assert "left:0" in declarations
