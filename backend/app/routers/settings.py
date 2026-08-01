"""Settings endpoints (RND-302 / A8-1 change password)."""

from __future__ import annotations

import os
import re
from typing import Any, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password, require_role, verify_password
from app.config import repository
from app.config.crypto import encrypt_value, mask
from app.config.resolver import get_config_resolver, invalidate
from app.config.schema import CONFIG_REGISTRY, ConfigItemSpec
from app.db.models import AdminUser
from app.db.session import get_db
from app.schemas.settings import SettingsErrorItem, SettingsGetOut, SettingsUpdateIn, SettingsUpdateOut

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


def _config_source(db: Session, key: str) -> str:
    """Determine the same DB > env > default source used by the resolver."""
    stored = repository.get_raw(db, key)
    if stored is not None and stored.value is not None:
        return "db"
    return "env" if key.upper() in os.environ else "default"


def _normalise_value(spec: ConfigItemSpec, value: Any) -> tuple[str | None, SettingsErrorItem | None]:
    """Validate one wire value and return its canonical database representation."""
    if spec.is_secret and value == "":
        # A masked secret cannot safely be round-tripped, so a blank secret is
        # explicitly a no-op rather than a request to erase it.
        return None, None

    if spec.value_type in {"string", "secret"}:
        if not isinstance(value, str):
            return None, SettingsErrorItem(
                key=spec.key,
                message="value must be a string",
                code="invalid_type",
            )
        if spec.required and value == "":
            return None, SettingsErrorItem(
                key=spec.key,
                message="value is required",
                code="required",
            )
        return value, None

    if spec.value_type == "int":
        if isinstance(value, bool):
            valid = False
        elif isinstance(value, int):
            return str(value), None
        else:
            valid = isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value) is not None
        if valid:
            return str(int(value)), None
        return None, SettingsErrorItem(
            key=spec.key,
            message="value must be an integer",
            code="invalid_type",
        )

    if spec.value_type == "bool":
        if isinstance(value, bool):
            return str(value).lower(), None
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower(), None
        return None, SettingsErrorItem(
            key=spec.key,
            message="value must be a boolean",
            code="invalid_type",
        )

    return None, SettingsErrorItem(
        key=spec.key,
        message="unsupported configuration value type",
        code="invalid_type",
    )


@settings_router.get("/settings", response_model=SettingsGetOut)
def get_settings(
    auth: Tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> SettingsGetOut:
    """Return all registered settings without ever exposing secret plaintext."""
    del auth
    resolver = get_config_resolver()
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, spec in CONFIG_REGISTRY.items():
        value = resolver.resolve(db, key)
        if spec.is_secret:
            value = mask(value or "")
        groups.setdefault(spec.group.value, []).append(
            {
                "key": key,
                "value": value,
                "source": _config_source(db, key),
                "requires_restart": spec.restart_required,
            }
        )
    return SettingsGetOut(groups=groups)


@settings_router.put("/settings", response_model=SettingsUpdateOut)
def put_settings(
    payload: SettingsUpdateIn,
    auth: Tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> SettingsUpdateOut | JSONResponse:
    """Atomically validate and persist a batch of deployment settings."""
    errors: list[SettingsErrorItem] = []
    validated: list[tuple[str, str | None]] = []

    for key, value in payload.updates.items():
        spec = CONFIG_REGISTRY.get(key)
        if spec is None:
            errors.append(
                SettingsErrorItem(
                    key=key,
                    message="unknown configuration key",
                    code="unknown_key",
                )
            )
            continue
        normalised, error = _normalise_value(spec, value)
        if error is not None:
            errors.append(error)
        else:
            validated.append((key, normalised))

    if errors:
        return JSONResponse(
            status_code=400,
            content={"errors": [error.model_dump() for error in errors]},
        )

    user, _tenant_id = auth
    for key, value in validated:
        # Blank secrets intentionally do not create, clear, or overwrite a row.
        if value is None:
            continue
        spec = CONFIG_REGISTRY[key]
        repository.upsert(
            db,
            key,
            encrypt_value(value) if spec.is_secret else value,
            is_secret=spec.is_secret,
            requires_restart=spec.restart_required,
            updated_by=getattr(user, "id", None),
        )

    db.commit()
    for key, _value in validated:
        invalidate(key)

    return SettingsUpdateOut(
        ok=True,
        restart_required_keys=[
            key for key in payload.updates if CONFIG_REGISTRY[key].restart_required
        ],
    )
