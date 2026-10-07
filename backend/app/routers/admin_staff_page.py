"""SSR route for the internal-staff directory page (Haisu split request)."""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_html_session
from app.i18n_assets import I18N_SCRIPT_TAG
from app.web import render_template
from app.web.sidenav import render_sidenav

router = APIRouter()


@router.get("/admin/staff", response_class=HTMLResponse)
def admin_staff_page(
    request: Request,
    tenant_id: Optional[str] = Depends(require_html_session),
):
    """Internal-staff directory page. Requires a valid session."""
    if tenant_id is None:
        return RedirectResponse("/admin/login", status_code=302)
    registered_paths = {
        route.path for route in request.app.routes if hasattr(route, "path")
    }
    return HTMLResponse(
        content=render_template(
            "staff",
            i18n_script=I18N_SCRIPT_TAG,
            sidenav=render_sidenav("staff", registered_paths),
        )
    )
