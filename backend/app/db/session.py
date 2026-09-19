from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.settings import get_database_settings

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        url = get_database_settings().database_url.strip()
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        # 2026-09-02 production 504: pool_size=2 + max_overflow=2 (4 slots)
        # exhausted under ordinary page-load concurrency and every auth lookup
        # queued 30s into TimeoutErrors. 3+2 restores headroom while staying
        # within the RND-191/RND-279 pool_size+max_overflow <= 5 discipline
        # (small pool bounds N+1 blast radius); pool_pre_ping evicts
        # server-dropped connections instead of serving them.
        _engine = create_engine(url, pool_size=3, max_overflow=2, pool_pre_ping=True)
    return _engine


def get_db() -> Generator[Session, None, None]:
    with Session(_get_engine()) as session:
        yield session


def get_engine():
    """Public accessor for the shared engine — used by readiness checks
    (app.db.schema_check) that need a raw connection rather than an
    ORM Session."""
    return _get_engine()
