"""Purge-script contract: mock_ingest rows go, real rows stay, dry run
touches nothing."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.purge_mock_ingest import _plan, _purge

_SCHEMA = """
CREATE TABLE archive_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, msgid TEXT NOT NULL, tenant_id TEXT);
CREATE TABLE archive_message_recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, tenant_id TEXT);
CREATE TABLE media_files (id INTEGER PRIMARY KEY AUTOINCREMENT, archive_message_id INTEGER NOT NULL, tenant_id TEXT);
CREATE TABLE archive_favorites (id INTEGER PRIMARY KEY AUTOINCREMENT, archive_message_id INTEGER NOT NULL, tenant_id TEXT);
CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, wecom_userid TEXT NOT NULL, tenant_id TEXT);
"""
TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "purge.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    rows = [
        ("INSERT INTO archive_messages (msgid, tenant_id) VALUES (?, ?)", [
            ("mock_msg_1", TENANT), ("mock_msg_2", TENANT),
            ("real_msg_1", TENANT),
        ]),
        ("INSERT INTO archive_message_recipients (message_id, tenant_id) VALUES (?, ?)", [
            (1, TENANT), (2, TENANT), (3, TENANT),
        ]),
        ("INSERT INTO media_files (archive_message_id, tenant_id) VALUES (?, ?)", [
            (1, TENANT),
        ]),
        ("INSERT INTO archive_favorites (archive_message_id, tenant_id) VALUES (?, ?)", [
            (1, TENANT),
        ]),
        ("INSERT INTO contacts (wecom_userid, tenant_id) VALUES (?, ?)", [
            ("staff_alice", TENANT), ("guest_bob", TENANT),
        ]),
    ]
    for sql, params in rows:
        conn.executemany(sql, params)
    conn.commit()
    yield db_path, conn
    conn.close()


def test_plan_counts_mock_rows_only(seeded_db) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    db_path, _ = seeded_db
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        plan = _plan(session)
    assert plan == {
        "messages": 2, "recipients": 2, "media_files": 1,
        "favorites": 1, "contacts": 0,
    }


def test_purge_removes_mock_rows_and_keeps_real_ones(seeded_db, capsys) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    db_path, _ = seeded_db
    engine = create_engine(f"sqlite:///{db_path}")

    # dry run：不删除任何行
    with Session(engine) as session:
        _plan(session)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM archive_messages")).scalar() == 3

    # --apply：mock 行删除、真实行保留
    with Session(engine) as session:
        counts = _purge(session)
        session.commit()

    assert counts["messages"] == 2 and counts["contacts"] == 0
    with engine.connect() as conn:
        remaining = [
            r[0] for r in conn.execute(text("SELECT msgid FROM archive_messages"))
        ]
    assert remaining == ["real_msg_1"]
