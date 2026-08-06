"""Minimal, one-use organization-creation claim lifecycle (RND-347)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import (
    TenantWecomConfig,
    WecomAuthorizationProof,
    WecomOrganizationClaim,
)

CLAIM_COOKIE = "wecom_org_claim"
CLAIM_TTL_SECONDS = 600


class OrganizationClaimError(RuntimeError):
    pass


class OrganizationAlreadyExists(OrganizationClaimError):
    pass


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_claim_from_proof(db: Session, raw_proof_token: str) -> str:
    now = datetime.now(timezone.utc)
    proof = (
        db.query(WecomAuthorizationProof)
        .filter(
            WecomAuthorizationProof.browser_token_hash == _digest(raw_proof_token),
            WecomAuthorizationProof.status == "pending",
            WecomAuthorizationProof.expires_at > now,
        )
        .with_for_update()
        .first()
    )
    if proof is None:
        raise OrganizationClaimError("Authorization proof is invalid or expired")

    existing = (
        db.query(TenantWecomConfig.id)
        .filter(
            TenantWecomConfig.corp_id == proof.corp_id,
            TenantWecomConfig.is_active.is_(True),
        )
        .first()
    )
    if existing is not None:
        proof.status = "consumed"
        proof.consumed_at = now
        db.commit()
        raise OrganizationAlreadyExists("Organization already exists")

    raw_ref = secrets.token_urlsafe(32)
    db.add(
        WecomOrganizationClaim(
            id=str(uuid.uuid4()),
            public_ref_hash=_digest(raw_ref),
            corp_id=proof.corp_id,
            corp_name=proof.corp_name,
            authorized_subject=proof.authorized_subject,
            agent_id=proof.agent_id,
            permanent_code_encrypted=proof.permanent_code_encrypted,
            state="pending",
            expires_at=now + timedelta(seconds=CLAIM_TTL_SECONDS),
        )
    )
    proof.status = "consumed"
    proof.consumed_at = now
    db.commit()
    return raw_ref


def get_browser_claim(
    db: Session, raw_ref: str | None, *, states: tuple[str, ...] = ("pending", "confirmed")
) -> WecomOrganizationClaim | None:
    if not raw_ref:
        return None
    now = datetime.now(timezone.utc)
    return (
        db.query(WecomOrganizationClaim)
        .filter(
            WecomOrganizationClaim.public_ref_hash == _digest(raw_ref),
            WecomOrganizationClaim.state.in_(states),
            WecomOrganizationClaim.expires_at > now,
        )
        .first()
    )


def confirm_browser_claim(db: Session, raw_ref: str | None) -> WecomOrganizationClaim:
    claim = get_browser_claim(db, raw_ref, states=("pending",))
    if claim is None:
        raise OrganizationClaimError("Organization claim is invalid or expired")
    claim.state = "confirmed"
    claim.confirmed_at = datetime.now(timezone.utc)
    db.commit()
    return claim


def cancel_browser_claim(db: Session, raw_ref: str | None) -> None:
    claim = get_browser_claim(db, raw_ref)
    if claim is not None:
        claim.state = "cancelled"
        claim.consumed_at = datetime.now(timezone.utc)
        db.commit()
