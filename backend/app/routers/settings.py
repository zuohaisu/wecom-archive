"""Settings endpoints (RND-302 / A8-1 change password)."""

from __future__ import annotations

import os
import re
from typing import Any, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.auth import get_auth_mode, get_current_user, hash_password, require_role, verify_password
from app.config import repository
from app.config.guard import is_initialized
from app.config.crypto import encrypt_value, mask
from app.config.resolver import get_config_resolver, invalidate
from app.config.schema import CONFIG_REGISTRY, ConfigItemSpec
from app.config.validation import check_domain_format, check_qiniu, check_wecom
from app.db.models import AdminUser, AppConfigStore
from app.db.session import get_db
from app.schemas.settings import SettingsErrorItem, SettingsGetOut, SettingsUpdateIn, SettingsUpdateOut

settings_router = APIRouter()


class _ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class _BootstrapBody(BaseModel):
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None
    wecom_corp_id: Optional[str] = None
    wecom_agent_id: Optional[str] = None
    wecom_oauth_secret: Optional[str] = None


class _ConnectionTestBody(BaseModel):
    """Only names a saved configuration target; credentials are never accepted."""

    # Extra fields are rejected in the route with a fixed detail so their
    # values (which could be credentials) never appear in a validation error.
    model_config = ConfigDict(extra="allow")

    target: Any


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
    write_audit(
        db,
        tenant_id=_tenant_id,
        admin_user_id=user.id,
        action=AuditAction.PASSWORD_CHANGED,
        object_type=AuditObjectType.USER,
        object_id=user.id,
    )
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


@settings_router.get("/settings/export", response_class=PlainTextResponse)
def export_settings(
    auth: Tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """Export resolved settings as .env text without exposing secrets."""
    del auth
    resolver = get_config_resolver()
    lines = []
    for key, spec in CONFIG_REGISTRY.items():
        value = "***" if spec.is_secret else (resolver.resolve(db, key) or "")
        lines.append(f"{key.upper()}={value}")
    return PlainTextResponse("\n".join(lines) + "\n")


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

    user, tenant_id = auth
    resolver = get_config_resolver()
    changed_keys: list[str] = []
    persisted_keys: list[str] = []
    for key, value in validated:
        # Blank secrets intentionally do not create, clear, or overwrite a row.
        if value is None:
            continue
        spec = CONFIG_REGISTRY[key]
        # Compare the canonical requested value to the effective value before
        # writing. This avoids both no-op config rows and misleading activity;
        # plaintext is never copied into audit detail.
        effective_value = resolver.resolve(db, key)
        # Persist a valid explicit setting even if it currently equals an
        # environment/default value. It is an intentional configuration
        # source choice; only the audit event is suppressed for that no-op.
        repository.upsert(
            db,
            key,
            encrypt_value(value) if spec.is_secret else value,
            is_secret=spec.is_secret,
            requires_restart=spec.restart_required,
            updated_by=getattr(user, "id", None),
        )
        persisted_keys.append(key)
        if effective_value != value:
            changed_keys.append(key)

    if changed_keys:
        write_audit(
            db,
            tenant_id=tenant_id,
            admin_user_id=getattr(user, "id", None),
            action=AuditAction.CONFIG_CHANGED,
            object_type=AuditObjectType.TENANT_CONFIG,
            detail={"changed_keys": sorted(changed_keys)},
        )
    db.commit()
    for key in persisted_keys:
        invalidate(key)

    return SettingsUpdateOut(
        ok=True,
        restart_required_keys=[
            key for key in payload.updates if CONFIG_REGISTRY[key].restart_required
        ],
    )


def _upsert_bootstrap_credential(db: Session, key: str, value: str) -> None:
    """Persist a bootstrap-only credential outside the editable config registry."""
    stored = db.get(AppConfigStore, key)
    if stored is None:
        db.add(
            AppConfigStore(
                key=key,
                group="advanced",
                value_type="string",
                value=value,
                is_secret=False,
                requires_restart=False,
                updated_by=None,
            )
        )
    else:
        stored.value = value


@settings_router.get("/settings/bootstrap-status")
def bootstrap_status(db: Session = Depends(get_db)) -> dict[str, bool]:
    """Publicly report whether first-run bootstrap is still required."""
    return {"initialized": is_initialized(db)}


@settings_router.post("/settings/bootstrap")
def bootstrap_settings(
    payload: _BootstrapBody,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    """Perform the one-time, public initialization before any session exists."""
    if is_initialized(db):
        raise HTTPException(status_code=403, detail="Already initialized")

    validated: list[tuple[str, str | None]] = []
    errors: list[SettingsErrorItem] = []
    for key, value in (
        ("wecom_corp_id", payload.wecom_corp_id),
        ("wecom_agent_id", payload.wecom_agent_id),
        ("wecom_oauth_secret", payload.wecom_oauth_secret),
    ):
        if value is None:
            continue
        normalised, error = _normalise_value(CONFIG_REGISTRY[key], value)
        if error is not None:
            errors.append(error)
        else:
            validated.append((key, normalised))

    if errors:
        return JSONResponse(
            status_code=400,
            content={"errors": [error.model_dump() for error in errors]},
        )

    if get_auth_mode() == "password":
        admin_username = (payload.admin_username or "").strip()
        admin_password = payload.admin_password or ""
        if not admin_username or not admin_password:
            raise HTTPException(status_code=400, detail="Administrator credentials are required")
        _upsert_bootstrap_credential(db, "admin_username", admin_username)
        _upsert_bootstrap_credential(db, "admin_password_hash", hash_password(admin_password))

    for key, value in validated:
        if value is None:
            continue
        spec = CONFIG_REGISTRY[key]
        repository.upsert(
            db,
            key,
            encrypt_value(value) if spec.is_secret else value,
            is_secret=spec.is_secret,
            requires_restart=spec.restart_required,
            updated_by=None,
        )

    db.commit()
    for key, _value in validated:
        invalidate(key)
    return {"ok": True}


@settings_router.post("/settings/test-connection")
def test_connection(
    payload: _ConnectionTestBody,
    auth: Tuple[AdminUser, str] = Depends(require_role()),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Test the selected saved integration without returning configuration values."""
    del auth
    if payload.model_extra or payload.target not in {"qiniu", "wecom", "domain"}:
        raise HTTPException(status_code=422, detail="invalid_connection_test_request")

    resolver = get_config_resolver()
    if payload.target == "qiniu":
        ok, reason = check_qiniu(
            resolver.resolve(db, "qiniu_access_key") or "",
            resolver.resolve(db, "qiniu_secret_key") or "",
            resolver.resolve(db, "qiniu_bucket") or "",
            resolver.resolve(db, "qiniu_domain") or "",
            region=resolver.resolve(db, "qiniu_region") or None,
        )
    elif payload.target == "wecom":
        ok, reason = check_wecom(
            resolver.resolve(db, "wecom_corp_id") or "",
            resolver.resolve(db, "wecom_oauth_secret") or "",
        )
    else:
        ok, reason = check_domain_format(resolver.resolve(db, "admin_domain") or "")

    return {"ok": ok, "reason": reason}
