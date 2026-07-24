import json
import re
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_html_session
from app.i18n_assets import I18N_JS_SOURCE, I18N_SCRIPT_TAG
from app.message_type_registry import build_filterable_type_options, build_frontend_registry_entries
from app.web import render_template

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
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Three-column conversation review console. Requires valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "review_console",
            i18n_script=I18N_SCRIPT_TAG,
            mtr_entries_json=_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON,
        )
    )


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


@router.get("/admin/diagnostics/reachability", response_class=HTMLResponse)
def admin_diagnostics_reachability(
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """
    Message Reachability diagnostics page (RND-180). Requires valid session.

    Thin UI over GET /api/admin/reachability-audit (RND-178) — this route
    only renders the page shell; all reachability classification and
    aggregation happens server-side in app.reachability_audit and is
    fetched client-side from that endpoint, never recomputed here.
    """
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(content=render_template("diagnostics", i18n_script=I18N_SCRIPT_TAG))
