import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_html_session
from app.config.guard import is_initialized
from app.db.session import get_db
from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG
from app.message_type_registry import build_filterable_type_options, build_frontend_registry_entries
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()

# RND-196 — the embedded MessageTypeRegistry's `entries` object (injected
# into templates/review_console.html via render_template, see
# admin_conversations below) is generated from app.message_type_registry
# instead of hand-duplicated: the backend Registry is now the runtime
# authority and the frontend consumes its exported metadata. Restricted to
# placeholder.<type> i18n keys that actually exist in app/assets/i18n.js
# so this can never emit a placeholderKey with no translation behind it —
# see build_frontend_registry_entries()'s docstring.
_KNOWN_PLACEHOLDER_I18N_KEYS = frozenset(
    re.findall(r'"(placeholder\.[a-zA-Z0-9_]+)"', I18N_JS_SOURCE)
)
_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON = json.dumps(
    build_frontend_registry_entries(known_placeholder_keys=_KNOWN_PLACEHOLDER_I18N_KEYS)
)

# RND-230 — the search page's "消息类型" filter options are generated from
# the same app.message_type_registry catalog as the console's
# MessageTypeRegistry above, instead of a second hand-maintained list —
# see build_filterable_type_options()'s docstring for why a hand-rolled
# subset previously drifted (missing raw types entirely, and mismatched
# "system"/"miniprogram" against the real stored "sys"/"weapp" values).
_SEARCH_MSGTYPE_OPTIONS_JSON = json.dumps(build_filterable_type_options())


@router.get("/admin/conversations", response_class=HTMLResponse)
def admin_conversations(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Conversation review console. Requires valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "review_console",
            i18n_script=I18N_SCRIPT_TAG,
            mtr_entries_json=_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON,
            # Archive Console v2 (design import): the inline sidebar search's
            # "全部类型" filter chip needs the same normalizedType -> rawValues
            # catalog the standalone search page's msgtype filter already
            # uses, to expand into a real `msgtype=...` query list instead of
            # a decorative no-op chip. Reuses the exact same pre-built JSON,
            # not a second computation.
            msgtype_options_json=_SEARCH_MSGTYPE_OPTIONS_JSON,
            sidenav=render_sidenav("review", {route.path for route in request.app.routes if hasattr(route, "path")}),
        )
    )


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Settings page (RND-302). Requires valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=render_template("settings", i18n_script=I18N_SCRIPT_TAG))


@router.get("/admin/settings/init", response_class=HTMLResponse)
def admin_settings_init(db=Depends(get_db)):
    """Render the public first-run bootstrap shell until setup is complete."""
    if is_initialized(db):
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=render_template("settings_init"))


@router.get("/admin/search", response_class=HTMLResponse)
def admin_search_page(
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Standalone search-results page (RND-229). Requires valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "search",
            i18n_script=I18N_SCRIPT_TAG,
            msgtype_options_json=_SEARCH_MSGTYPE_OPTIONS_JSON,
        )
    )


@router.get("/admin/analytics", response_class=HTMLResponse)
def admin_analytics(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Usage analytics page. Requires valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "analytics",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav(
                "analytics",
                {route.path for route in request.app.routes if hasattr(route, "path")},
            ),
        )
    )


@router.get("/admin/diagnostics/reachability", response_class=HTMLResponse)
def admin_diagnostics_reachability(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Archive health page backed by persistent reachability-check snapshots."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "diagnostics",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav(
                "diagnostics",
                {route.path for route in request.app.routes if hasattr(route, "path")},
            ),
        )
    )
