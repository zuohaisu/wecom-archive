"""Settings endpoints (RND-302 / A8-1 change password)."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password, verify_password
from app.db.models import AdminUser
from app.db.session import get_db

settings_router = APIRouter()


class _ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


@settings_router.post("/settings/password")
def change_password(
    body: _ChangePasswordBody,
    current: Tuple[AdminUser, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Verify the old password and set a new PBKDF2 password hash."""
    user, _tenant_id = current
    if user.password_hash is None:
        raise HTTPException(status_code=400, detail="no_password_set")
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid_old_password")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="weak_password")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="same_as_old")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return JSONResponse({"ok": True})
