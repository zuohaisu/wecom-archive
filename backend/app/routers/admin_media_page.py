"""SSR route for the RND-329 admin media page."""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_html_session
from app.i18n_assets import I18N_SCRIPT_TAG
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()


@router.get("/admin/media", response_class=HTMLResponse)
def admin_media_page(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Render the authenticated media library; data loads from its API."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "media",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav(
                "media",
                {route.path for route in request.app.routes if hasattr(route, "path")},
            ),
        )
    )
