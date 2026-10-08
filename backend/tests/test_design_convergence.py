"""GH-100 design-system convergence guards.

The project once carried six parallel design languages; this file pins the
three the issue retired so they cannot quietly grow back.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATES = _BACKEND / "app" / "web" / "templates"

_AUTH_PAGES = (
    "login.html",
    "forgot_password.html",
    "reset_password.html",
    "organization_confirm.html",
    "settings_init.html",
)


def test_no_template_references_the_retired_styles_css() -> None:
    styles_css = _BACKEND / "app" / "web" / "static" / "styles.css"
    assert not styles_css.exists(), "styles.css was retired; do not resurrect it"
    for template in _TEMPLATES.glob("*.html"):
        assert "static/styles.css" not in template.read_text(encoding="utf-8"), template.name


def test_auth_pages_load_design_system_css() -> None:
    for name in _AUTH_PAGES:
        source = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert 'href="/web/static/design-system.css' in source, name


def test_search_page_uses_design_tokens_not_a_private_blue() -> None:
    source = (_TEMPLATES / "search.html").read_text(encoding="utf-8")

    assert "#1890ff" not in source, "search must ride --color-primary (#1677ff)"
    assert not re.search(r"var\(--(primary|primary-hover|primary-soft|topbar-|page-bg|col-bg|kw|err)\b", source), (
        "search.html must not reference the retired private custom properties"
    )
    # 搜索关键词高亮保留语义色，落在专用规则上而非全局变量。
    assert re.search(r"\.rc-snippet mark\{[^}]*color:#e00", source)


def test_review_console_inline_style_is_only_page_specific_tweaks() -> None:
    """GH-100: console component styles live in design-system.css; the
    inline block keeps only the page shell (:root bubble vars, body frame,
    three-column layout) under 50 lines."""
    source = (_TEMPLATES / "review_console.html").read_text(encoding="utf-8")
    style_block = source.split("<style>", 1)[1].split("</style>", 1)[0]

    assert 'href="/web/static/design-system.css' in source
    assert len(style_block.splitlines()) < 50
    # 共享组件不再内联覆盖：侧边栏家族完全继承共享系统。
    assert ".side-nav" not in style_block
    # 气泡语义变量按 issue 要求留在页面 :root。
    assert "--bubble-self-bg" in style_block


def test_design_system_hosts_the_console_component_rules() -> None:
    css = (_BACKEND / "app" / "web" / "static" / "design-system.css").read_text(
        encoding="utf-8"
    )
    for marker in (
        ".tl-bubble{",
        ".tl-bubble-self{",
        ".conv-card{",
        ".scope-popover{",
        ".panel-stat{",
    ):
        assert marker in css, marker
