"""
Regression guard for the search results page's "用户"/"员工" filter option
derivation (RND-230, QA remediation finding C12).

Two prior versions of `collectParticipants` both broke on group chats:

  v1 sourced "用户" (contact-side) options purely from
  entity_type==='contact' rows. Real backend `_pick_entity_id` prefers
  staff whenever ANY staff participates in a message, so entity_type is
  essentially never 'contact' for ordinary staff<->contact traffic — the
  "用户" popover was empty for the common case.

  v2 patched that by inferring the contact side from `sender` or by
  parsing the direct conversation_id's two participant tokens. That still
  failed for GROUP messages sent BY staff: a group has no "other side" to
  parse out of its conversation_id (which is just the bare roomid), so a
  staff-authored group message revealed no contact at all — even though
  the backend's `user=<contact>` filter genuinely supports narrowing to
  that contact.

The actual fix drops entity_id/entity_type inference entirely:
MessageSearchResult now carries `contact_ids`/`staff_ids`, the message's
full tenant-scoped participant sets (sender + every recipient, split by
is_staff — see app.routers.search's `_derive_conversation_membership`
reuse), and collectParticipants just unions those arrays across ALL
results. This test runs the actual extracted `collectParticipants`/
`participantLabelFor`/`setParticipant` helpers under Node — no DOM, no
Playwright — against fixtures shaped like real API responses.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.main import _SEARCH_PAGE_HTML

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _extract_participant_helpers() -> str:
    start_marker = "function participantLabelFor(r,id){"
    end_marker = "function refreshParticipantCache(){"
    start = _SEARCH_PAGE_HTML.index(start_marker)
    end = _SEARCH_PAGE_HTML.index(end_marker, start)
    src = _SEARCH_PAGE_HTML[start:end]
    assert "function collectParticipants(kind){" in src
    return src


def _run(all_fixture: list) -> dict:
    harness = f"""
var ALL = {json.dumps(all_fixture)};
{_extract_participant_helpers()}
process.stdout.write(JSON.stringify({{
  contact: collectParticipants('contact'),
  staff: collectParticipants('staff')
}}));
"""
    result = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return json.loads(result.stdout)


def test_group_message_authored_by_staff_still_surfaces_its_contacts():
    """The exact C12 group-chat gap: sender is staff, recipients are
    contacts, entity_id/entity_type alone name only the staff side —
    contact_ids must carry the rest."""
    rows = [{
        "sender": "staff_alice", "sender_display_name": "Alice",
        "entity_id": "staff_alice", "entity_type": "staff",
        "conversation_type": "group", "conversation_id": "room1",
        "contact_ids": ["contact_bob", "contact_carol"], "staff_ids": ["staff_alice"],
    }]
    out = _run(rows)
    assert out["contact"] == {"contact_bob": "contact_bob", "contact_carol": "contact_carol"}
    assert out["staff"] == {"staff_alice": "Alice"}


def test_direct_conversation_both_directions_recovered_with_display_names():
    rows = [
        {
            "sender": "staff_alice", "sender_display_name": "Alice",
            "entity_id": "staff_alice", "entity_type": "staff",
            "conversation_type": "direct", "conversation_id": "direct__contact_bob___staff_alice",
            "contact_ids": ["contact_bob"], "staff_ids": ["staff_alice"],
        },
        {
            "sender": "contact_bob", "sender_display_name": "Bob",
            "entity_id": "staff_alice", "entity_type": "staff",
            "conversation_type": "direct", "conversation_id": "direct__contact_bob___staff_alice",
            "contact_ids": ["contact_bob"], "staff_ids": ["staff_alice"],
        },
    ]
    out = _run(rows)
    assert out["contact"] == {"contact_bob": "Bob"}
    assert out["staff"] == {"staff_alice": "Alice"}


def test_room_with_no_staff_still_yields_contact_options():
    rows = [{
        "sender": "contact_dave", "sender_display_name": "Dave",
        "entity_id": "contact_dave", "entity_type": "contact",
        "conversation_type": "group", "conversation_id": "room2",
        "contact_ids": ["contact_dave"], "staff_ids": [],
    }]
    out = _run(rows)
    assert out["contact"] == {"contact_dave": "Dave"}
    assert out["staff"] == {}


def test_a_later_row_without_a_display_name_never_downgrades_an_earlier_good_label():
    """setParticipant must not let a bare-id fallback (this row's sender
    isn't the target id, so no display name is available for it here)
    clobber a real name already found from an earlier row for the same
    id — see setParticipant's docstring."""
    rows = [
        {
            "sender": "staff_alice", "sender_display_name": "Alice",
            "entity_id": "staff_alice", "entity_type": "staff",
            "conversation_type": "direct", "conversation_id": "direct__contact_bob___staff_alice",
            "contact_ids": ["contact_bob"], "staff_ids": ["staff_alice"],
        },
        {
            "sender": "contact_bob", "sender_display_name": "Bob",
            "entity_id": "staff_alice", "entity_type": "staff",
            "conversation_type": "direct", "conversation_id": "direct__contact_bob___staff_alice",
            "contact_ids": ["contact_bob"], "staff_ids": ["staff_alice"],
        },
    ]
    out = _run(rows)
    assert out["staff"]["staff_alice"] == "Alice"


def test_missing_participant_fields_degrade_to_no_options_not_a_crash():
    """Defensive: a row shaped without contact_ids/staff_ids (e.g. an
    older cached response) must not throw — collectParticipants guards
    with `r[field]||[]`."""
    rows = [{
        "sender": "staff_alice", "sender_display_name": "Alice",
        "entity_id": "staff_alice", "entity_type": "staff",
        "conversation_type": "direct", "conversation_id": "direct__contact_bob___staff_alice",
    }]
    out = _run(rows)
    assert out["contact"] == {}
    assert out["staff"] == {}
