"""SSR route for the RND-364 recycle-bin page."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_html_session
from app.i18n_assets import I18N_SCRIPT_TAG
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()


@router.get("/admin/recycle-bin", response_class=HTMLResponse)
def recycle_bin_page(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Render the tenant recycle bin; data loads from its API."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "recycle_bin",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav(
                "recycle-bin",
                {route.path for route in request.app.routes if hasattr(route, "path")},
            ),
        )
    )
