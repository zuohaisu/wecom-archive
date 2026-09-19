"""Issue #149 — web engine pool sizing after the 2026-09-02 504 incident.

The incident: the web engine carried pool_size=2 + max_overflow=2 (4 slots
total). Ordinary page-load concurrency (peak 129 req/min, every request
running an auth lookup) exhausted the pool; further requests queued for the
30-second pool_timeout and raised TimeoutError, producing the 504 cascade.
The engine now carries 5 slots with pool_pre_ping; these tests pin both so
a future edit cannot silently shrink the pool or drop health probing again.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.db import session as db_session


def _rebuild_engine(monkeypatch, url: str):
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(
        db_session,
        "get_database_settings",
        lambda: SimpleNamespace(database_url=url),
    )
    return db_session.get_engine()


def test_web_engine_pool_holds_five_slots(monkeypatch, tmp_path) -> None:
    engine = _rebuild_engine(monkeypatch, f"sqlite:///{tmp_path}/pool.db")
    assert engine.pool.size() == 3
    assert engine.pool._max_overflow == 2  # 3 + 2 = 5, the RND-191/RND-279 ceiling


def test_web_engine_pool_pre_ping_is_enabled(monkeypatch, tmp_path) -> None:
    engine = _rebuild_engine(monkeypatch, f"sqlite:///{tmp_path}/pool.db")
    assert engine.pool._pre_ping is True


def test_web_engine_is_a_cached_singleton(monkeypatch, tmp_path) -> None:
    url = f"sqlite:///{tmp_path}/pool.db"
    first = _rebuild_engine(monkeypatch, url)
    assert db_session.get_engine() is first
