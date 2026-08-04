"""External-contact identity normalization, persistence, and lookup helpers.

RND-170 keeps three separate facts separate:

* a customer-level current WeCom nickname;
* each following employee's local remark; and
* a timeline of customer-nickname transitions.

``ExternalContact.name`` predates this model and remains a compatibility
label only. New readers must use the fields and helpers in this module rather
than infer a customer nickname from that legacy column.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import exists, false, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models import (
    ExternalContact,
    ExternalContactFollow,
    ExternalContactNicknameHistory,
)

_SAFE_DISPLAY_MAX_LENGTH = 128


@dataclass(frozen=True)
class IdentitySyncResult:
    """Aggregate-only outcome of applying one externalcontact/get payload."""

    nickname_observed: bool = False
    nickname_changed: bool = False
    follow_relations_changed: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_identity_name(value: object) -> Optional[str]:
    """Return a comparison-safe name, or ``None`` for blank/invisible input.

    WeCom nicknames are user-controlled. NFKC plus whitespace collapsing makes
    ordinary formatting variants compare equal; format/control characters are
    removed so zero-width and bidi-only strings cannot create fake identities
    or nickname-history churn. The raw API value is retained only when it
    has a visible normalized counterpart; blank/invisible input is represented
    as ``None`` rather than persisted as bad raw data.
    """
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    visible = "".join(
        char
        for char in normalized
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    compact = " ".join(visible.split())
    if not compact:
        return None
    # A string made solely of combining/variation marks has no base glyph and
    # renders invisibly too (for example U+034F), even though those marks are
    # not ``Cf``. Keep marks when attached to an actual visible character.
    if not any(unicodedata.category(char)[0] not in {"M", "Z", "C"} for char in compact):
        return None
    return compact


def safe_display_nickname(value: object) -> Optional[str]:
    """Return a bounded user-facing nickname derived from normalized text."""
    normalized = normalize_identity_name(value)
    if normalized is None:
        return None
    if len(normalized) <= _SAFE_DISPLAY_MAX_LENGTH:
        return normalized
    return normalized[: _SAFE_DISPLAY_MAX_LENGTH - 1] + "…"


def _clean_identifier(value: object) -> Optional[str]:
    """Accept a bounded WeCom identifier without retaining invisible noise."""
    normalized = normalize_identity_name(value)
    if normalized is None or len(normalized) > 64:
        return None
    return normalized


def resolve_external_contact_display_name(
    external_userid: Optional[str],
    *,
    remark: Optional[str] = None,
    current_nickname: Optional[str] = None,
) -> str:
    """Resolve the archive title without ever exposing a raw external ID.

    A caller supplies ``remark`` only when it has an explicit follow-user
    context. Without that context the current real nickname wins; if neither
    is usable, a deterministic opaque label remains non-empty and safe.
    """
    safe_remark = safe_display_nickname(remark)
    if safe_remark:
        return safe_remark
    safe_nickname = safe_display_nickname(current_nickname)
    if safe_nickname:
        return safe_nickname
    if external_userid:
        digest = hashlib.sha256(external_userid.encode("utf-8")).hexdigest()[:8]
        return f"External contact · {digest}"
    return "External contact"


def _nickname_observation(detail: object) -> tuple[bool, Optional[str]]:
    """Return whether the authoritative API actually supplied a name field.

    Missing/non-string fields are partial or malformed payloads, not an
    observed blank nickname. A real ``""``/whitespace/invisible string is an
    observation and is intentionally returned so a valid-to-blank transition
    can be recorded.
    """
    if not isinstance(detail, dict):
        return False, None
    external = detail.get("external_contact")
    if not isinstance(external, dict) or "name" not in external:
        return False, None
    raw = external.get("name")
    return (True, raw) if isinstance(raw, str) else (False, None)


def _follow_observations(detail: object) -> Optional[dict[str, tuple[Optional[str], Optional[str]]]]:
    """Parse a complete ``follow_user`` snapshot, or ``None`` if absent.

    Returning ``None`` (rather than an empty mapping) is important: a partial
    response must not deactivate every known employee relationship. An actual
    empty array is a valid full snapshot and does deactivate active relations.
    """
    if not isinstance(detail, dict):
        return None
    follows = detail.get("follow_user")
    if not isinstance(follows, list):
        return None

    result: dict[str, tuple[Optional[str], Optional[str]]] = {}
    malformed = False
    for follow in follows:
        if not isinstance(follow, dict):
            malformed = True
            continue
        follow_userid = _clean_identifier(follow.get("userid"))
        if not follow_userid:
            malformed = True
            continue
        if follow_userid in result:
            continue
        raw_remark = follow.get("remark")
        raw = raw_remark if isinstance(raw_remark, str) else None
        normalized = normalize_identity_name(raw)
        result[follow_userid] = (raw if normalized is not None else None, normalized)
    # Do not mistake a corrupt/partial snapshot for proof every absent
    # relationship was removed. A genuinely empty list remains authoritative.
    return None if malformed else result


def _set_if_changed(instance: object, attribute: str, value: object) -> bool:
    if getattr(instance, attribute) == value:
        return False
    setattr(instance, attribute, value)
    return True


def sync_external_contact_identity(
    session: Session,
    contact: ExternalContact,
    detail: object,
    *,
    observed_at: Optional[datetime] = None,
) -> IdentitySyncResult:
    """Apply current nickname and follow-remark facts from one API response.

    The caller has already upserted the customer-level ``ExternalContact`` and
    owns the outer transaction. This function never mutates legacy ``name``.
    Consecutive equal normalized nicknames do not create history rows or
    timestamp-only writes, making periodic/event refreshes idempotent.
    """
    when = observed_at or _utc_now()
    nickname_observed, raw_nickname = _nickname_observation(detail)
    nickname_changed = False

    if nickname_observed:
        normalized = normalize_identity_name(raw_nickname)
        stored_raw = raw_nickname if normalized is not None else None
        display = safe_display_nickname(raw_nickname)
        if contact.current_nickname_observed_at is None:
            # Baseline observation: there is no earlier known state to put in
            # history yet, including when the first observed state is blank.
            contact.current_nickname_raw = stored_raw
            contact.current_nickname_normalized = normalized
            contact.current_nickname_display = display
            contact.current_nickname_observed_at = when
        elif normalized != contact.current_nickname_normalized:
            session.add(
                ExternalContactNicknameHistory(
                    tenant_id=contact.tenant_id,
                    external_userid=contact.external_userid,
                    old_nickname_raw=contact.current_nickname_raw,
                    old_nickname_normalized=contact.current_nickname_normalized,
                    old_nickname_display=contact.current_nickname_display,
                    new_nickname_raw=stored_raw,
                    new_nickname_normalized=normalized,
                    new_nickname_display=display,
                    observed_at=when,
                )
            )
            contact.current_nickname_raw = stored_raw
            contact.current_nickname_normalized = normalized
            contact.current_nickname_display = display
            contact.current_nickname_observed_at = when
            nickname_changed = True
    follow_changes = _sync_follow_remarks(session, contact, _follow_observations(detail), when)
    return IdentitySyncResult(
        nickname_observed=nickname_observed,
        nickname_changed=nickname_changed,
        follow_relations_changed=follow_changes,
    )


def _sync_follow_remarks(
    session: Session,
    contact: ExternalContact,
    observations: Optional[dict[str, tuple[Optional[str], Optional[str]]]],
    observed_at: datetime,
) -> int:
    if observations is None:
        return 0

    existing = {
        row.follow_userid: row
        for row in session.scalars(
            select(ExternalContactFollow).where(
                ExternalContactFollow.tenant_id == contact.tenant_id,
                ExternalContactFollow.external_userid == contact.external_userid,
            )
        )
    }
    changes = 0
    for follow_userid, (remark_raw, remark_normalized) in observations.items():
        relation = existing.get(follow_userid)
        if relation is None:
            session.add(
                ExternalContactFollow(
                    tenant_id=contact.tenant_id,
                    external_userid=contact.external_userid,
                    follow_userid=follow_userid,
                    remark_raw=remark_raw,
                    remark_normalized=remark_normalized,
                    is_active=True,
                    observed_at=observed_at,
                )
            )
            changes += 1
            continue

        changed = False
        # Raw formatting churn is not a new employee-remark state. Retain the
        # first safe raw observation until the normalized remark actually
        # changes, mirroring nickname idempotency.
        if relation.remark_normalized != remark_normalized:
            relation.remark_raw = remark_raw
            relation.remark_normalized = remark_normalized
            changed = True
        changed |= _set_if_changed(relation, "is_active", True)
        if changed:
            relation.observed_at = observed_at
            changes += 1

    observed_users = set(observations)
    for follow_userid, relation in existing.items():
        if follow_userid not in observed_users and relation.is_active:
            relation.is_active = False
            relation.observed_at = observed_at
            changes += 1
    return changes


def external_contact_display_names(
    session: Session,
    tenant_id: str,
    external_userids: Iterable[str],
    *,
    follow_userid: Optional[str] = None,
) -> dict[str, str]:
    """Return current archive labels for known external contacts only.

    ``follow_userid`` makes an employee context explicit and enables that
    employee's active remark. Without it the helper never picks an arbitrary
    employee remark; it uses the customer nickname or opaque fallback instead.
    A missing new table is tolerated only for pre-migration/test fixtures so
    legacy callers retain their old behavior until schema readiness blocks a
    real production request.
    """
    external_ids = {value for value in external_userids if _clean_identifier(value)}
    if not external_ids:
        return {}
    scoped_follow_userid = _clean_identifier(follow_userid)

    try:
        with session.begin_nested():
            contacts = session.scalars(
                select(ExternalContact).where(
                    ExternalContact.tenant_id == tenant_id,
                    ExternalContact.external_userid.in_(external_ids),
                )
            ).all()
            remarks: dict[str, Optional[str]] = {}
            if scoped_follow_userid:
                remarks = {
                    row.external_userid: row.remark_normalized
                    for row in session.scalars(
                        select(ExternalContactFollow).where(
                            ExternalContactFollow.tenant_id == tenant_id,
                            ExternalContactFollow.external_userid.in_(external_ids),
                            ExternalContactFollow.follow_userid == scoped_follow_userid,
                            ExternalContactFollow.is_active.is_(True),
                        )
                    )
                }
    except DBAPIError:
        return {}

    return {
        contact.external_userid: resolve_external_contact_display_name(
            contact.external_userid,
            remark=remarks.get(contact.external_userid),
            current_nickname=(
                contact.current_nickname_display or contact.current_nickname_raw
            ),
        )
        for contact in contacts
    }


def normalized_search_term(value: object) -> Optional[str]:
    """Normalize a user search term with the same blank rules as nicknames."""
    return normalize_identity_name(value)


def _escape_like_pattern(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def external_contact_search_predicate(tenant_id: str, search_term: object):
    """Build a tenant-scoped predicate matching current, remark, or history."""
    normalized = normalized_search_term(search_term)
    if normalized is None:
        return false()
    pattern = f"%{_escape_like_pattern(normalized)}%"
    remark_match = exists().where(
        ExternalContactFollow.tenant_id == tenant_id,
        ExternalContactFollow.external_userid == ExternalContact.external_userid,
        ExternalContactFollow.is_active.is_(True),
        ExternalContactFollow.remark_normalized.ilike(pattern, escape="\\"),
    )
    # A transition's ``new`` value may still be the current nickname. Only
    # an old value represents a historical identity for search purposes;
    # after a later rename that later value appears as ``old`` in turn.
    history_match = exists().where(
        ExternalContactNicknameHistory.tenant_id == tenant_id,
        ExternalContactNicknameHistory.external_userid == ExternalContact.external_userid,
        ExternalContactNicknameHistory.old_nickname_normalized.ilike(pattern, escape="\\"),
    )
    return or_(
        ExternalContact.current_nickname_normalized.ilike(pattern, escape="\\"),
        remark_match,
        history_match,
    )


def load_external_contact_search_matches(
    session: Session,
    tenant_id: str,
    search_term: object,
    *,
    external_userids: Optional[Iterable[str]] = None,
    limit: int = 100,
) -> dict[str, list[dict[str, Optional[str]]]]:
    """Return de-duplicated hit metadata keyed by one external identity.

    The result never includes blank/invisible nickname values. ``follow_userid``
    is populated only for a remark match so callers can show its employee
    context. The bounded source queries keep global search from materializing
    a tenant-wide match set in Python.
    """
    normalized = normalized_search_term(search_term)
    if normalized is None:
        return {}
    allowed_ids = (
        {value for value in external_userids if _clean_identifier(value)}
        if external_userids is not None
        else None
    )
    if allowed_ids == set():
        return {}
    pattern = f"%{_escape_like_pattern(normalized)}%"
    matches: dict[str, list[dict[str, Optional[str]]]] = {}

    def add(external_userid: str, match_type: str, follow_userid: Optional[str] = None) -> None:
        item = {"match_type": match_type, "follow_userid": follow_userid}
        bucket = matches.setdefault(external_userid, [])
        if item not in bucket:
            bucket.append(item)

    try:
        with session.begin_nested():
            current_query = select(ExternalContact.external_userid).where(
                ExternalContact.tenant_id == tenant_id,
                ExternalContact.current_nickname_normalized.ilike(pattern, escape="\\"),
            )
            if allowed_ids is not None:
                current_query = current_query.where(ExternalContact.external_userid.in_(allowed_ids))
            for external_userid in session.execute(current_query.limit(limit)).scalars():
                add(external_userid, "current_nickname")

            remark_query = select(
                ExternalContactFollow.external_userid,
                ExternalContactFollow.follow_userid,
            ).where(
                ExternalContactFollow.tenant_id == tenant_id,
                ExternalContactFollow.is_active.is_(True),
                ExternalContactFollow.remark_normalized.ilike(pattern, escape="\\"),
            )
            if allowed_ids is not None:
                remark_query = remark_query.where(ExternalContactFollow.external_userid.in_(allowed_ids))
            for external_userid, follow_userid in session.execute(remark_query.limit(limit)):
                add(external_userid, "remark", follow_userid)

            history_query = select(ExternalContactNicknameHistory.external_userid).where(
                ExternalContactNicknameHistory.tenant_id == tenant_id,
                ExternalContactNicknameHistory.old_nickname_normalized.ilike(pattern, escape="\\"),
            )
            if allowed_ids is not None:
                history_query = history_query.where(
                    ExternalContactNicknameHistory.external_userid.in_(allowed_ids)
                )
            for external_userid in session.execute(history_query.limit(limit)).scalars():
                add(external_userid, "historical_nickname")
    except DBAPIError:
        return {}

    return matches
