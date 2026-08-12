"""Encrypted suite_ticket persistence and freshness authority (RND-350)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import (
    FieldDecryptionError,
    FieldEncryptionConfigurationError,
    decrypt_value,
    encrypt_value,
)
from app.db.models import WecomSuiteTicketState

SUITE_TICKET_WARNING_SECONDS = 20 * 60
SUITE_TICKET_EXPIRED_SECONDS = 30 * 60
SUITE_TICKET_MAX_BYTES = 512


class SuiteTicketUnavailable(RuntimeError):
    """The current suite ticket cannot safely authorize a provider request."""


class SuiteTicketWriteOutcome(str, Enum):
    STORED = "stored"
    REPLACED = "replaced"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True)
class SuiteTicketFreshness:
    received: bool
    state: str
    last_received_at: datetime | None
    age_seconds: int | None
    requires_alert: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ticket_digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _validated_ticket(ticket: str) -> str:
    value = ticket.strip()
    if not value or len(value.encode("utf-8")) > SUITE_TICKET_MAX_BYTES:
        raise ValueError("Invalid suite ticket")
    return value


def _apply_newer_ticket(
    row: WecomSuiteTicketState,
    *,
    ticket: str,
    digest: str,
    source_timestamp: int,
    received_at: datetime,
) -> SuiteTicketWriteOutcome:
    if source_timestamp < row.source_timestamp:
        return SuiteTicketWriteOutcome.STALE
    if source_timestamp == row.source_timestamp:
        if digest == row.ticket_digest:
            return SuiteTicketWriteOutcome.DUPLICATE
        # Two different signed payloads claiming the same provider timestamp
        # are ambiguous. Keep the already-authoritative value fail-closed.
        return SuiteTicketWriteOutcome.STALE
    row.ticket_encrypted = encrypt_value(ticket)
    row.ticket_digest = digest
    row.source_timestamp = source_timestamp
    row.received_at = received_at
    return SuiteTicketWriteOutcome.REPLACED


def store_suite_ticket(
    db: Session,
    *,
    suite_id: str,
    ticket: str,
    source_timestamp: int,
    received_at: datetime | None = None,
) -> SuiteTicketWriteOutcome:
    """Atomically retain only the newest signed ticket for a provider suite."""
    normalized_suite_id = suite_id.strip()
    if not normalized_suite_id or len(normalized_suite_id) > 128:
        raise ValueError("Invalid suite id")
    if isinstance(source_timestamp, bool) or source_timestamp <= 0:
        raise ValueError("Invalid suite ticket timestamp")
    normalized_ticket = _validated_ticket(ticket)
    digest = _ticket_digest(normalized_ticket)
    received = _as_utc(received_at or _utcnow())

    row = (
        db.query(WecomSuiteTicketState)
        .filter(WecomSuiteTicketState.suite_id == normalized_suite_id)
        .with_for_update()
        .first()
    )
    if row is not None:
        outcome = _apply_newer_ticket(
            row,
            ticket=normalized_ticket,
            digest=digest,
            source_timestamp=source_timestamp,
            received_at=received,
        )
        db.commit()
        return outcome

    db.add(
        WecomSuiteTicketState(
            suite_id=normalized_suite_id,
            ticket_encrypted=encrypt_value(normalized_ticket),
            ticket_digest=digest,
            source_timestamp=source_timestamp,
            received_at=received,
        )
    )
    try:
        db.commit()
        return SuiteTicketWriteOutcome.STORED
    except IntegrityError:
        # A concurrent first delivery won the primary-key race. Re-evaluate
        # against that row so an older callback can never overwrite it.
        db.rollback()
        row = (
            db.query(WecomSuiteTicketState)
            .filter(WecomSuiteTicketState.suite_id == normalized_suite_id)
            .with_for_update()
            .one()
        )
        outcome = _apply_newer_ticket(
            row,
            ticket=normalized_ticket,
            digest=digest,
            source_timestamp=source_timestamp,
            received_at=received,
        )
        db.commit()
        return outcome


def suite_ticket_freshness(
    db: Session,
    suite_id: str,
    *,
    now: datetime | None = None,
) -> SuiteTicketFreshness:
    """Return an operational status that contains no ticket-derived value."""
    row = db.get(WecomSuiteTicketState, suite_id.strip()) if suite_id.strip() else None
    if row is None:
        return SuiteTicketFreshness(False, "missing", None, None, True)
    observed_at = _as_utc(row.received_at)
    age_seconds = max(0, int((_as_utc(now or _utcnow()) - observed_at).total_seconds()))
    if age_seconds >= SUITE_TICKET_EXPIRED_SECONDS:
        state = "expired"
    elif age_seconds >= SUITE_TICKET_WARNING_SECONDS:
        state = "warning"
    else:
        state = "fresh"
    return SuiteTicketFreshness(
        True,
        state,
        observed_at,
        age_seconds,
        state != "fresh",
    )


def load_fresh_suite_ticket(
    db: Session,
    suite_id: str,
    *,
    now: datetime | None = None,
) -> str:
    """Decrypt the current ticket only when it is younger than 30 minutes."""
    status = suite_ticket_freshness(db, suite_id, now=now)
    if status.state in {"missing", "expired"}:
        raise SuiteTicketUnavailable("WeCom provider ticket is unavailable")
    row = db.get(WecomSuiteTicketState, suite_id.strip())
    if row is None:
        raise SuiteTicketUnavailable("WeCom provider ticket is unavailable")
    try:
        return _validated_ticket(decrypt_value(row.ticket_encrypted))
    except (
        FieldDecryptionError,
        FieldEncryptionConfigurationError,
        ValueError,
    ) as exc:
        raise SuiteTicketUnavailable("WeCom provider ticket is unavailable") from exc
