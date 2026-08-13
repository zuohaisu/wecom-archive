"""Shared admin-console side navigation renderer."""
from __future__ import annotations

from html import escape
from typing import Iterable

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
            {"id": "messages", "key": "nav.messages", "path": "/admin/messages"},
            {"id": "media", "key": "nav.mediaAttachments", "path": "/admin/media"},
            {"id": "exports", "key": "nav.exports", "path": "/admin/exports"},
        ),
    },
    {
        "group_key": "nav.group.directory",
        "items": (
            {"id": "users", "key": "nav.staffSeats", "path": "/admin/users"},
            {"id": "contacts", "key": "nav.externalContacts", "path": "/admin/contacts"},
        ),
    },
    {
        "group_key": "nav.group.system",
        "items": (
            {"id": "diagnostics", "key": "nav.diagnostics", "path": "/admin/diagnostics/reachability"},
            {"id": "settings", "key": "nav.settings", "path": "/admin/settings"},
        ),
    },
)


def render_sidenav(active_id: str, registered_paths: Iterable[str]) -> str:
    """Render the shared sidebar, enabling only registered route paths."""
    paths = frozenset(registered_paths)
    out = [
        '<nav class="side-nav">',
        '  <div class="side-nav-brand">',
        '    <img class="side-nav-logo" src="/web/static/brand/icon-tile-24.svg" alt="康冠时代" width="24" height="24">',
        '    <div class="side-nav-title" data-i18n="app.subtitle">对话审阅控制台</div>',
        '  </div>',
        '  <div class="side-nav-scroll">',
    ]
    for group in NAV:
        out.append(f'    <div class="side-nav-group" data-i18n="{escape(group["group_key"], quote=True)}"></div>')
        for item in group["items"]:
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
