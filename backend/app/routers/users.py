"""Tenant-scoped admin-user listing endpoints (RND-284)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import create_password_reset_token, get_current_user, require_role
from app.db.models import (
    AdminAccessRequest,
    AdminLoginIdentity,
    AdminSession,
    AdminUser,
    ArchiveMessage,
)
from app.db.session import get_db
from app.email import send_password_reset_email
from app.schemas.admin_users import AdminUserDetail, AdminUserListItem, AdminUserListOut
from app.services.avatar_sync import internal_avatar_presentations
from app.settings import get_email_settings, get_wecom_oauth_settings

users_router = APIRouter()
_USER_ROLES = {"owner", "admin", "compliance", "legal", "readonlyaudit"}


@users_router.get("/users", response_model=AdminUserListOut)
def list_admin_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(
        None, pattern="^(owner|admin|compliance|legal|readonlyaudit)$"
    ),
    status: Optional[str] = Query(None, pattern="^(active|disabled)$"),
    q: Optional[str] = Query(None, max_length=128),
    silent_days: Optional[int] = Query(None, ge=1),
    auth: tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdminUserListOut:
    """Return users belonging only to the authenticated user's tenant."""
    _, tenant_id = auth
    statement = db.query(AdminUser).filter(AdminUser.tenant_id == tenant_id)

    if role:
        statement = statement.filter(AdminUser.role == role)
    if status:
        statement = statement.filter(AdminUser.status == status)
    if silent_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=silent_days)
        statement = statement.filter(AdminUser.last_active_at < cutoff)
    if q:
        like = f"%{q}%"
        statement = statement.filter(
            or_(
                AdminUser.name.ilike(like),
                AdminUser.wecom_user_id.ilike(like),
                AdminUser.email.ilike(like),
                AdminUser.department.ilike(like),
            )
        )

    total = statement.with_entities(func.count(AdminUser.id)).scalar() or 0
    rows = (
        statement.order_by(
            AdminUser.last_active_at.desc().nullslast(), AdminUser.name.asc()
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    # RND-284: ``msg_count_30d`` is the count of archive messages sent by the
    # employee (``sender == wecom_user_id``), not received messages. This only
    # counts row ids and deliberately never reads message payload/content fields.
    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000
    )
    aggregates = (
        db.query(ArchiveMessage.sender, func.count(ArchiveMessage.id))
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.msgtime >= cutoff_ms,
        )
        .group_by(ArchiveMessage.sender)
        .all()
    )
    msg_counts = {sender: count for sender, count in aggregates}
    avatars = internal_avatar_presentations(
        db, tenant_id, {user.wecom_user_id for user in rows}
    )

    return AdminUserListOut(
        items=[
            AdminUserListItem(
                id=user.id,
                name=user.name,
                wecom_user_id=user.wecom_user_id,
                email=user.email,
                department=user.department,
                avatar_url=avatars[user.wecom_user_id].url,
                avatar_status=avatars[user.wecom_user_id].status,
                role=user.role,
                status=user.status,
                last_active_at=(
                    user.last_active_at.isoformat() if user.last_active_at else None
                ),
                msg_count_30d=msg_counts.get(user.wecom_user_id, 0),
            )
            for user in rows
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@users_router.get("/users/{user_id}", response_model=AdminUserDetail)
def get_admin_user_detail(
    user_id: str,
    auth: tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdminUserDetail:
    """Return one tenant-scoped console employee profile for the detail drawer."""
    _, tenant_id = auth
    user = _resolve_target(db, user_id, tenant_id)
    avatar = internal_avatar_presentations(db, tenant_id, {user.wecom_user_id})[
        user.wecom_user_id
    ]
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
    msg_count = (
        db.query(func.count(ArchiveMessage.id))
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.sender == user.wecom_user_id,
            ArchiveMessage.msgtime >= cutoff_ms,
        )
        .scalar()
        or 0
    )
    return AdminUserDetail(
        id=user.id,
        name=user.name,
        wecom_user_id=user.wecom_user_id,
        email=user.email,
        department=user.department,
        avatar_url=avatar.url,
        avatar_status=avatar.status,
        role=user.role,
        status=user.status,
        last_active_at=user.last_active_at.isoformat() if user.last_active_at else None,
        msg_count_30d=int(msg_count),
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


class _StatusUpdate(BaseModel):
    status: str


class _RoleUpdate(BaseModel):
    role: str


def _resolve_target(db: Session, user_id: str, tenant_id: str) -> AdminUser:
    """Look up a target within the caller's tenant without account enumeration."""
    user = (
        db.query(AdminUser)
        .filter(AdminUser.id == user_id, AdminUser.tenant_id == tenant_id)
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _user_dto(user: AdminUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
    }


def _require_owner_authority(
    db: Session,
    *,
    actor: AdminUser,
    target: AdminUser,
    next_role: Optional[str] = None,
    next_status: Optional[str] = None,
) -> None:
    """Protect the owner role from peer-admin escalation or lockout.

    ``admin`` is intentionally allowed to manage ordinary tenant accounts, but
    it must never be able to turn itself (or another account) into an owner,
    nor change an existing owner's access.  Owners may manage each other, but
    the tenant must retain at least one owner.
    """
    if actor.role != "owner" and (
        target.role == "owner" or next_role == "owner"
    ):
        raise HTTPException(status_code=403, detail="owner_role_requires_owner")

    removes_active_owner = (
        target.role == "owner"
        and target.status == "active"
        and (next_role not in (None, "owner") or next_status == "disabled")
    )
    if removes_active_owner:
        owner_count = (
            db.query(func.count(AdminUser.id))
            .filter(
                AdminUser.tenant_id == target.tenant_id,
                AdminUser.role == "owner",
                AdminUser.status == "active",
            )
            .scalar()
            or 0
        )
        if owner_count <= 1:
            raise HTTPException(status_code=400, detail="cannot_remove_last_owner")


@users_router.patch("/users/{user_id}")
def update_user_status(
    user_id: str,
    body: _StatusUpdate,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Enable or disable a user within the caller's tenant."""
    if body.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="invalid_status")
    current_user, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    if target.id == current_user.id and body.status == "disabled":
        raise HTTPException(status_code=400, detail="cannot_disable_self")
    _require_owner_authority(
        db, actor=current_user, target=target, next_status=body.status
    )

    if target.status == body.status:
        return _user_dto(target)

    target.status = body.status
    if body.status == "disabled":
        now = datetime.now(timezone.utc)
        db.query(AdminSession).filter(
            AdminSession.admin_user_id == target.id,
            AdminSession.is_revoked.is_(False),
            AdminSession.expires_at > now,
        ).update({AdminSession.is_revoked: True})
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=current_user.id,
        action=AuditAction.USER_DISABLED if body.status == "disabled" else AuditAction.USER_ENABLED,
        object_type=AuditObjectType.USER,
        object_id=target.id,
    )
    db.commit()
    return _user_dto(target)


@users_router.patch("/users/{user_id}/role")
def update_user_role(
    user_id: str,
    body: _RoleUpdate,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Change a tenant user's role, with an owner-only escalation boundary."""
    if body.role not in _USER_ROLES:
        raise HTTPException(status_code=400, detail="invalid_role")
    current_user, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot_change_own_role")
    _require_owner_authority(
        db, actor=current_user, target=target, next_role=body.role
    )

    if target.role == body.role:
        return _user_dto(target)

    previous_role = target.role
    target.role = body.role
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=current_user.id,
        action=AuditAction.USER_ROLE_CHANGED,
        object_type=AuditObjectType.USER,
        object_id=target.id,
        detail={"previous_role": previous_role, "new_role": body.role},
    )
    db.commit()
    return _user_dto(target)


@users_router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: str,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Email a one-time password-reset link to an active tenant user."""
    _, tenant_id = auth
    target = _resolve_target(db, user_id, tenant_id)
    if target.status != "active":
        raise HTTPException(status_code=409, detail="user_not_active")
    if not target.email:
        raise HTTPException(status_code=400, detail="user_has_no_email")

    email_settings = get_email_settings()
    try:
        ttl = int(email_settings.reset_token_ttl_hours or "1")
    except ValueError:
        ttl = 1
    raw_token = create_password_reset_token(db, target, ttl)
    base_url = email_settings.reset_base_url or get_wecom_oauth_settings().admin_domain
    reset_link = f"{base_url.rstrip('/')}/admin/reset-password?token={raw_token}"
    if not send_password_reset_email(target.email, reset_link):
        db.rollback()
        raise HTTPException(status_code=500, detail="email_delivery_failed")
    actor, _ = auth
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=actor.id,
        action=AuditAction.USER_PASSWORD_RESET_INITIATED,
        object_type=AuditObjectType.USER,
        object_id=target.id,
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# RND-321 — access-request review (owner/admin only)
#
# admin_access_requests holds WeCom identities the app has verified but
# that are not yet bound to any AdminUser (see AdminAccessRequest's model
# docstring). Nothing here ever auto-links or auto-creates an account —
# every row is inert until an owner/admin takes one of the two explicit
# actions below. Deliberately kept separate from the regular user-lifecycle
# endpoints above rather than folded into /users: a pending request is not
# a user, has no role, and must not appear in that listing (AC-5).
# ---------------------------------------------------------------------------


def _access_request_dto(db: Session, tenant_id: str, request: AdminAccessRequest) -> dict[str, Any]:
    """Never includes `subject` (the raw WeCom UserId) — reviewers need the
    display name and email hint to make a decision, not the raw identity
    value itself (https://github.com/zuohaisu/wecom-archive/wiki/Agent-Data-Minimization: identity fields don't
    belong on a response surface that doesn't need them to function).
    """
    suspected_match = None
    suspected_match_status = "none"
    if request.email_hint:
        # A hint only, shown to a human for their own judgment call — never
        # used anywhere to auto-link or auto-create (AC-3, product decision
        # #3). Matches on this tenant's accounts only. Email reuse across
        # accounts is real (shared mailboxes, reissued addresses), so two or
        # more accounts sharing this email is a conflict for the reviewer to
        # judge, not something to resolve by picking one arbitrarily — only
        # a single unambiguous match is ever surfaced as a candidate.
        candidates = (
            db.query(AdminUser)
            .filter(
                AdminUser.tenant_id == tenant_id,
                AdminUser.email.isnot(None),
                func.lower(AdminUser.email) == request.email_hint,
            )
            .limit(2)
            .all()
        )
        if len(candidates) == 1:
            suspected_match_status = "single"
            suspected_match = {"id": candidates[0].id, "name": candidates[0].name}
        elif len(candidates) > 1:
            suspected_match_status = "multiple"
    return {
        "id": request.id,
        "display_name": request.display_name,
        "email_hint": request.email_hint,
        "status": request.status,
        "resolution": request.resolution,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "resolved_at": request.resolved_at.isoformat() if request.resolved_at else None,
        "suspected_match": suspected_match,
        "suspected_match_status": suspected_match_status,
    }


@users_router.get("/access-requests")
def list_access_requests(
    status: Optional[str] = Query(None, pattern="^(pending|resolved)$"),
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Owner/admin only (AC-5) — ordinary roles cannot see requests, email
    hints, or identity information via this or any other endpoint."""
    _, tenant_id = auth
    statement = db.query(AdminAccessRequest).filter(AdminAccessRequest.tenant_id == tenant_id)
    statement = statement.filter(AdminAccessRequest.status == (status or "pending"))
    rows = statement.order_by(AdminAccessRequest.created_at.desc()).all()
    return {"items": [_access_request_dto(db, tenant_id, row) for row in rows]}


def _resolve_pending_request(db: Session, request_id: str, tenant_id: str) -> AdminAccessRequest:
    """Tenant-scoped lookup without an existence oracle — a wrong-tenant id
    and a missing id both 404 identically."""
    request = (
        db.query(AdminAccessRequest)
        .filter(AdminAccessRequest.id == request_id, AdminAccessRequest.tenant_id == tenant_id)
        .first()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="access_request_not_found")
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="access_request_already_resolved")
    return request


def _claim_pending_request(db: Session, request_id: str) -> bool:
    """Atomically flip pending -> claimed-in-progress by including the
    status in the UPDATE's WHERE clause and checking the row count, not by
    trusting the read a moment earlier. Two concurrent reviewers resolving
    the same request race here at the database, not in Python — exactly
    one UPDATE matches a row; the other sees rowcount 0 and must treat
    that as "someone else already resolved this", not retry the write.
    """
    result = db.query(AdminAccessRequest).filter(
        AdminAccessRequest.id == request_id,
        AdminAccessRequest.status == "pending",
    ).update({AdminAccessRequest.status: "resolved"})
    return result == 1


def _find_wecom_user_id_collision(
    db: Session, tenant_id: str, subject: str, *, exclude_admin_user_id: Optional[str] = None
) -> Optional[AdminUser]:
    """A pre-RND-321 orphaned account — created by the old, superseded
    access_requested flow, deliberately excluded from migration 0034's
    identity backfill — can still hold `subject` in the legacy
    admin_users.wecom_user_id compat column, which carries a
    (tenant_id, wecom_user_id) uniqueness constraint. Resolving a fresh
    request for the same subject needs to know about that before
    attempting a write that would otherwise fail — see the two callers.
    """
    query = db.query(AdminUser).filter(
        AdminUser.tenant_id == tenant_id,
        AdminUser.wecom_user_id == subject,
    )
    if exclude_admin_user_id is not None:
        query = query.filter(AdminUser.id != exclude_admin_user_id)
    return query.first()


def _is_releasable_legacy_orphan(db: Session, account: AdminUser) -> bool:
    """True only for a confirmed pre-RND-321 orphan: created by the old
    access_requested flow, and never itself bound to a login identity.
    Refusing to release anything else is deliberate — an unexpected
    collision shape (should not occur under the current design, since
    admin_login_identities is the sole binding mechanism going forward)
    surfaces as an error for a human to investigate, never something this
    silently acts on.
    """
    if account.invite_status != "access_requested":
        return False
    return (
        db.query(AdminLoginIdentity)
        .filter(
            AdminLoginIdentity.admin_user_id == account.id,
            AdminLoginIdentity.provider == "wecom",
        )
        .first()
        is None
    )


def _handle_legacy_identity_collision(
    db: Session,
    *,
    tenant_id: str,
    subject: str,
    actor: AdminUser,
    release_requested: bool,
    exclude_admin_user_id: Optional[str] = None,
) -> Optional[str]:
    """Returns the released legacy account's id if a release happened,
    None if there was no collision to begin with. Raises HTTPException for
    every other outcome: conflict shown but not released (default —
    release_requested is opt-in, never implied), release denied to a
    non-owner, or release refused because the colliding account isn't a
    confirmed-safe legacy orphan. Never includes the raw WeCom subject in
    any response — only the colliding account's id and name, exactly like
    the suspected-match hint.
    """
    colliding = _find_wecom_user_id_collision(
        db, tenant_id, subject, exclude_admin_user_id=exclude_admin_user_id
    )
    if colliding is None:
        return None
    if not release_requested:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "legacy_identity_conflict",
                "conflicting_account": {"id": colliding.id, "name": colliding.name},
            },
        )
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="owner_required_for_legacy_release")
    if not _is_releasable_legacy_orphan(db, colliding):
        raise HTTPException(status_code=409, detail="legacy_release_not_permitted")
    # Clears only the stale compat value so it stops colliding — never the
    # AdminUser row itself, its role, status, name, email, or audit history.
    colliding.wecom_user_id = f"__legacy_ar__:{uuid.uuid4()}"
    return colliding.id


class _LinkAccessRequestBody(BaseModel):
    admin_user_id: str
    release_conflicting_legacy_account: bool = False


@users_router.post("/access-requests/{request_id}/link")
def link_access_request(
    request_id: str,
    body: _LinkAccessRequestBody,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Bind a pending request to an explicitly chosen, existing account.

    Never triggered by an email or name match — the caller must name the
    exact target account id (AC-3). The owner-boundary check reuses
    _require_owner_authority unchanged: with no role/status change
    proposed, it reduces to exactly "a non-owner actor cannot touch an
    owner-role target", which is the correct rule here too — attaching a
    new login path to an owner account is still an operation on that
    account.
    """
    current_user, tenant_id = auth
    request = _resolve_pending_request(db, request_id, tenant_id)
    target = _resolve_target(db, body.admin_user_id, tenant_id)
    _require_owner_authority(db, actor=current_user, target=target)

    existing_identity = (
        db.query(AdminLoginIdentity)
        .filter(
            AdminLoginIdentity.admin_user_id == target.id,
            AdminLoginIdentity.provider == "wecom",
        )
        .first()
    )
    if existing_identity is not None:
        raise HTTPException(status_code=409, detail="account_already_has_login_identity")

    released_legacy_account_id = _handle_legacy_identity_collision(
        db,
        tenant_id=tenant_id,
        subject=request.subject,
        actor=current_user,
        release_requested=body.release_conflicting_legacy_account,
        exclude_admin_user_id=target.id,
    )

    if not _claim_pending_request(db, request_id):
        db.rollback()
        raise HTTPException(status_code=409, detail="access_request_already_resolved")

    now = datetime.now(timezone.utc)
    request.resolution = "linked"
    request.resolved_admin_user_id = target.id
    request.resolved_by_admin_user_id = current_user.id
    request.resolved_at = now
    db.add(
        AdminLoginIdentity(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider="wecom",
            subject=request.subject,
            admin_user_id=target.id,
        )
    )
    # Uniqueness is verified above; only after that check passes does the
    # legacy compatibility field get synced (never the other way around —
    # see AdminLoginIdentity's model docstring).
    target.wecom_user_id = request.subject
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="identity_conflict")

    audit_detail = {"linked_admin_user_id": target.id}
    if released_legacy_account_id:
        audit_detail["released_legacy_account_id"] = released_legacy_account_id
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=current_user.id,
        action=AuditAction.USER_ACCESS_REQUEST_LINKED,
        object_type=AuditObjectType.ACCESS_REQUEST,
        object_id=request.id,
        detail=audit_detail,
    )
    db.commit()
    return _user_dto(target)


class _CreateAccountFromRequestBody(BaseModel):
    role: str
    status: str = "active"
    name: Optional[str] = None
    release_conflicting_legacy_account: bool = False


@users_router.post("/access-requests/{request_id}/create-account")
def create_account_from_access_request(
    request_id: str,
    body: _CreateAccountFromRequestBody,
    auth: tuple[AdminUser, str] = Depends(require_role("admin", "owner")),
    db: Session = Depends(get_db),
):
    """Create a brand-new account from a pending request and bind it.

    The requester never gets to choose their own role or status (AC-4) —
    both are explicit choices made by the reviewing owner/admin, defaulted
    to nothing (role is required) so a client can't silently omit it and
    get an unintended default.
    """
    if body.role not in _USER_ROLES:
        raise HTTPException(status_code=400, detail="invalid_role")
    if body.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="invalid_status")
    current_user, tenant_id = auth
    if body.role == "owner" and current_user.role != "owner":
        raise HTTPException(status_code=403, detail="owner_role_requires_owner")

    request = _resolve_pending_request(db, request_id, tenant_id)

    released_legacy_account_id = _handle_legacy_identity_collision(
        db,
        tenant_id=tenant_id,
        subject=request.subject,
        actor=current_user,
        release_requested=body.release_conflicting_legacy_account,
    )

    if not _claim_pending_request(db, request_id):
        db.rollback()
        raise HTTPException(status_code=409, detail="access_request_already_resolved")

    now = datetime.now(timezone.utc)
    new_user = AdminUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        wecom_user_id=request.subject,
        name=body.name or request.display_name,
        email=request.email_hint,
        role=body.role,
        status=body.status,
    )
    db.add(new_user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="identity_conflict")

    request.resolution = "created"
    request.resolved_admin_user_id = new_user.id
    request.resolved_by_admin_user_id = current_user.id
    request.resolved_at = now
    db.add(
        AdminLoginIdentity(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider="wecom",
            subject=request.subject,
            admin_user_id=new_user.id,
        )
    )
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="identity_conflict")

    audit_detail = {"created_admin_user_id": new_user.id, "role": body.role, "status": body.status}
    if released_legacy_account_id:
        audit_detail["released_legacy_account_id"] = released_legacy_account_id
    write_audit(
        db,
        tenant_id=tenant_id,
        admin_user_id=current_user.id,
        action=AuditAction.USER_ACCESS_REQUEST_ACCOUNT_CREATED,
        object_type=AuditObjectType.ACCESS_REQUEST,
        object_id=request.id,
        detail=audit_detail,
    )
    db.commit()
    return _user_dto(new_user)
