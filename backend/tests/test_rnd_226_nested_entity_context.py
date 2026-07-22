"""
RND-226 — nested media must propagate conversation entity context.

Root cause (see the ticket): the timeline route resolves a conversation
with the entity context mode/staff_id/contact_id/conversation_type and
already appends that context to every top-level media_url/
media_access_url it emits. Nested mixed/chatrecord media, however, did
NOT carry that context:

  1. _build_nested_media_descriptor / _enrich_nested_media_fields never
     receive entity context, so a nested node's access_url had no entity
     context on it.
  2. get_nested_message_media / get_nested_message_media_access did not
     accept mode/staff_id/contact_id/conversation_type at all.
  3. _resolve_authorized_nested_media called _fetch_conversation_messages
     WITHOUT mode/entity_id and did no conversation_type consistency
     check.

RND-226 fix subtlety (this is what the first review caught): the
conversation_type stamped onto each media URL MUST be PER-MESSAGE — the
message's own roomid ("group" if it has a real roomid, else "direct") —
NOT the timeline's bucket-level conversation_type ("group if ANY message
in the bucket has a roomid"). In a collision bucket that contains BOTH a
direct message and a group message, a bucket-level "group" would be
stamped onto the direct message's nested URL, and the nested resolver
(which validates conversation_type against THAT message's own roomid)
would correctly reject it as a mismatch (HTTP 400, "Image failed to
load"). The fix derives conversation_type per-message so a direct image's
URL carries conversation_type=direct and a group video's carries
conversation_type=group — both of which the resolver accepts.

In a genuine direct/group conversation-id COLLISION (a group room whose
roomid literally equals a direct pair's canonical id), the timeline must
now emit BOTH sides' nested media with correct per-message context, and
following those URLs (access AND bytes) must succeed for both sides.

These tests are the regression suite: they assert per-message context
and that BOTH the direct-side and group-side nested URLs are reachable
on a collision timeline, then exercise the nested routes' own validation
(ambiguous-without-context, conversation_type mismatch, invalid mode,
valid positive path).

Run (from backend/):
    pytest tests/test_rnd_226_nested_entity_context.py -v
"""


from __future__ import annotations

from typing import Optional

from tests.test_media_access_descriptor import (
    _authed,
    client,  # noqa: F401 -- pytest fixture, imported so it is discovered
)
from tests.test_nested_media_access import (
    _nested_access_url,
    _node,
    _structured_content,
)
from tests.test_reachability_audit import (
    _TENANT_A,
    _insert_message,
    _insert_recipient,
    db,  # noqa: F401 -- pytest fixture, imported so it is discovered
)

# Canonical id for the direct pair (contact_bob, staff_alice). The
# derivation sorts the two userids, so this is stable regardless of
# sender/recipient direction (matches test_http_contract.py's DIRECT_CID).
_DIRECT_CID = "direct__contact_bob___staff_alice"


# ---------------------------------------------------------------------------
# Unit-level RED: the descriptor builder must accept + append entity context.
# ---------------------------------------------------------------------------


def test_descriptor_appends_entity_context_to_access_urls(tmp_path, monkeypatch) -> None:
    """_build_nested_media_descriptor must accept media_context_qs and
    append it to both access_url and thumbnail_access_url (only when
    non-empty). Un-fixed code raises TypeError (no such parameter)."""
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"jpeg")
    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A,
        download_status="downloaded", storage_backend="local", storage_ref=str(img),
        local_path=str(img), file_size=8,
    )

    qs = "?mode=staff&staff_id=staff_alice&conversation_type=group"
    descriptor = _build_nested_media_descriptor(
        "image", media_file, "conv-1", "msg-1", "0", media_context_qs=qs
    )
    assert descriptor["access_url"] == (
        "/api/conversations/conv-1/messages/msg-1/nested-media/0/access" + qs
    )


def test_descriptor_without_context_is_unchanged() -> None:
    """Empty media_context_qs must leave access_url byte-identical to the
    pre-RND-226 shape (backward compatibility for context-free requests)."""
    from app.db.models import MediaFile
    from app.routers.conversations import _build_nested_media_descriptor

    media_file = MediaFile(
        sdkfileid="sdk-1", archive_message_id=1, tenant_id=_TENANT_A,
        download_status="pending",
    )
    descriptor = _build_nested_media_descriptor(
        "image", media_file, "conv-1", "msg-1", "0", media_context_qs=""
    )
    # pending -> not downloaded -> access_url stays None either way; the
    # point here is only that the empty-context call signature is accepted.
    assert descriptor["access_url"] is None


# ---------------------------------------------------------------------------
# Collision fixtures — a group room whose roomid == a direct pair CID.
# ---------------------------------------------------------------------------


def _seed_nested_collision(db, tmp_path, monkeypatch):
    """Seed a genuine direct/group collision where BOTH sides are nested
    (mixed) media messages, so the nested routes are exercised. staff_alice
    participates in both sides (sender), contact_bob is the recipient on
    both — mirrors test_http_contract.py::TestMediaEntityContext."""
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    dm_img = tmp_path / "dm.jpg"
    dm_img.write_bytes(b"\xff\xd8\xff" + b"direct")
    gm_img = tmp_path / "gm.jpg"
    gm_img.write_bytes(b"\xff\xd8\xff" + b"group")

    # Direct side: sender staff_alice, recipient contact_bob, no roomid.
    direct = _insert_message(
        db, msgid="dm-nested", msgtype="mixed", sender="staff_alice", msgtime=1000,
        structured_content=_structured_content(
            [_node("0", "image", media={"has_reference": True})],
            [{"path": "0", "type": "image", "sdkfileid": "sdk-dm-nested"}],
        ),
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, direct.id, "contact_bob")

    # Group side: roomid literally equals the direct pair's canonical id.
    group = _insert_message(
        db, msgid="gm-nested", msgtype="mixed", sender="staff_alice",
        roomid=_DIRECT_CID, msgtime=2000,
        structured_content=_structured_content(
            [_node("0", "image", media={"has_reference": True})],
            [{"path": "0", "type": "image", "sdkfileid": "sdk-gm-nested"}],
        ),
        tenant_id=_TENANT_A,
    )
    _insert_recipient(db, group.id, "contact_bob")

    for mid, sdk, path in [
        (direct.id, "sdk-dm-nested", dm_img),
        (group.id, "sdk-gm-nested", gm_img),
    ]:
        db.add(MediaFile(
            tenant_id=_TENANT_A, sdkfileid=sdk, archive_message_id=mid,
            download_status="downloaded", file_type="image",
            local_path=str(path), storage_backend="local", storage_ref=str(path),
            mime_type="image/jpeg", file_size=9,
        ))
    db.commit()
    return direct, group


# ---------------------------------------------------------------------------
# Full-route RED: timeline nested descriptor must carry entity context.
# ---------------------------------------------------------------------------


def _parse_qs_value(url: str, key: str) -> Optional[str]:
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(url).query)
    values = qs.get(key)
    return values[0] if values else None


def test_timeline_nested_access_url_carries_per_message_entity_context(client, db, tmp_path, monkeypatch) -> None:
    """Every nested node descriptor's access_url must carry the entity
    context (mode/staff_id) the timeline was resolved with, AND a
    conversation_type that matches THAT message's own type — NOT a single
    bucket-level value. In the collision bucket, the direct image must get
    conversation_type=direct and the group image conversation_type=group.
    (The first review caught the bucket-level bug: every URL used to carry
    the bucket's 'group', so the direct-side URL was rejected on follow.)"""
    from app.main import app

    _seed_nested_collision(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            f"/api/conversations/{_DIRECT_CID}/messages"
            "?mode=staff&staff_id=staff_alice&conversation_type=group&limit=10"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert msgs, "collision timeline returned no messages"

    seen_types: set = set()
    for m in msgs:
        sc = m.get("structured_content")
        if not sc:
            continue
        for item in sc["fields"].get("items", []):
            media = item.get("media") or {}
            url = media.get("access_url")
            if not url:
                continue
            assert "mode=staff" in url, f"missing entity context in {url}"
            assert "staff_id=staff_alice" in url, f"missing staff_id in {url}"
            ct = _parse_qs_value(url, "conversation_type")
            assert ct in ("direct", "group"), f"missing/invalid per-message conversation_type in {url}"
            # Each message's per-message type must agree with whether IT
            # has a roomid (the exact rule the resolver validates against).
            assert ct == ("group" if m.get("roomid") else "direct"), (
                f"per-message conversation_type {ct} disagrees with message "
                f"roomid={m.get('roomid')!r} for {url}"
            )
            seen_types.add(ct)

    # The collision bucket MUST contain BOTH sides, otherwise this test is
    # not actually exercising the bug.
    assert seen_types == {"direct", "group"}, f"collision bucket did not emit both sides: {seen_types}"


def test_collision_direct_side_nested_access_and_bytes_succeed(client, db, tmp_path, monkeypatch) -> None:
    """The reported blocker: a direct-side nested image in a collision
    timeline must be reachable. The timeline emits an access_url with
    conversation_type=direct (per-message), and BOTH the access route and
    the bytes route must return 200 following it. Un-fixed code stamped
    conversation_type=group onto the direct image's URL, so the resolver
    rejected it with 400 ('Image failed to load')."""
    from app.main import app

    direct, group = _seed_nested_collision(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            f"/api/conversations/{_DIRECT_CID}/messages"
            "?mode=staff&staff_id=staff_alice&conversation_type=group&limit=10"
        )
        assert resp.status_code == 200, resp.text
        msgs = resp.json()["messages"]

        direct_url: Optional[str] = None
        for m in msgs:
            if m.get("msgid") == direct.msgid:
                direct_url = (m["structured_content"]["fields"]["items"][0]
                              .get("media", {}).get("access_url"))
        assert direct_url, "direct-side nested access_url not found in timeline"

        # access descriptor
        access = client.get(direct_url)
        assert access.status_code == 200, f"direct access 400: {access.text}"
        body = access.json()
        assert body["access_type"] == "proxy"
        # bytes route (follow the proxy url, preserving the same context)
        bytes_resp = client.get(body["url"])
        assert bytes_resp.status_code == 200, f"direct bytes not 200: {bytes_resp.status_code}"
        assert bytes_resp.content.startswith(b"\xff\xd8\xff"), "direct bytes wrong content"
    finally:
        app.dependency_overrides.clear()


def test_collision_group_side_nested_access_and_bytes_succeed(client, db, tmp_path, monkeypatch) -> None:
    """Mirror of the direct-side test for the group side, to confirm BOTH
    sides are reachable on the same collision timeline."""
    from app.main import app

    direct, group = _seed_nested_collision(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            f"/api/conversations/{_DIRECT_CID}/messages"
            "?mode=staff&staff_id=staff_alice&conversation_type=group&limit=10"
        )
        assert resp.status_code == 200, resp.text
        msgs = resp.json()["messages"]

        group_url: Optional[str] = None
        for m in msgs:
            if m.get("msgid") == group.msgid:
                group_url = (m["structured_content"]["fields"]["items"][0]
                             .get("media", {}).get("access_url"))
        assert group_url, "group-side nested access_url not found in timeline"

        access = client.get(group_url)
        assert access.status_code == 200, f"group access not 200: {access.text}"
        bytes_resp = client.get(access.json()["url"])
        assert bytes_resp.status_code == 200, f"group bytes not 200: {bytes_resp.status_code}"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Full-route RED: nested access route must accept + honor entity context.
# ---------------------------------------------------------------------------


def test_nested_access_with_entity_context_resolves_in_collision(client, db, tmp_path, monkeypatch) -> None:
    """With entity context supplied, a nested-media access request on a
    collision CID must resolve to the correct side (200), the same way the
    timeline does. Un-fixed code ignores the context and hits the
    entity-blind ID-only path, which raises 400 for a genuine collision
    (RED: got 400, want 200)."""
    from app.main import app

    _seed_nested_collision(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            _nested_access_url(_DIRECT_CID, "gm-nested", "0")
            + "?mode=staff&staff_id=staff_alice&conversation_type=group"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["access_type"] == "proxy"


def test_nested_access_without_context_is_ambiguous_in_collision(client, db, tmp_path, monkeypatch) -> None:
    """Safety guard: an entity-context-free nested request on a genuine
    collision must never silently resolve to an arbitrary side — it is a
    400, same policy the timeline enforces. (Holds before and after the
    fix; documents the security property.)"""
    from app.main import app

    _seed_nested_collision(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(_nested_access_url(_DIRECT_CID, "gm-nested", "0"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400, f"expected 400 ambiguous, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Full-route RED: validation + conversation_type consistency on the nested
# routes must match the timeline / top-level media routes exactly.
# ---------------------------------------------------------------------------


def _seed_plain_group_nested(db, tmp_path, monkeypatch, roomid="room-nested-226"):
    from app.db.models import MediaFile

    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
    img = tmp_path / "plain.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"plain")
    msg = _insert_message(
        db, msgid="plain-nested", msgtype="mixed", sender="staff_alice",
        roomid=roomid, msgtime=100,
        structured_content=_structured_content(
            [_node("0", "image", media={"has_reference": True})],
            [{"path": "0", "type": "image", "sdkfileid": "sdk-plain-nested"}],
        ),
        tenant_id=_TENANT_A,
    )
    db.add(MediaFile(
        tenant_id=_TENANT_A, sdkfileid="sdk-plain-nested", archive_message_id=msg.id,
        download_status="downloaded", file_type="image", local_path=str(img),
        storage_backend="local", storage_ref=str(img), mime_type="image/jpeg", file_size=8,
    ))
    db.commit()
    return msg


def test_nested_access_conversation_type_mismatch_is_rejected(client, db, tmp_path, monkeypatch) -> None:
    """A conversation_type that contradicts the resolved bucket's actual
    type must be rejected (400), mirroring the timeline route (~1728-1736)
    and _resolve_authorized_media. This group room resolves to 'group', so
    conversation_type=direct is a mismatch. Un-fixed code ignores the
    param and returns 200 (RED)."""
    from app.main import app

    _seed_plain_group_nested(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            _nested_access_url("room-nested-226", "plain-nested", "0")
            + "?conversation_type=direct"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400, f"expected 400 mismatch, got {resp.status_code}: {resp.text}"


def test_nested_access_invalid_mode_is_rejected(client, db, tmp_path, monkeypatch) -> None:
    """An invalid mode must be rejected (400), identical validation to the
    timeline and top-level media routes. Un-fixed code ignores the param
    and returns 200 (RED)."""
    from app.main import app

    _seed_plain_group_nested(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            _nested_access_url("room-nested-226", "plain-nested", "0") + "?mode=bogus"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400, f"expected 400 invalid mode, got {resp.status_code}: {resp.text}"


def test_nested_access_valid_entity_context_on_plain_group_succeeds(client, db, tmp_path, monkeypatch) -> None:
    """Positive path: a well-formed entity context that agrees with the
    resolved bucket still succeeds (200) — the new validation must not
    break the ordinary case."""
    from app.main import app

    _seed_plain_group_nested(db, tmp_path, monkeypatch)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(
            _nested_access_url("room-nested-226", "plain-nested", "0")
            + "?mode=staff&staff_id=staff_alice&conversation_type=group"
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
