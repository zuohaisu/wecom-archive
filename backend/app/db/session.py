import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        _engine = create_engine(url)
    return _engine


def get_db() -> Generator[Session, None, None]:
    with Session(_get_engine()) as session:
        yield session
