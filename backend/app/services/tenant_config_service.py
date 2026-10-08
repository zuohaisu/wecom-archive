"""Tenant-scoped WeCom archive configuration for provisioning tenants (RND-386).

A provisioning owner fills the session-archive credentials that Tencent
requires the customer's enterprise admin to create in their own WeCom admin
console: the archive Secret, the RSA private key whose public key was uploaded
to the admin console, and the callback Token/EncodingAESKey configured for the
archive event URL. corp_id and agent_id always come from the trusted
third-party authorization binding — never from the client.

All secrets are stored with the existing field-encryption primitives
(app.crypto / FIELD_ENCRYPTION_KEY). Snapshots return masks only; blank input
values never overwrite an existing secret.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from app.audit import AuditAction, AuditObjectType, write_audit
from app.crypto import FieldDecryptionError
from app.config.crypto import mask
from app.config.resolver import get_config_resolver
from app.db.models import Tenant, TenantWecomConfig, ThirdPartyOrganizationBinding
from app.settings import APP_EDITION_SELFHOST, get_app_edition, get_wecom_oauth_settings
from app.services.wecom_callback_crypto import (
    CallbackConfigurationError,
    decode_aes_key,
)

#: Order matters for the wizard: each key maps to one form field.
FIELD_KEYS = (
    "archive_secret",
    "private_key",
    "publickey_version",
    "callback_token",
    "callback_encoding_aes_key",
)

SECRET_FIELD_KEYS = frozenset(
    {"archive_secret", "private_key", "callback_token", "callback_encoding_aes_key"}
)

#: Keys a client must never be able to write through this surface.
RESERVED_KEYS = frozenset(
    {"tenant_id", "corp_id", "agent_id", "is_active", "lifecycle_status"}
)

_ARCHIVE_CALLBACK_PATH = "/api/wecom/archive/events"


class TenantConfigValidationError(ValueError):
    """Structured per-field validation failure; never carries input values."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("tenant config validation failed")


class TenantConfigBindingMissingError(RuntimeError):
    """Raised when a provisioning tenant lacks its trusted org binding."""


def _stored_value(config: TenantWecomConfig | None, key: str):
    if config is None:
        return None
    return {
        "archive_secret": config.app_secret,
        "private_key": config.private_key_encrypted,
        "publickey_version": config.publickey_version,
        "callback_token": config.callback_token_encrypted,
        "callback_encoding_aes_key": config.callback_encoding_aes_key_encrypted,
    }[key]


def _plaintext(config: TenantWecomConfig, key: str) -> str:
    # Single-key dispatch: the accessors must never be evaluated for keys
    # whose stored column is still None.
    if key == "archive_secret":
        return config.decrypted_app_secret
    if key == "private_key":
        return config.decrypted_private_key
    if key == "callback_token":
        return config.decrypted_callback_token
    return config.decrypted_callback_encoding_aes_key


def _field_snapshot(
    config: TenantWecomConfig | None, key: str, missing: list[str]
) -> dict:
    stored = _stored_value(config, key)
    if stored is None or stored == "":
        missing.append(key)
        return {"status": "missing", "source": None, "mask": None}
    if key in SECRET_FIELD_KEYS:
        try:
            masked = mask(_plaintext(config, key))
        except FieldDecryptionError:
            return {"status": "unreadable", "source": "db", "mask": None}
        return {"status": "set", "source": "db", "mask": masked}
    # publickey_version is a non-secret integer.
    return {"status": "set", "source": "db", "mask": None, "value": stored}


def _callback_url() -> str | None:
    admin_domain = get_wecom_oauth_settings().admin_domain.strip()
    if not admin_domain:
        return None
    return f"{admin_domain.rstrip('/')}{_ARCHIVE_CALLBACK_PATH}"


def config_snapshot(db: Session, tenant_id: str) -> dict:
    """Read-only view of the tenant's archive config. Never returns plaintext."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if get_app_edition() == APP_EDITION_SELFHOST:
        resolver = get_config_resolver()
        corp_id = (resolver.resolve(db, "wecom_corp_id") or "").strip()
        agent_id = (resolver.resolve(db, "wecom_agent_id") or "").strip()
        binding_source = "runtime_config" if corp_id and agent_id else None
    else:
        binding = (
            db.query(ThirdPartyOrganizationBinding)
            .filter(ThirdPartyOrganizationBinding.tenant_id == tenant_id)
            .first()
        )
        corp_id = binding.corp_id if binding is not None else None
        agent_id = binding.agent_id if binding is not None else None
        binding_source = "third_party_binding" if binding is not None else None
    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant_id)
        .first()
    )
    org = {
        "corp_name": tenant.name if tenant is not None else None,
        "corp_id": corp_id or None,
        "agent_id": agent_id or None,
        "source": binding_source,
    }
    missing: list[str] = []
    fields = {
        key: _field_snapshot(config, key, missing) for key in FIELD_KEYS
    }
    return {
        "org": org,
        "fields": fields,
        "missing": missing,
        "callback_url": _callback_url(),
    }


def _validate_archive_secret(value: str) -> str | None:
    if not value or any(ord(ch) < 32 for ch in value) or len(value) > 256:
        return "invalid_archive_secret"
    return None


def _validate_private_key(value: str) -> str | None:
    try:
        key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        return "invalid_private_key"
    if not isinstance(key, rsa.RSAPrivateKey):
        return "invalid_private_key"
    return None


def _validate_publickey_version(value: int) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return "invalid_publickey_version"
    return None


def _validate_callback_token(value: str) -> str | None:
    if (
        not value
        or len(value) > 64
        or any(ord(ch) <= 32 for ch in value)
        or any(ch in value for ch in '"\'<>&')
    ):
        return "invalid_callback_token"
    return None


def _validate_callback_aes_key(value: str) -> str | None:
    try:
        decode_aes_key(value)
    except CallbackConfigurationError:
        return "invalid_callback_aes_key"
    return None


def _validate_updates(updates: dict) -> dict[str, str | None]:
    """Return per-field validation codes (None = valid); no input values kept."""
    result: dict[str, str | None] = {}
    for key in (
        "archive_secret",
        "private_key",
        "callback_token",
        "callback_encoding_aes_key",
    ):
        if key in updates:
            result[key] = {
                "archive_secret": _validate_archive_secret,
                "private_key": _validate_private_key,
                "callback_token": _validate_callback_token,
                "callback_encoding_aes_key": _validate_callback_aes_key,
            }[key](updates[key])
    if "publickey_version" in updates:
        result["publickey_version"] = _validate_publickey_version(
            updates["publickey_version"]
        )
    # The pair must be configured together; blanks are skipped before this
    # check, so only an explicit single-sided submission reaches it.
    if updates.get("callback_token") and not updates.get("callback_encoding_aes_key"):
        result["callback_encoding_aes_key"] = "callback_pair_required"
    if updates.get("callback_encoding_aes_key") and not updates.get("callback_token"):
        result["callback_token"] = "callback_pair_required"
    return result


def _non_blank_updates(updates: dict) -> dict:
    """Drop blank values so they never overwrite existing secrets."""
    cleaned: dict = {}
    for key, value in updates.items():
        if key in RESERVED_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value
    return cleaned


def apply_config_updates(db: Session, tenant_id: str, updates: dict) -> dict:
    """Validate, persist, audit, and return the refreshed snapshot.

    Raises TenantConfigValidationError for per-field failures (mapped to a
    400 response by the router) and TenantConfigBindingMissingError when the
    trusted authorization binding is absent.
    """
    rejected = [key for key in updates if key in RESERVED_KEYS]
    if rejected:
        raise TenantConfigValidationError(
            [
                {"key": key, "message": "reserved configuration key", "code": "reserved_key"}
                for key in sorted(rejected)
            ]
        )
    cleaned = _non_blank_updates(updates)
    codes = _validate_updates(cleaned)
    errors = [
        {"key": key, "message": "invalid value", "code": code}
        for key, code in codes.items()
        if code is not None
    ]
    if errors:
        raise TenantConfigValidationError(errors)

    if get_app_edition() == APP_EDITION_SELFHOST:
        resolver = get_config_resolver()
        corp_id = (resolver.resolve(db, "wecom_corp_id") or "").strip()
        agent_id = (resolver.resolve(db, "wecom_agent_id") or "").strip()
        if not corp_id or not agent_id:
            raise TenantConfigBindingMissingError()
    else:
        binding = (
            db.query(ThirdPartyOrganizationBinding)
            .filter(ThirdPartyOrganizationBinding.tenant_id == tenant_id)
            .first()
        )
        if binding is None:
            raise TenantConfigBindingMissingError()
        corp_id, agent_id = binding.corp_id, binding.agent_id or ""

    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant_id)
        .first()
    )
    is_new = config is None
    if is_new:
        # app_secret is NOT NULL: the first save must include the archive
        # secret so the row is never created with placeholder credentials.
        if "archive_secret" not in cleaned:
            raise TenantConfigValidationError(
                [
                    {
                        "key": "archive_secret",
                        "message": "required for the first save",
                        "code": "required_first",
                    }
                ]
            )
        config = TenantWecomConfig(
            id=str(uuid4()),
            tenant_id=tenant_id,
            corp_id=corp_id,
            agent_id=agent_id,
            callback_domain="",
            is_active=True,
        )
        db.add(config)

    changed: list[str] = []
    if get_app_edition() == APP_EDITION_SELFHOST:
        if config.corp_id != corp_id:
            config.corp_id = corp_id
            changed.append("corp_id")
        if config.agent_id != agent_id:
            config.agent_id = agent_id
            changed.append("agent_id")
    if "archive_secret" in cleaned and "private_key" in cleaned:
        config.set_credentials(cleaned["archive_secret"], cleaned["private_key"])
        changed.extend(("archive_secret", "private_key"))
    elif "archive_secret" in cleaned:
        config.set_app_secret(cleaned["archive_secret"])
        changed.append("archive_secret")
    elif "private_key" in cleaned:
        config.set_private_key(cleaned["private_key"])
        changed.append("private_key")
    if "publickey_version" in cleaned:
        config.publickey_version = cleaned["publickey_version"]
        changed.append("publickey_version")
    if "callback_token" in cleaned and "callback_encoding_aes_key" in cleaned:
        config.set_callback_credentials(
            cleaned["callback_token"], cleaned["callback_encoding_aes_key"]
        )
        changed.extend(("callback_token", "callback_encoding_aes_key"))

    if changed:
        write_audit(
            db,
            tenant_id=tenant_id,
            action=AuditAction.CONFIG_CHANGED,
            object_type=AuditObjectType.TENANT_CONFIG,
            detail={"changed_keys": sorted(changed), "created": is_new},
        )
    db.commit()
    return config_snapshot(db, tenant_id)


_INVALID_CREDENTIAL_ERRCODES = {"40001", "40013", "40125"}


def connectivity_failure_reason(error: RuntimeError) -> str:
    """Map known token failures to safe, coarse response categories."""
    message = str(error)
    if message == "Failed to fetch WeCom access_token":
        return "network_error"
    if any(f"errcode={code}" in message for code in _INVALID_CREDENTIAL_ERRCODES):
        return "invalid_credentials"
    return "unknown"


def _local_field_check(config: TenantWecomConfig, key: str) -> tuple[bool, str | None]:
    """Local, offline verification of one stored field."""
    if _stored_value(config, key) in (None, ""):
        return False, "missing"
    try:
        if key == "archive_secret":
            _plaintext(config, key)
        elif key == "private_key":
            code = _validate_private_key(_plaintext(config, key))
            if code is not None:
                return False, code
        elif key == "publickey_version":
            if not config.publickey_version or config.publickey_version < 1:
                return False, "invalid_publickey_version"
        elif key == "callback_token":
            code = _validate_callback_token(_plaintext(config, key))
            if code is not None:
                return False, code
        elif key == "callback_encoding_aes_key":
            code = _validate_callback_aes_key(_plaintext(config, key))
            if code is not None:
                return False, code
    except FieldDecryptionError:
        return False, "unreadable"
    return True, None


def test_config(db: Session, tenant_id: str) -> dict:
    """Verify stored values locally and probe real WeCom connectivity.

    Never includes credentials or their contents in the result; each field is
    reported as ok plus a fixed safe error code.
    """
    config = (
        db.query(TenantWecomConfig)
        .filter(TenantWecomConfig.tenant_id == tenant_id)
        .first()
    )
    if config is None:
        fields = {key: {"ok": False, "safe_error_code": "missing"} for key in FIELD_KEYS}
        return {"all_ok": False, "fields": fields}

    fields: dict[str, dict] = {}
    for key in FIELD_KEYS:
        ok, code = _local_field_check(config, key)
        fields[key] = {"ok": ok, "safe_error_code": code}
    # Real connectivity probe using the stored archive secret; failures map to
    # the same coarse categories as the platform check (RND-312).
    if fields["archive_secret"]["ok"]:
        try:
            from app.auth import get_wecom_token

            get_wecom_token(
                config.corp_id,
                config.decrypted_app_secret,
                cache_key=f"provisioning-config-test:{tenant_id}",
            )
            fields["connectivity"] = {"ok": True, "safe_error_code": None}
        except RuntimeError as error:
            fields["connectivity"] = {
                "ok": False,
                "safe_error_code": connectivity_failure_reason(error),
            }
    else:
        fields["connectivity"] = {
            "ok": False,
            "safe_error_code": "skipped",
        }
    return {
        "all_ok": all(field["ok"] for field in fields.values()),
        "fields": fields,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
