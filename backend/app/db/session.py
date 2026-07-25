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
        _engine = create_engine(url, pool_size=2, max_overflow=2)
    return _engine


def get_db() -> Generator[Session, None, None]:
    with Session(_get_engine()) as session:
        yield session


def get_engine():
    """Public accessor for the shared engine — used by readiness checks
    (app.db.schema_check) that need a raw connection rather than an
    ORM Session."""
    return _get_engine()
