"""Shared admin-console side navigation renderer."""
from __future__ import annotations

from html import escape
from typing import Iterable

from app.settings import APP_EDITION_SELFHOST, get_app_edition

# One navigation configuration for every admin page. ``path`` determines
# whether an item is available.
NAV = (
    {
        "group_key": "nav.group.overview",
        "items": (
            {"id": "dashboard", "key": "nav.dashboard", "path": "/dashboard"},
            {"id": "billing", "key": "nav.billing", "path": "/admin/billing"},
        ),
    },
    {
        "group_key": "nav.group.review",
        "items": (
            {"id": "review", "key": "nav.conversationsReview", "path": "/admin/conversations"},
            {"id": "search", "key": "nav.globalSearch", "path": "/admin/search"},
        ),
    },
    {
        "group_key": "nav.group.data",
        "items": (
            {"id": "media", "key": "nav.mediaAttachments", "path": "/admin/media"},
            {"id": "exports", "key": "nav.exports", "path": "/admin/exports"},
            {"id": "favorites", "key": "nav.favorites", "path": "/admin/favorites"},
            {"id": "messages", "key": "nav.messages", "path": "/admin/messages"},
            {"id": "cleanup", "key": "nav.cleanup", "path": "/admin/cleanup"},
            {"id": "recycle-bin", "key": "nav.recycleBin", "path": "/admin/recycle-bin"},
        ),
    },
    {
        "group_key": "nav.group.directory",
        "items": (
            {"id": "staff", "key": "nav.staffDirectory", "path": "/admin/staff"},
            {"id": "contacts", "key": "nav.externalContacts", "path": "/admin/contacts"},
        ),
    },
    {
        "group_key": "nav.group.system",
        "items": (
            {"id": "users", "key": "nav.users", "path": "/admin/users"},
            {"id": "diagnostics", "key": "nav.diagnostics", "path": "/admin/diagnostics/reachability"},
            {"id": "settings", "key": "nav.settings", "path": "/admin/settings"},
        ),
    },
    {
        "group_key": "nav.group.help",
        "items": (
            {"id": "support", "key": "nav.support", "path": "/admin/support"},
        ),
    },
)



_NAV_DRAWER_MARKUP = (
    # GH-102: ≤900px the shared sidebar becomes a slide-in drawer (see
    # design-system.css); the hamburger + backdrop live here so every page
    # that renders the shared sidenav gets the narrow-screen switcher.
    '<button type="button" class="nav-toggle" id="nav-toggle" aria-label="菜单" '
    'aria-expanded="false" aria-controls="side-nav" onclick="toggleNav()">☰</button>'
    '<div class="nav-backdrop" id="nav-backdrop" hidden></div>'
)


def _nav_drawer_markup() -> str:
    return _NAV_DRAWER_MARKUP



def render_sidenav(active_id: str, registered_paths: Iterable[str]) -> str:
    """Render the shared sidebar, enabling only registered route paths."""
    paths = frozenset(registered_paths)
    selfhost = get_app_edition() == APP_EDITION_SELFHOST
    out = [
        _nav_drawer_markup(),
        '<nav class="side-nav" id="side-nav">',
        '  <div class="side-nav-brand">',
        '    <img class="side-nav-logo" src="/api/branding/logo" alt="品牌标识" width="24" height="24" onerror="this.onerror=null;this.src=\'/web/static/brand/icon-tile-24.svg\'">',
        '    <div class="side-nav-title" data-i18n="app.subtitle">对话审阅控制台</div>',
        '  </div>',
        '  <div class="side-nav-scroll">',
    ]
    for group in NAV:
        out.append(f'    <div class="side-nav-group" data-i18n="{escape(group["group_key"], quote=True)}"></div>')
        for item in group["items"]:
            if selfhost and item["id"] == "billing":
                continue
            item_id = escape(item["id"], quote=True)
            key = escape(item["key"], quote=True)
            path = item["path"]
            if path in paths:
                href = escape(item.get("href", path), quote=True)
                active = " active" if item["id"] == active_id else ""
                current = ' aria-current="page"' if item["id"] == active_id else ""
                out.append(
                    f'    <a class="side-nav-item{active}" href="{href}" data-i18n="{key}"{current}></a>'
                )
            else:
                out.append(
                    f'    <span class="side-nav-item side-nav-disabled" data-nav-id="{item_id}" '
                    f'aria-disabled="true"><span data-i18n="{key}"></span>'
                    '<em data-i18n="nav.comingSoon">即将推出</em></span>'
                )
        if (
            group["group_key"] == "nav.group.system"
            and selfhost
            and "/admin/provisioning/settings" in paths
        ):
            out.append(
                '    <a class="side-nav-item" href="/admin/provisioning/settings" '
                'data-i18n="nav.archiveConfig"></a>'
            )
    out.extend(
        (
            '  </div>',
            '  <div class="side-nav-user">',
            '    <span id="current-user"></span>',
            '    <div class="side-nav-user-row">',
            '      <div class="lang-switch" id="lang-switch">',
            '        <button class="btn-lang" id="btn-lang-toggle" type="button" onclick="toggleLangMenu()" data-i18n="nav.language">语言</button>',
            '        <div class="lang-menu" id="lang-menu" style="display:none"></div>',
            '      </div>',
            '      <button class="btn-logout" onclick="doLogout()" data-i18n="nav.logout">退出登录</button>',
            '    </div>',
            '  </div>',
            '</nav>',
        )
    )
    return "\n".join(out)


# Restricted navigation for provisioning tenants (RND-348). Shared by the
# provisioning pages and the owner billing page so the wizard steps stay in
# one place.
_PROVISIONING_NAV_ITEMS = (
    ("organization", "/admin/provisioning", "组织状态"),
    ("billing", "/admin/billing", "开始 15 天免费试用"),
    ("settings", "/admin/provisioning/settings", "配置准备"),
)


# Platform operations console (RND-414). A separate, un-navigated plane —
# no i18n (internal-only, Chinese-only by design) and every route ships in
# this same change, so unlike ``NAV`` above there is no disabled/"coming
# soon" state to render.
PLATFORM_NAV = (
    {
        "group": "总览",
        "items": (
            {"id": "dashboard", "label": "运营看板", "path": "/platform"},
        ),
    },
    {
        "group": "客户",
        "items": (
            {"id": "tenants", "label": "订阅状态", "path": "/platform/tenants"},
            {"id": "search", "label": "搜索", "path": "/platform/search"},
        ),
    },
    {
        "group": "基础设施",
        "items": (
            {"id": "api-performance", "label": "接口性能", "path": "/platform/api-performance"},
        ),
    },
    {
        "group": "治理",
        "items": (
            {"id": "audit", "label": "全局审计", "path": "/platform/audit"},
            {"id": "settings", "label": "账户与安全", "path": "/platform/settings"},
        ),
    },
)


def render_platform_sidenav(active_id: str) -> str:
    """Render the platform-plane side nav. ``active_id`` matches a
    ``PLATFORM_NAV`` item id; the tenant-detail page passes ``"tenants"`` so
    the list item stays highlighted while viewing one tenant (Spec §1)."""
    out = [
        _nav_drawer_markup(),
        '<nav class="side-nav" id="side-nav">',
        '  <div class="side-nav-brand">',
        '    <img class="side-nav-logo" src="/web/static/brand/icon-tile-24.svg" alt="康冠时代" width="24" height="24">',
        '    <div class="side-nav-title">平台运营</div>',
        '    <button class="btn btn-sm side-nav-toggle" type="button" id="platform-nav-toggle" '
        'aria-expanded="false" hidden>菜单</button>',
        '  </div>',
        '  <div class="side-nav-scroll">',
    ]
    for group in PLATFORM_NAV:
        out.append(f'    <div class="side-nav-group">{escape(group["group"])}</div>')
        for item in group["items"]:
            active = " active" if item["id"] == active_id else ""
            current = ' aria-current="page"' if item["id"] == active_id else ""
            label = escape(item["label"])
            if item.get("note"):
                label += f' <em>{escape(item["note"])}</em>'
            out.append(
                f'    <a class="side-nav-item{active}" href="{escape(item["path"], quote=True)}"{current}>{label}</a>'
            )
    out.extend(
        (
            '  </div>',
            '  <div class="side-nav-user"><small class="tz-note">UTC+8 · 数据延迟 &lt; 60s</small></div>',
            '</nav>',
        )
    )
    return "\n".join(out)


def render_platform_admin_bar(admin_email: str) -> str:
    """Render the platform-plane admin bar (RND-414). Present on every
    ``/platform/*`` page; distinct from the tenant-admin ``.topbar`` below
    it, which carries the per-page breadcrumb and global search."""
    return (
        '<div class="admin-bar">'
        '<strong>365 Archive</strong>'
        '<span class="badge">平台运营控制台</span>'
        '<span>内部系统 — 仅限平台运营人员 · 不展示聊天内容、搜索词与客户个人信息</span>'
        '<div class="right">'
        f'<span>{escape(admin_email)} · super_admin</span>'
        '<span class="badge">操作全量审计</span>'
        '<form method="post" action="/platform/logout" style="display:inline">'
        '<button class="btn btn-sm" type="submit">退出</button>'
        '</form>'
        '</div>'
        '</div>'
    )


def render_platform_topbar(breadcrumb_current: str) -> str:
    """Render the platform-plane topbar: breadcrumb + global tenant search.
    Search jumps straight to a tenant's detail page (Spec §1 "全局搜索"),
    it does not filter the tenant list."""
    return (
        '<header class="topbar">'
        '<div class="breadcrumb"><span>平台运营</span><span class="sep">/</span>'
        f'<span class="cur">{escape(breadcrumb_current)}</span></div>'
        '<div class="search-input"><span class="ico">⌕</span>'
        '<input class="input" id="platform-search" type="search" '
        'placeholder="搜索客户名称 / slug / corp_id，回车跳转详情"></div>'
        '<div class="topbar-actions"><span class="tz-note" id="platform-last-refresh"></span>'
        '<button class="btn btn-sm" type="button" id="platform-refresh">刷新数据</button></div>'
        '</header>'
    )


def render_provisioning_sidenav(active_id: str) -> str:
    """Render the cloud activation or local archive-configuration navigation."""
    selfhost = get_app_edition() == APP_EDITION_SELFHOST
    items = (
        (
            ("dashboard", "/dashboard", "回到控制台"),
            ("settings", "/admin/provisioning/settings", "会话存档配置"),
        )
        if selfhost
        else _PROVISIONING_NAV_ITEMS
    )
    title = "本地会话存档配置" if selfhost else "组织自助开通"
    group_title = "本地配置" if selfhost else "开通步骤"
    out = [
        '<nav class="side-nav" id="side-nav">',
        f'  <div class="side-nav-brand"><img class="side-nav-logo" src="/web/static/brand/icon-tile-24.svg" alt="康冠时代" width="24" height="24"><div class="side-nav-title">{title}</div></div>',
        '  <div class="side-nav-scroll">',
        f'    <div class="side-nav-group">{group_title}</div>',
    ]
    for item_id, href, label in items:
        active = " active" if item_id == active_id else ""
        current = ' aria-current="page"' if item_id == active_id else ""
        out.append(
            f'    <a class="side-nav-item{active}" href="{href}"{current}>{label}</a>'
        )
    out.extend(
        (
            '  </div>',
            '  <div class="side-nav-user"><button class="btn-logout" onclick="doLogout()">退出登录</button></div>',
            '</nav>',
        )
    )
    return "\n".join(out)
