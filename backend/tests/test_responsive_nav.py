"""GH-102 guards: narrow-screen nav drawer + review-console responsive convergence."""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATES = _BACKEND / "app" / "web" / "templates"
_STATIC = _BACKEND / "app" / "web" / "static"


def _sidenav_py() -> str:
    return (_BACKEND / "app" / "web" / "sidenav.py").read_text(encoding="utf-8")


def test_shared_sidenav_emits_the_narrow_drawer_switcher() -> None:
    source = _sidenav_py()
    assert 'class="nav-toggle"' in source
    assert 'id="nav-backdrop"' in source
    assert "toggleNav()" in source
    assert 'aria-expanded="false"' in source
    # 汉堡属于管理侧与 provisioning 变体；platform 平面保持自有响应式。
    assert source.count("_nav_drawer_markup(),") == 2


def test_no_page_hides_the_side_nav_anymore() -> None:
    """GH-102: the drawer replaces the old display:none treatment."""
    for css in _STATIC.glob("*.css"):
        assert ".side-nav{display:none}" not in css.read_text(encoding="utf-8"), css.name
    for template in _TEMPLATES.glob("*.html"):
        assert ".side-nav{display:none}" not in template.read_text(encoding="utf-8"), template.name


def test_design_system_drawer_css_is_class_gated() -> None:
    css = (_STATIC / "design-system.css").read_text(encoding="utf-8")

    # 类门控：platform 从不加载 nav-drawer.js，抽屉样式对其零泄漏。
    assert "html.has-nav-drawer .side-nav{position:fixed" in css
    assert "html.has-nav-drawer .side-nav.open{transform:none" in css
    assert "html.has-nav-drawer .nav-toggle{display:inline-flex" in css
    assert "html.has-nav-drawer .topbar{padding-left:52px}" in css


def test_nav_drawer_js_contract() -> None:
    source = (_STATIC / "nav-drawer.js").read_text(encoding="utf-8")

    assert "has-nav-drawer" in source
    assert "wecom_admin_theme" not in source  # 与 theme.js 职责分离
    assert "window.toggleNav = toggleNav" in source
    assert "event.key === 'Escape'" in source
    assert "closest('a')) closeNav()" in source


def test_render_template_injects_both_bootstrap_scripts() -> None:
    source = (_BACKEND / "app" / "web" / "__init__.py").read_text(encoding="utf-8")
    assert "/web/static/theme.js" in source
    assert "/web/static/nav-drawer.js" in source
    assert 'name.startswith("platform")' in source


def test_review_console_narrow_breakpoints() -> None:
    source = (_TEMPLATES / "review_console.html").read_text(encoding="utf-8")

    # 会话列表抽屉（≤900）
    assert 'id="btn-conv-list"' in source
    assert "onclick=\"toggleConvList()\"" in source
    assert 'id="drawer-backdrop"' in source
    assert ".col-conv{position:fixed;left:0;top:0;bottom:0;width:min(328px,86vw)" in source
    assert ".col-conv.conv-open{transform:none}" in source
    # 详情面板 overlay（≤1300），宽屏保留 closed=隐藏 语义
    assert 'id="panel-backdrop"' in source
    assert ".col-panel.panel-closed{display:none}" in source
    assert ".col-panel.panel-closed{display:flex;transform:translateX(100%)}" in source
    assert ".col-panel.panel-open{transform:none}" in source
    # 640 单栏
    assert "@media (max-width: 640px){" in source
    assert ".col-conv{width:100vw}" in source
    # scope-popover 窄屏限宽
    assert ".scope-popover{max-width:calc(100vw - 24px)}" in source


def test_console_panel_toggle_is_class_based() -> None:
    source = (_STATIC / "console" / "console-entry.js").read_text(encoding="utf-8")

    assert "applyPanelState()" in source
    assert "col.style.display=panelOpen" not in source
    assert "matchMedia('(max-width:1300px)').matches" in source
    assert "toggleConvList" in source
    assert "closeConvList" in source


def test_conv_list_i18n_keys_exist_in_all_three_locales() -> None:
    source = (_BACKEND / "app" / "assets" / "i18n.js").read_text(encoding="utf-8")
    for value in ("会话列表", "會話列表", "Conversations"):
        assert f'"console.convList": "{value}"' in source, value
