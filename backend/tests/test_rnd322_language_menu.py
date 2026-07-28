"""RND-322 regression coverage for the review-console language menu."""
from __future__ import annotations

import re

from tests._rnd216_web_shims import review_console_html


def test_language_menu_opens_upward_inside_clipping_side_nav() -> None:
    """Keep the bottom navigation menu within `.side-nav`'s clipped bounds."""
    html = review_console_html()
    css = re.search(r"<style>(.*?)</style>", html, re.S)
    assert css is not None
    rule = re.search(r"\.lang-menu\{([^}]*)\}", css.group(1))
    assert rule is not None

    declarations = re.sub(r"\s+", "", rule.group(1))
    assert "position:absolute" in declarations
    assert "bottom:100%" in declarations
    assert "top:auto" in declarations
    assert "right:0" in declarations
