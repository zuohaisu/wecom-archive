"""Real-PostgreSQL regression for overlapping favorite batches (RND-366)."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import BrokenBarrierError, local

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    AdminUser,
    ArchiveFavorite,
    ArchiveMessage,
    ArchiveMessageRecipient,
    MediaFile,
    Tenant,
)
from app.routers.favorites import _commit_mutation, _mutate_or_503
from app.schemas.favorites import FavoriteBatchIn, FavoriteObjectIn
import app.services.favorites as favorite_service


def _postgres_admin_url():
    return make_url(
        os.environ.get(
            "RND201_TEST_DATABASE_URL",
            f"postgresql://{os.environ.get('USER', 'postgres')}@/postgres?host=/tmp",
        )
    )


def _local_postgres_url():
    url = _postgres_admin_url()
    # Use only a local Unix socket; never let this integration test follow an
    # environment URL to a network database.
    if url.host is not None:
        return None
    socket_host = url.query.get("host")
    if socket_host is None or not str(socket_host).startswith("/"):
        return None
    return url


def _postgres_reachable() -> bool:
    url = _local_postgres_url()
    if url is None:
        return False
    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        if engine is not None:
            engine.dispose()


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable local-only PostgreSQL for concurrency regression"
)


def _create_test_schema(engine) -> None:
    # These unrelated trigram/full-text indexes require optional extensions;
    # the lock race only needs the actual tenant, message, media and favorite
    # tables with their constraints.
    tables_with_optional_indexes = (AdminUser.__table__, ArchiveMessage.__table__)
    saved_indexes = {table: set(table.indexes) for table in tables_with_optional_indexes}
    try:
        for table in tables_with_optional_indexes:
            table.indexes.clear()
        Base.metadata.create_all(
            engine,
            tables=[
                Tenant.__table__,
                AdminUser.__table__,
                ArchiveMessage.__table__,
                ArchiveMessageRecipient.__table__,
                MediaFile.__table__,
                ArchiveFavorite.__table__,
            ],
        )
    finally:
        for table, indexes in saved_indexes.items():
            table.indexes.update(indexes)


@pytest.mark.parametrize(
    ("first_batch", "second_batch", "expected_favorites"),
    [
        (
            (("message", "race-a"), ("message", "race-b")),
            (("message", "race-b"), ("message", "race-a")),
            2,
        ),
        (
            (("media", "101"), ("message", "race-a")),
            (("media", "102"), ("message", "race-b")),
            4,
        ),
    ],
    ids=("reverse-message-order", "cross-parent-media-locks"),
)
def test_overlapping_batches_do_not_deadlock_on_postgresql(
    monkeypatch, first_batch, second_batch, expected_favorites
) -> None:
    """Batch and joined-media locks must not form a PostgreSQL lock cycle."""
    admin_url = _local_postgres_url()
    assert admin_url is not None
    database_name = f"rnd366_favorites_{uuid.uuid4().hex[:16]}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    created = False
    engine = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        created = True
        target_url = admin_url.set(database=database_name)
        engine = create_engine(target_url, pool_size=4, max_overflow=0)
        _create_test_schema(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            db.add(Tenant(id="tenant-race", name="Race", slug="race"))
            db.flush()
            db.add(
                AdminUser(
                    id="owner-race",
                    tenant_id="tenant-race",
                    wecom_user_id="staff_race",
                    role="owner",
                )
            )
            db.add_all(
                [
                    ArchiveMessage(
                        id=1,
                        msgid="race-a",
                        seq=1,
                        publickey_ver=1,
                        encrypt_random_key="synthetic-key-a",
                        encrypt_chat_msg="synthetic-envelope-a",
                        decrypt_status="success",
                        content_text="synthetic A",
                        msgtype="text",
                        sender="staff_race",
                        tenant_id="tenant-race",
                        msgtime=1,
                    ),
                    ArchiveMessage(
                        id=2,
                        msgid="race-b",
                        seq=2,
                        publickey_ver=1,
                        encrypt_random_key="synthetic-key-b",
                        encrypt_chat_msg="synthetic-envelope-b",
                        decrypt_status="success",
                        content_text="synthetic B",
                        msgtype="text",
                        sender="staff_race",
                        tenant_id="tenant-race",
                        msgtime=2,
                    ),
                ]
            )
            db.add_all(
                [
                    MediaFile(
                        id=101,
                        sdkfileid="synthetic-media-a",
                        archive_message_id=2,
                        tenant_id="tenant-race",
                    ),
                    MediaFile(
                        id=102,
                        sdkfileid="synthetic-media-b",
                        archive_message_id=1,
                        tenant_id="tenant-race",
                    ),
                ]
            )
            db.commit()

        # The pause after each transaction's first target makes the pre-fix
        # opposite-order lock cycle deterministic. With canonical ordering,
        # one transaction waits for the first target before reaching the
        # barrier; its timeout releases the first lock holder to finish.
        first_target_barrier = threading.Barrier(2)
        thread_state = local()
        apply_one = favorite_service._apply_one

        def _pause_after_first_target(*args, **kwargs):
            result = apply_one(*args, **kwargs)
            call_count = getattr(thread_state, "call_count", 0) + 1
            thread_state.call_count = call_count
            if call_count == 1:
                try:
                    first_target_barrier.wait(timeout=1.0)
                except BrokenBarrierError:
                    pass
            return result

        monkeypatch.setattr(favorite_service, "write_audit", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(favorite_service, "_apply_one", _pause_after_first_target)

        def _mutate_in_own_transaction(objects: tuple[tuple[str, str], tuple[str, str]]):
            with factory() as db:
                db.execute(text("SET LOCAL statement_timeout = '10s'"))
                batch = FavoriteBatchIn(
                    action="favorite",
                    items=[
                        FavoriteObjectIn(object_type=object_type, object_id=object_id)
                        for object_type, object_id in objects
                    ],
                )
                try:
                    result = _mutate_or_503(
                        db,
                        lambda: favorite_service.mutate_favorites(
                            db,
                            tenant_id="tenant-race",
                            actor_id="owner-race",
                            payload=batch,
                        ),
                    )
                    _commit_mutation(db)
                    return 200, result
                except Exception as exc:
                    db.rollback()
                    return getattr(exc, "status_code", None), type(exc).__name__

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_mutate_in_own_transaction, first_batch),
                pool.submit(_mutate_in_own_transaction, second_batch),
            ]
            outcomes = [future.result(timeout=15) for future in futures]

        assert [status for status, _result in outcomes] == [200, 200]
        with factory() as db:
            assert (
                db.query(ArchiveFavorite)
                .filter_by(tenant_id="tenant-race")
                .count()
                == expected_favorites
            )
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
