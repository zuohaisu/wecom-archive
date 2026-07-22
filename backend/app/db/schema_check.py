"""
Shared Alembic revision + DB connectivity checks (RND-227).

Used by both the readiness health endpoints (app.main) and the deploy-time
revision-verification CLI (scripts/verify_alembic_head.py) so the two
paths can never silently drift out of sync — there is exactly one place
that knows what "the database is on the right schema" means.

Nothing here ever returns or logs a connection string; failures are
reported as revision ids (safe, already-public in git) or generic
exception type names only.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_DIR / "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = _BACKEND_DIR / "alembic"


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))
    return cfg


def repository_head_revisions() -> frozenset[str]:
    """Revision id(s) alembic/versions/ considers HEAD, purely from the
    files on disk — no DB connection involved. A branched migration
    history can legitimately have more than one head, so this is a set,
    not a single value."""
    script = ScriptDirectory.from_config(_alembic_config())
    return frozenset(script.get_heads())


def database_current_revisions(connection: Connection) -> frozenset[str]:
    """Revision id(s) actually applied to `connection`'s database. Empty
    if alembic_version has no rows (never migrated) or the table does
    not exist yet."""
    ctx = MigrationContext.configure(connection)
    return frozenset(ctx.get_current_heads())


def revisions_match(db_revisions: frozenset[str], repo_revisions: frozenset[str]) -> bool:
    """DB current revision set must equal the repository head set
    exactly. Covers: a single head (the common case), a deliberate
    multi-head branch (both sets must match member-for-member), and an
    unmigrated database (empty db_revisions never equals a non-empty
    repo_revisions)."""
    return bool(repo_revisions) and db_revisions == repo_revisions


def check_revision(connection: Connection) -> tuple[bool, str, frozenset[str], frozenset[str]]:
    """Returns (ok, message, db_revisions, repo_revisions). `message` is
    safe to print/log — it contains only revision ids, never a
    connection string or stack trace."""
    repo_revs = repository_head_revisions()
    db_revs = database_current_revisions(connection)
    ok = revisions_match(db_revs, repo_revs)
    db_desc = sorted(db_revs) if db_revs else "<no revision>"
    repo_desc = sorted(repo_revs) if repo_revs else "<no migrations found>"
    if ok:
        message = f"OK: database revision(s) {db_desc} match repository head {repo_desc}"
    else:
        message = f"MISMATCH: database at {db_desc}, repository head is {repo_desc}"
    return ok, message, db_revs, repo_revs


def check_database_alive(connection: Connection) -> None:
    """Raises on failure/timeout; callers decide how to report it."""
    connection.execute(text("SELECT 1"))


def full_readiness_check(engine: Engine) -> tuple[bool, str]:
    """Combined DB-alive + schema-revision check for the HTTP readiness
    endpoint. Any failure (connection refused, auth failure, missing
    table, revision mismatch, ...) collapses to a single generic,
    secret-free message — the caller (an unauthenticated endpoint) must
    never leak exception text, which can occasionally embed connection
    details depending on the driver."""
    try:
        with engine.connect() as connection:
            check_database_alive(connection)
            ok, message, _, _ = check_revision(connection)
            return ok, message
    except Exception:
        return False, "database unreachable or schema check failed"
