import logging
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from app.db.schema_check import full_readiness_check
from app.db.session import get_engine
from app.routers.admin_audit_page import router as admin_audit_page_router
from app.routers.admin_contacts_page import router as admin_contacts_page_router
from app.routers.admin_media_page import router as admin_media_page_router
from app.routers.admin_users_page import router as admin_users_page_router
from app.routers.audit import router as audit_router
from app.routers.auth import router as auth_router
from app.routers.conversations import router as conversations_router
from app.routers.external_contacts import router as external_contacts_router
from app.routers.media import MediaAccessNoStoreMiddleware
from app.routers.media import router as media_router
from app.routers.media_library import router as media_library_router
from app.routers.messages import router as messages_router
from app.routers.reachability_audit import router as reachability_audit_router
from app.routers.search import router as search_router
from app.routers.settings import settings_router
from app.routers.sync import router as sync_router
from app.routers.users import users_router
from app.routers.web import router as web_router
from app.routers.wecom_events import router as wecom_events_router

logger = logging.getLogger(__name__)


class _RedactOAuthCallbackQueryFilter(logging.Filter):
    """
    RND-225: uvicorn's default access logger (enabled unless the process is
    started with --no-access-log — see docs/DEPLOYMENT.md, which does not
    pass it) records the full request line for every request, including the
    query string. The WeCom OAuth callback's `code` (a short-lived but
    directly replayable authorization code) and `state` (CSRF token) would
    otherwise be written verbatim to that log on every login. This does not
    cover a reverse proxy's own access log, if one is placed in front of
    this app — that needs equivalent redaction configured separately.
    """

    _REDACT_PREFIXES = ("/api/auth/wecom/callback?",)

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2]
            if path.startswith(self._REDACT_PREFIXES):
                redacted = path.split("?", 1)[0] + "?[REDACTED]"
                record.args = (args[0], args[1], redacted, args[3], args[4])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactOAuthCallbackQueryFilter())


def _readiness_body(response: Response) -> dict:
    """RND-227: real readiness, not a static ok. A DB outage or a schema
    that hasn't been migrated to the repository's head must never report
    HTTP 200 — that was the exact gap that let deploys restart the
    service ahead of `alembic upgrade head` and serve UndefinedColumn
    500s. Never exposes exception text, connection strings, or revision
    ids in the response body (kept generic on purpose for an
    unauthenticated endpoint) — see app.db.schema_check.full_readiness_check
    for the detail that stays server-side only."""
    try:
        ok, _detail = full_readiness_check(get_engine())
    except Exception:
        # get_engine() itself can raise (e.g. DATABASE_URL unset) before
        # full_readiness_check's own try/except ever starts — caught
        # here too so this endpoint can never surface a 500/stack trace,
        # only the documented 200/503 contract.
        ok = False
    if not ok:
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}


class _VersionedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


_STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app() -> FastAPI:
    """Composition root: builds and returns a fresh, fully-wired FastAPI
    instance. Kept as a factory (RND-223) rather than a module-level side
    effect so tests/tooling can construct an independent app instance; the
    module-level `app` below is what `uvicorn app.main:app` actually serves."""
    app = FastAPI(title="Crowntime WeCom Archive")
    # RND-187: guarantees Cache-Control: no-store on every response (success or
    # error, any status code) for the media access descriptor endpoint — see
    # MediaAccessNoStoreMiddleware's docstring for why this must be a
    # response-side middleware rather than a header set inside the route.
    app.add_middleware(MediaAccessNoStoreMiddleware)
    app.include_router(auth_router)
    app.include_router(conversations_router)
    app.include_router(media_router)
    app.include_router(reachability_audit_router)
    app.include_router(search_router)
    app.include_router(sync_router, prefix="/api/admin")
    app.include_router(audit_router, prefix="/api/admin")
    app.include_router(users_router, prefix="/api/admin")
    app.include_router(settings_router, prefix="/api/admin")
    app.include_router(media_library_router, prefix="/api/admin")
    app.include_router(external_contacts_router, prefix="/api/admin")
    app.include_router(wecom_events_router)
    app.include_router(admin_users_page_router)
    app.include_router(admin_audit_page_router)
    app.include_router(admin_media_page_router)
    app.include_router(admin_contacts_page_router)
    app.include_router(web_router)
    app.include_router(messages_router)

    @app.get("/health/live")
    def health_live():
        """Liveness only: the process can respond to HTTP at all. Does not
        touch the database — a DB outage must not make this fail, or
        orchestration tooling would kill/restart a process that isn't the
        actual problem. Use /health/ready (or /health, see below) to gate
        deploys and load-balancer readiness."""
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready(response: Response):
        return _readiness_body(response)

    @app.get("/health")
    def health(response: Response):
        """Kept as an alias for /health/ready (not liveness) for backward
        compatibility: every existing caller (scripts/deploy_server.sh's
        public check, external uptime monitoring) already treats this path
        as "is the service actually usable", and downgrading it to a
        liveness-only check would silently reintroduce the schema-drift gap
        this endpoint exists to close."""
        return _readiness_body(response)

    # -----------------------------------------------------------------------
    # RND-216 — admin console static assets (base.css/diagnostics.css,
    # search.js/diagnostics.js, and — since RND-217 split it into 8 modules —
    # web/static/console/*.js). Mounted last so it never shadows an API route.
    # Every template references these through
    # app.web.STATIC_VERSION's `?v=<hash>` query string (see render_template),
    # so a long, immutable Cache-Control here is safe: any content change
    # produces a new URL, and stale-cached responses under the old URL are
    # simply never requested again. This is unrelated to (and does not need an
    # exemption from) app.routers.media.MediaAccessNoStoreMiddleware —
    # that middleware only touches the media-access-descriptor path pattern
    # (see _MEDIA_ACCESS_PATH_RE), never /web/static.
    # -----------------------------------------------------------------------
    app.mount(
        "/web/static",
        _VersionedStaticFiles(directory=str(_STATIC_DIR), check_dir=False),
        name="web-static",
    )

    return app


app = create_app()  # 保持 uvicorn app.main:app 完全兼容
