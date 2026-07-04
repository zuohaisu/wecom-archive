"""
Deterministic display-name resolution shared by the contact/conversation APIs (RND-129).

Production backfill of contacts.name is currently blocked by WeCom trusted
domain/IP configuration (RND-130), so contacts.name is mostly empty today.
These resolvers must therefore never return a blank primary label: when no
real name is known they fall back to a stable, readable label derived from
the raw WeCom ID, while the raw ID itself always stays available to callers
as a separate field for secondary/debug display.

Never fabricates a real person's name or a group's name — only real
metadata (contacts.name / a future room name) is ever used as the primary
label; everything else is a generic, clearly-labelled fallback.
"""

from __future__ import annotations

from typing import Optional


def _short_suffix(raw_id: str, length: int = 6) -> str:
    """Last `length` chars of raw_id — short enough for a compact fallback label."""
    if not raw_id:
        return ""
    return raw_id[-length:] if len(raw_id) > length else raw_id


def resolve_person_display_name(raw_id: Optional[str], name: Optional[str] = None) -> str:
    """Display name for a contact or staff/monitored account.

    A non-blank resolved name (typically contacts.name) always wins.
    Otherwise falls back to the raw ID itself: still unique, stable, and
    exactly what the rest of the UI needs to look the entity back up.
    Never returns a blank string.
    """
    clean_name = name.strip() if isinstance(name, str) else None
    if clean_name:
        return clean_name
    return raw_id or "Unknown contact"


def resolve_room_display_name(roomid: Optional[str], room_name: Optional[str] = None) -> str:
    """Display name for a group conversation.

    Room metadata sync is not implemented yet (RND-129 non-goal), so
    room_name is currently always None and this falls through to a
    readable "Group chat · <suffix>" label rather than the bare roomid.
    Never returns a blank string.
    """
    clean_name = room_name.strip() if isinstance(room_name, str) else None
    if clean_name:
        return clean_name
    if not roomid:
        return "Group chat"
    return f"Group chat · {_short_suffix(roomid)}"
