"""
Tests for RND-228 — Optimize search scalability and shared staff resolution.

Validates:
  - search_contacts bounds each source query (Contact, AdminUser) at the
    SQL level (name-match-first ordering + LIMIT) instead of pulling every
    matching row into Python before truncating with a slice.
  - The existing ordering/dedup/AdminUser-name-precedence contract from
    RND-159 is preserved when the match count far exceeds `limit`.
  - _collect_staff_ids / _load_display_names_for_ids (reused from
    app.conversation_membership) drive search_messages identically to the
    removed parallel implementations they replaced.

Reuses the sqlite-backed schema/fixtures from test_reachability_audit.py
and the client/auth helpers from test_search_api.py.

Run (from backend/):
    pytest tests/test_rnd_228_search_scalability.py -v

# flake8: noqa: F811
"""

from __future__ import annotations

from sqlalchemy.orm import Query as ORMQuery

from app.db.models import AdminUser, Contact

from tests.test_reachability_audit import (
    _TENANT_A,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 — pytest fixture, must be imported to be discovered
)
from tests.test_search_api import (
    client,  # noqa: F401 — pytest fixture, must be imported to be discovered
    _authed,
    _insert_contact,
)


def test_search_contacts_bounded_result_with_many_matches(client, db) -> None:
    """With far more matches than `limit`, the endpoint still returns
    exactly `limit` rows, name matches ranked before wecom_userid matches,
    with no duplicate wecom_userids."""
    from app.main import app

    # 30 contacts matching by name, 30 more matching only by wecom_userid —
    # 60 total matches, well over limit=20.
    for i in range(30):
        _insert_contact(db, f"name_match_{i:03d}", f"target person {i:03d}", _TENANT_A)
    for i in range(30):
        _insert_contact(db, f"id_target_{i:03d}", None, _TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=target&limit=20")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 20
    # Name matches must be ranked before wecom_userid matches.
    assert all(d["match_field"] == "name" for d in data)
    ids = [d["wecom_userid"] for d in data]
    assert len(ids) == len(set(ids))


def test_search_contacts_admin_user_name_fills_missing_contact_name_under_bound(client, db) -> None:
    """AdminUser name fills in a *missing* Contact name for the same
    wecom_userid — the actual merge rule (`if name and not existing_name`
    in search_contacts) only ever fills a gap, it never overrides an
    existing Contact name (see the complementary test below).

    A couple of unrelated name-matching filler contacts occupy the
    higher-ranked slots ahead of our target so the per-source bounded
    fetch is genuinely exercised, but stay well under `limit` so the
    target Contact row is NOT accidentally excluded by the SQL LIMIT —
    the whole point of this test is to verify the row survives and is
    still visible to the merge, not merely absent from contact_rows.
    """
    from app.main import app

    for i in range(2):
        _insert_contact(db, f"fillrank_{i:03d}", f"fillrank noise {i:03d}", _TENANT_A)

    # Contact has NO name — this is the "gap" AdminUser should fill.
    _insert_contact(db, "fillrank_target", None, _TENANT_A)
    admin_user = AdminUser(
        id="admin-rnd228-fill", tenant_id=_TENANT_A,
        wecom_user_id="fillrank_target", name="Admin Fill Name",
    )
    db.add(admin_user)
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=fillrank&limit=5")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    target = next((d for d in data if d["wecom_userid"] == "fillrank_target"), None)
    assert target is not None, (
        "Contact row must survive the per-source LIMIT for this test to "
        "actually exercise the merge fallback, not just its absence"
    )
    assert target["display_name"] == "Admin Fill Name"


def test_search_contacts_existing_contact_name_is_preserved_over_admin_user(client, db) -> None:
    """The complementary case: when a Contact row already has a non-empty
    name, an AdminUser row for the same wecom_userid must NOT override it.
    AdminUser only ever fills a missing Contact name — it never takes
    precedence over an existing one."""
    from app.main import app

    _insert_contact(db, "preserve_target", "keepme Contact Name", _TENANT_A)
    admin_user = AdminUser(
        id="admin-rnd228-preserve", tenant_id=_TENANT_A,
        wecom_user_id="preserve_target", name="keepme Admin Name",
    )
    db.add(admin_user)
    db.commit()

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=keepme&limit=20")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    target = next((d for d in data if d["wecom_userid"] == "preserve_target"), None)
    assert target is not None
    assert target["display_name"] == "keepme Contact Name"


def test_search_contacts_per_source_query_is_bounded_at_sql_level(client, db, monkeypatch) -> None:
    """Structural guarantee: the Contact-table query never returns more
    than `limit` rows to Python — i.e. .limit(limit) is applied at the SQL
    level before .all(), not a full fetch followed by a Python-side slice.
    Without this, a large tenant's total matching row count would flow
    into Python before truncation, growing memory/DB cost with match
    count instead of staying bounded near `limit`."""
    from app.main import app

    for i in range(50):
        _insert_contact(db, f"bulk_{i:03d}", f"target bulk {i:03d}", _TENANT_A)

    captured_contact_row_counts: list[int] = []
    original_all = ORMQuery.all

    def _tracking_all(self):
        rows = original_all(self)
        descs = self.column_descriptions
        if descs and descs[0].get("entity") is Contact:
            captured_contact_row_counts.append(len(rows))
        return rows

    monkeypatch.setattr(ORMQuery, "all", _tracking_all)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/contacts?q=target&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(resp.json()) == 10
    assert captured_contact_row_counts, "expected the Contact query to be observed"
    assert all(count <= 10 for count in captured_contact_row_counts)


def test_search_messages_staff_resolution_uses_shared_helper(client, db) -> None:
    """search_messages' entity/staff resolution must stay behavior-
    equivalent after switching from the removed _build_staff_ids to the
    shared conversation_membership._collect_staff_ids: an AdminUser seat
    that also appears as a message participant is still classified staff
    for entity navigation."""
    from app.main import app

    admin_user = AdminUser(
        id="admin-rnd228-2", tenant_id=_TENANT_A, wecom_user_id="seat_a", name="Seat A",
    )
    db.add(admin_user)
    db.commit()

    msg = _insert_message(
        db, msgtype="text", sender="seat_a",
        tenant_id=_TENANT_A, content_text="hello keyword", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_a", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["entity_id"] == "seat_a"
    assert r["entity_type"] == "staff"


def test_search_messages_display_name_uses_shared_helper(client, db) -> None:
    """search_messages' display-name resolution must stay behavior-
    equivalent after switching from the removed _build_display_name_map to
    the shared conversation_membership._load_display_names_for_ids."""
    from app.main import app

    _insert_contact(db, "staff_disp", "共享助手显示名", _TENANT_A)

    msg = _insert_message(
        db, msgtype="text", sender="staff_disp",
        tenant_id=_TENANT_A, content_text="hello keyword", msgtime=100,
    )
    _insert_recipient(db, msg.id, "contact_disp", tenant_id=_TENANT_A)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/search/messages?q=keyword")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["sender_display_name"] == "共享助手显示名"
