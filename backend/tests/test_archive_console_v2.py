"""
Tests for the Archive Console v2 redesign (Claude Design import — this
work has no Linear ticket number, so test names are descriptive rather
than RND-numbered like most of this suite).

Covers what's new:
  - GET /api/conversations/{conversation_id}/detail: schema shape, tenant
    scoping, participant-inference reuse (via the same
    _build_conversation_list aggregation GET /api/conversations uses),
    404 for an unknown conversation.
  - Frontend (executed under Node, same technique as
    test_admin_group_participant_overflow.py): graded media-unavailable
    placeholder states, the audit-mode msgtype-badge/audit-line toggle,
    the text/voice-only bubble-wrap rule (bare media / own-box cards
    otherwise), the client-side conversation-type filter, and the shared
    structured-card header.

Run (from backend/):
    pytest tests/test_archive_console_v2.py -v
"""

from __future__ import annotations

import json
import re
import shutil
from unittest.mock import MagicMock

import pytest

from app.db.models import ArchiveMessage, ArchiveMessageRecipient
from app.routers.web import _MESSAGE_TYPE_REGISTRY_ENTRIES_JSON
from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source
from tests.test_reachability_audit import (  # noqa: F401 -- db is a pytest fixture
    _TENANT_A,
    _TENANT_B,
    _insert_message,
    db,
)

_REVIEW_CONSOLE_JS = review_console_js_source()
NODE = shutil.which("node")


def _extract(pattern: str, label: str) -> str:
    match = re.search(pattern, _REVIEW_CONSOLE_JS, re.S)
    assert match is not None, f"{label} not found in _REVIEW_CONSOLE_JS"
    return match.group(0)


def _insert_recipient(db, message_id: int, receiver_userid: str, tenant_id: str = _TENANT_A):
    r = ArchiveMessageRecipient(
        message_id=message_id,
        receiver_userid=receiver_userid,
        receiver_type="user",
        tenant_id=tenant_id,
    )
    db.add(r)
    db.commit()
    return r


# ---------------------------------------------------------------------------
# Backend: GET /api/conversations/{conversation_id}/detail
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _authed(app, db_session, tenant_id):
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: (MagicMock(), tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def test_conversation_detail_group_returns_stats_and_participants(client, db) -> None:
    from app.main import app

    m1 = _insert_message(
        db,
        msgtype="text",
        content_text="hi",
        sender="staff_alice",
        roomid="room-1",
        msgtime=1000,
        decrypt_status="success",
    )
    _insert_recipient(db, m1.id, "contact_bob")
    m2 = _insert_message(
        db,
        msgtype="text",
        content_text="still decrypting",
        sender="contact_bob",
        roomid="room-1",
        msgtime=2000,
        decrypt_status="pending",
    )
    _insert_recipient(db, m2.id, "staff_alice")

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-1/detail")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == "room-1"
    assert body["message_count"] == 2
    # 1 of 2 messages decrypted successfully.
    assert body["decrypted_percent"] == 50.0
    roles = {p["id"]: p["role"] for p in body["participants"]}
    assert roles.get("staff_alice") == "staff"
    assert roles.get("contact_bob") == "contact"


def test_conversation_detail_unknown_conversation_404s(client, db) -> None:
    from app.main import app

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/does-not-exist/detail")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_conversation_detail_is_tenant_scoped(client, db) -> None:
    """A conversation that only exists for tenant B must 404 for tenant A —
    never leak tenant B's messages/participants into tenant A's panel."""
    from app.main import app

    m = _insert_message(
        db,
        msgtype="text",
        content_text="tenant b only",
        sender="staff_x",
        roomid="room-tenant-b",
        tenant_id=_TENANT_B,
        msgtime=1000,
    )
    _insert_recipient(db, m.id, "contact_y", tenant_id=_TENANT_B)

    _authed(app, db, _TENANT_A)
    try:
        resp = client.get("/api/conversations/room-tenant-b/detail")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_rnd240_detail_aggregation_matches_full_row_computation(client, db) -> None:
    """RND-240: get_conversation_detail was rewritten to resolve membership
    as bare ids + a load_only projection instead of materializing every
    full ArchiveMessage row, to avoid the large raw_encrypted_payload/
    decrypted_payload/structured_content columns on conversations with
    thousands of messages. This proves the new path's response is
    field-for-field identical to what the OLD full-row computation would
    have produced, on a conversation large enough (300 messages, mixed
    decrypt_status, two distinct participants) that the two code paths'
    query shapes genuinely differ -- not just a small fixture that happens
    to agree either way."""
    from app.conversation_membership import (
        _fetch_conversation_messages,
        _load_recipients_map,
        _load_display_names_for_ids,
        _staff_ids_for_participants,
    )
    from app.main import app
    from app.services.listing_service import _build_conversation_list

    room = "rnd240-large-room"
    n = 300
    rows = []
    for i in range(n):
        sender = "staff_alice" if i % 2 == 0 else "contact_bob"
        rows.append(
            ArchiveMessage(
                msgid=f"rnd240-{i}",
                seq=i,
                publickey_ver=1,
                encrypt_random_key="x",
                encrypt_chat_msg="y",
                decrypt_status="success" if i % 3 else "pending",
                content_text=f"message body {i}",
                msgtype="text",
                sender=sender,
                roomid=room,
                msgtime=1000 + i,
                tenant_id=_TENANT_A,
            )
        )
    db.bulk_save_objects(rows, return_defaults=True)
    db.commit()

    msg_ids = [
        row[0]
        for row in db.query(ArchiveMessage.id).filter(ArchiveMessage.roomid == room).all()
    ]
    recipient_rows = [
        ArchiveMessageRecipient(
            message_id=mid,
            receiver_userid="contact_bob" if idx % 2 == 0 else "staff_alice",
            receiver_type="user",
            tenant_id=_TENANT_A,
        )
        for idx, mid in enumerate(msg_ids)
    ]
    db.bulk_save_objects(recipient_rows)
    db.commit()

    # New path: the actual HTTP endpoint.
    _authed(app, db, _TENANT_A)
    try:
        resp = client.get(f"/api/conversations/{room}/detail")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    new_body = resp.json()

    # Old path: byte-for-byte reimplementation of the pre-RND-240 body,
    # using the full-row _fetch_conversation_messages this endpoint used
    # to call directly.
    old_messages = _fetch_conversation_messages(db, room, _TENANT_A)
    old_recipients_map = _load_recipients_map(db, _TENANT_A, [m.id for m in old_messages])
    old_participant_ids: set[str] = {m.sender for m in old_messages if m.sender}
    for recipient_ids in old_recipients_map.values():
        old_participant_ids.update(recipient_ids)
    old_display_names = _load_display_names_for_ids(db, _TENANT_A, old_participant_ids)
    old_staff_ids = _staff_ids_for_participants(db, _TENANT_A, old_participant_ids)
    old_buckets = _build_conversation_list(
        old_messages, old_recipients_map, old_display_names, old_staff_ids
    )
    old_bucket = next(
        (b for b in old_buckets if b["conversation_id"] == room), old_buckets[0]
    )
    old_participants = [
        {"id": sid, "raw_id": sid, "display_name": name, "role": "staff"}
        for sid, name in zip(
            old_bucket["monitored_account_ids"], old_bucket["monitored_account_display_names"]
        )
    ] + [
        {"id": cid, "raw_id": cid, "display_name": name, "role": "contact"}
        for cid, name in zip(old_bucket["contact_ids"], old_bucket["contact_display_names"])
    ]
    old_decrypted_count = sum(1 for m in old_messages if m.decrypt_status == "success")
    old_body = {
        "conversation_id": room,
        "message_count": len(old_messages),
        "decrypted_percent": round(old_decrypted_count * 100.0 / len(old_messages), 1),
        "participants": old_participants,
    }

    assert new_body == old_body


# ---------------------------------------------------------------------------
# Frontend: graded media-unavailable placeholder
# ---------------------------------------------------------------------------


def _graded_placeholder_bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('zh-CN');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        _extract(
            r"var MEDIA_STATUS_DOT=\{.*?\nfunction renderGradedMediaPlaceholder\(typeLabel,status\)\{.*?\n\}",
            "renderGradedMediaPlaceholder",
        ),
    ]
    return "\n".join(parts)


def _render_graded_placeholder(type_label: str, status: str) -> str:
    assert NODE, "node executable not found"
    harness = (
        f"{_graded_placeholder_bundle()}\n"
        f"process.stdout.write(renderGradedMediaPlaceholder({json.dumps(type_label)},{json.dumps(status)}));"
    )
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def test_graded_placeholder_distinguishes_not_downloaded_failed_unsupported() -> None:
    """Message Types spec: each status gets DISTINCT explanatory copy, not
    one uniform 'label · status' line -- not_downloaded/failed/unsupported
    must never render identical reason text."""
    not_downloaded = _render_graded_placeholder("语音消息", "not_downloaded")
    failed = _render_graded_placeholder("语音消息", "failed")
    unsupported = _render_graded_placeholder("语音消息", "unsupported")

    assert "语音消息 · 未下载" in not_downloaded
    assert "语音消息 · 下载失败" in failed
    assert "语音消息 · 不支持" in unsupported

    reasons = {
        re.search(r'<div class="media-placeholder-reason">(.*?)</div>', html).group(1)
        for html in (not_downloaded, failed, unsupported)
    }
    assert len(reasons) == 3, f"expected 3 distinct reason strings, got {reasons}"


def test_graded_placeholder_always_pairs_type_and_status() -> None:
    html = _render_graded_placeholder("图片消息", "unknown")
    assert "图片消息" in html and "状态未知" in html
    assert "media-placeholder-dot" in html


# ---------------------------------------------------------------------------
# Frontend: timelineRowHtml — bubble-wrap rule (Message Types visual refresh)
# ---------------------------------------------------------------------------


def _timeline_row_bundle() -> str:
    parts = [
        _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core"),
        "I18N.setLocale('zh-CN');",
        _extract(r"function esc\(s\)\{.*?\n\}", "esc()"),
        _extract(r"function fmtTime\(ms\)\{.*?\n\}", "fmtTime()"),
        _extract(r"function pad\(n\)\{.*?\}", "pad()"),
        _extract(r"var MEDIA_LABELS=\{.*?\};", "MEDIA_LABELS"),
        _extract(r"var MEDIA_STATUS_LABELS=\{.*?\};", "MEDIA_STATUS_LABELS"),
        _extract(
            r"var MEDIA_STATUS_DOT=\{.*?\nfunction renderGradedMediaPlaceholder\(typeLabel,status\)\{.*?\n\}",
            "renderGradedMediaPlaceholder",
        ),
        f"var RND216_MTR_ENTRIES = {_MESSAGE_TYPE_REGISTRY_ENTRIES_JSON};",
        _extract(r"var MessageTypeRegistry=\(function\(\)\{.*?\n\}\)\(\);", "MessageTypeRegistry"),
        _extract(r"function isSafeUrl\(u\)\{.*?\n\}", "isSafeUrl()"),
        _extract(r"function hostnameOf\(u\)\{.*?\n\}", "hostnameOf()"),
        _extract(r"function fmtCoord\(n\)\{.*?\}", "fmtCoord()"),
        _extract(
            r"var CARD_DOT_COLORS=\{.*?\nfunction structuredCardHeader\(labelKey,rawType,dotColor\)\{.*?\n\}",
            "structuredCardHeader",
        ),
        _extract(r"function renderStructuredFallback\(m\)\{.*?\n\}", "renderStructuredFallback()"),
        _extract(r"function renderTodoCard\(m\)\{.*?\n\}", "renderTodoCard()"),
        "function renderLinkCard(m){return '';}function renderLocationCard(m){return '';}"
        "function renderMarkdownCard(m){return '';}function renderNewsCard(m){return '';}"
        "function renderMiniprogramCard(m){return '';}function renderVoteCard(m){return '';}"
        "function renderCollectCard(m){return '';}function renderMeetingCard(m){return '';}"
        "function renderScheduleCard(m){return '';}function renderRedpacketCard(m){return '';}"
        "function renderSwitchCorpCard(m){return '';}function renderSystemCard(m){return '';}"
        "function renderCardMessage(m){return '';}function renderAudioArchiveMessage(m){return '';}"
        "function renderAudioDocMessage(m){return '';}function renderSphfeedCard(m){return '';}",
        _extract(r"var STRUCTURED_CARD_RENDERERS=\{.*?\n\};", "STRUCTURED_CARD_RENDERERS"),
        _extract(r"function renderStructuredCard\(m\)\{.*?\n\}", "renderStructuredCard()"),
        _extract(
            r"var MediaAccessCache=\(function\(\)\{.*?\nfunction renderCompositeMessage\(m\)\{.*?\n\}",
            "RND-206 rich-media/composite block",
        ),
        _extract(r"function renderRevokePlaceholder\(m\)\{.*?\n\}", "renderRevokePlaceholder()"),
        _extract(r"function renderMessageBody\(m\)\{.*?\n\}", "renderMessageBody()"),
        _extract(r"function safeRenderMessageBody\(m\)\{.*?\n\}", "safeRenderMessageBody()"),
        _extract(r"function timelineSignature\(msgs\)\{.*?\n\}", "timelineSignature()"),
        _extract(r"function timelineRowHtml\(m\)\{.*?\n\}", "timelineRowHtml()"),
    ]
    return "\n".join(parts)


def _render_row(msg: dict, *, audit_mode: bool = False, selected_msg_id=None) -> str:
    assert NODE, "node executable not found"
    harness = f"""
{_timeline_row_bundle()}
var mode='staff';
var selEntityId=null;
var auditMode={json.dumps(audit_mode)};
var selectedMsgId={json.dumps(selected_msg_id)};
process.stdout.write(timelineRowHtml({json.dumps(msg)}));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def _base_msg(**overrides) -> dict:
    msg = {
        "msgid": "m-1",
        "sender": "contact_zhangsan",
        "sender_display_name": "Zhang San",
        "sender_raw_id": "contact_zhangsan",
        "recipients": [],
        "recipient_display_names": [],
        "msgtime": 1751702400000,
        "msgtype": "text",
        "content_text": "hello",
        "roomid": None,
        "decrypt_status": "success",
        "media_type": "text",
        "media_status": None,
        "media_access_url": None,
        "normalized_type": "text",
        "support_status": "supported",
        "renderer_strategy": "text_body",
        "display_label_key": "messageType.text",
    }
    msg.update(overrides)
    return msg


def test_text_message_keeps_bubble_wrapper() -> None:
    html = _render_row(_base_msg())
    assert 'class="tl-bubble' in html


def test_available_image_renders_bare_no_bubble_wrapper() -> None:
    html = _render_row(
        _base_msg(
            msgtype="image",
            content_text=None,
            media_type="image",
            media_status="available",
            media_access_url="/media/access/1",
            normalized_type="image",
            renderer_strategy="media_preview",
        )
    )
    assert 'class="tl-bubble' not in html
    assert 'class="tl-bare"' in html


def test_todo_card_renders_bare_no_bubble_wrapper() -> None:
    """Structured/business cards supply their own box -- wrapping them in
    .tl-bubble too produced the double-boxed look this fix removes."""
    html = _render_row(
        _base_msg(
            msgtype="todo",
            content_text=None,
            # RND-197: structured/interactive types report media_type=
            # "structured" (see app.media_classification.classify_media) --
            # distinct from "unknown" (an unregistered msgtype).
            media_type="structured",
            normalized_type="todo",
            renderer_strategy="structured_card",
            structured_content={"fields": {"title": "跟进", "content": "详情"}},
        )
    )
    assert 'class="tl-bubble' not in html
    assert 'class="tl-bare"' in html
    assert "structured-card-todo" in html


def test_unavailable_voice_renders_bare_graded_placeholder_not_bubble() -> None:
    """Only AVAILABLE voice keeps the bubble (waveform/player fits inside
    one); an unavailable voice's graded placeholder is its own box."""
    html = _render_row(
        _base_msg(
            msgid="m-voice",
            msgtype="voice",
            content_text=None,
            media_type="voice",
            media_status="not_downloaded",
            normalized_type="voice",
            renderer_strategy="media_preview",
        )
    )
    assert 'class="tl-bubble' not in html
    assert "media-placeholder-reason" in html


# ---------------------------------------------------------------------------
# Frontend: audit-mode toggle (msgtype badge + audit line gating)
# ---------------------------------------------------------------------------


def test_audit_mode_off_hides_msgtype_badge_and_audit_line() -> None:
    html = _render_row(_base_msg(msgtype="todo", normalized_type="todo"), audit_mode=False)
    assert "tl-audit-line" not in html
    assert '>todo<' not in html


def test_audit_mode_on_shows_msgtype_badge_and_audit_line() -> None:
    html = _render_row(_base_msg(msgtype="todo", normalized_type="todo"), audit_mode=True)
    assert "tl-audit-line" in html
    assert "m-1 · text · todo" in html or "todo" in html


def test_audit_mode_never_shows_badge_for_plain_text() -> None:
    """text messages never get the msgtype badge, audit mode or not --
    matches the pre-existing 'only show non-text types' rule."""
    html_on = _render_row(_base_msg(), audit_mode=True)
    assert "tl-audit-line" in html_on  # the audit line itself always appears
    # but no msgtype badge span, since msgtype==='text'
    assert '<span class="badge badge-count" style="font-size:.67rem">text</span>' not in html_on


# ---------------------------------------------------------------------------
# Frontend: client-side conversation-type filter
# ---------------------------------------------------------------------------


def _run_conv_type_filter(convs: list[dict], filter_value: str) -> list[str]:
    assert NODE, "node executable not found"
    apply_src = _extract(r"function applyConvTypeFilter\(convs\)\{.*?\n\}", "applyConvTypeFilter()")
    harness = f"""
{apply_src}
var convTypeFilter={json.dumps(filter_value)};
var result = applyConvTypeFilter({json.dumps(convs)});
process.stdout.write(JSON.stringify(result.map(function(c){{return c.conversation_id;}})));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def test_conv_type_filter_all_returns_everything() -> None:
    convs = [
        {"conversation_id": "g1", "conversation_type": "group"},
        {"conversation_id": "d1", "conversation_type": "direct"},
    ]
    assert _run_conv_type_filter(convs, "all") == ["g1", "d1"]


def test_conv_type_filter_group_only() -> None:
    convs = [
        {"conversation_id": "g1", "conversation_type": "group"},
        {"conversation_id": "d1", "conversation_type": "direct"},
        {"conversation_id": "g2", "conversation_type": "group"},
    ]
    assert _run_conv_type_filter(convs, "group") == ["g1", "g2"]


def test_conv_type_filter_direct_only() -> None:
    convs = [
        {"conversation_id": "g1", "conversation_type": "group"},
        {"conversation_id": "d1", "conversation_type": "direct"},
    ]
    assert _run_conv_type_filter(convs, "direct") == ["d1"]


# ---------------------------------------------------------------------------
# Frontend: shared structured-card header
# ---------------------------------------------------------------------------


def test_structured_card_header_renders_dot_label_and_raw_msgtype() -> None:
    assert NODE, "node executable not found"
    header_src = _extract(
        r"var CARD_DOT_COLORS=\{.*?\nfunction structuredCardHeader\(labelKey,rawType,dotColor\)\{.*?\n\}",
        "structuredCardHeader",
    )
    i18n_core_src = _extract(r"/\* I18N_CORE_START.*?I18N_CORE_END \*/", "I18N core")
    esc_src = _extract(r"function esc\(s\)\{.*?\n\}", "esc()")
    harness = f"""
{i18n_core_src}
I18N.setLocale('zh-CN');
{esc_src}
{header_src}
process.stdout.write(structuredCardHeader('messageType.todo','todo',CARD_DOT_COLORS.todo));
"""
    result = run_node(harness)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    html = result.stdout
    assert "sc-hd-dot" in html
    assert "sc-hd-raw" in html and ">todo<" in html
    assert "待办消息" in html  # messageType.todo, zh-CN
