"""SSR route for the tenant-scoped favorites page (RND-369)."""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_html_session
from app.i18n_assets import I18N_SCRIPT_TAG
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()


@router.get("/admin/favorites", response_class=HTMLResponse)
def admin_favorites_page(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Render the authenticated favorites library; data loads from its API."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    return HTMLResponse(
        content=render_template(
            "favorites",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav(
                "favorites",
                {route.path for route in request.app.routes if hasattr(route, "path")},
            ),
        )
    )
