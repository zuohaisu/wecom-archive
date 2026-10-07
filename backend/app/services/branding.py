"""Tenant-isolated custom branding and managed-domain control-plane helpers.

RND-259 deliberately keeps customer-controlled data in the application database:
logo and favicon bytes are scoped by the owning ``tenant_branding`` row rather
than exposed as globally-addressable object-store keys.  Domain activation is a
strict, fail-closed gate: DNS ownership verification, trusted certificate
status, current entitlement, and an exact Host match are all required before a
custom host is accepted.

The managed edge/certificate controller is intentionally outside this module.
It reports its coarse lifecycle result through ``record_certificate_status``;
that function never accepts a certificate/private key and tenant-facing routes
cannot call it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse

from app.db.models import AdminSession, AdminUser, Tenant, TenantBranding
from app.services.entitlements import CUSTOM_BRANDING, CUSTOM_DOMAIN, has_entitlement
from app.settings import get_auth_settings, get_wecom_oauth_settings

MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_FAVICON_BYTES = 1 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 16_000_000

LOGO_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
FAVICON_MIME_BY_FORMAT = {**LOGO_MIME_BY_FORMAT, "ICO": "image/x-icon"}

_DOMAIN_STATES = frozenset({"pending_verification", "verified", "active"})
_CERTIFICATE_STATES = frozenset({"not_requested", "pending", "issued", "failed", "expired"})
_RESERVED_SUFFIXES = frozenset(
    {"localhost", "local", "internal", "test", "example", "invalid", "home", "corp", "lan"}
)
_DEFAULT_LOGO_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "brand" / "logo-horizontal.svg"
_DEFAULT_FAVICON_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "brand" / "favicon.svg"


class BrandingValidationError(ValueError):
    """A fixed, safe validation code suitable for an API response."""


class DomainConflictError(ValueError):
    """A hostname is already reserved by another tenant."""


class DomainStateError(ValueError):
    """A requested domain transition cannot safely occur."""


@dataclass(frozen=True)
class ImagePayload:
    mime_type: str
    content: bytes
    width: int
    height: int


@dataclass(frozen=True)
class DnsCheck:
    verified: bool
    reason: Optional[str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ascii_hostname(value: str) -> str:
    """Normalize an HTTP Host hostname, including a bracketed IPv6 literal.

    This permissive helper is only for identifying configured platform hosts.
    Customer custom domains always go through :func:`normalize_custom_hostname`.
    """
    candidate = value.strip().lower().rstrip(".")
    if not candidate or any(char.isspace() for char in candidate) or any(char in candidate for char in "/\\@,?#"):
        raise BrandingValidationError("invalid_host")
    if candidate.startswith("[") and candidate.endswith("]"):
        try:
            return str(ipaddress.IPv6Address(candidate[1:-1]))
        except ValueError as exc:
            raise BrandingValidationError("invalid_host") from exc
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    try:
        result = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BrandingValidationError("invalid_host") from exc
    if len(result) > 253 or not result:
        raise BrandingValidationError("invalid_host")
    labels = result.split(".")
    if any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
        for label in labels
    ):
        raise BrandingValidationError("invalid_host")
    return result


def hostname_from_host_header(value: str) -> str:
    """Parse one Host header without trusting a forwarded/client-provided URL."""
    raw = value.strip()
    if not raw:
        raise BrandingValidationError("invalid_host")
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            raise BrandingValidationError("invalid_host")
        suffix = raw[closing + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            raise BrandingValidationError("invalid_host")
        return _ascii_hostname(raw[: closing + 1])
    if raw.count(":") > 1:
        raise BrandingValidationError("invalid_host")
    host, separator, port = raw.partition(":")
    if separator and (not port or not port.isdigit()):
        raise BrandingValidationError("invalid_host")
    return _ascii_hostname(host)


def normalize_custom_hostname(value: str, *, platform_hosts: Iterable[str] = ()) -> str:
    """Return a canonical public hostname or reject unsafe/reserved input."""
    candidate = value.strip()
    if not candidate or candidate != candidate.rstrip(".") or ":" in candidate or "*" in candidate:
        raise BrandingValidationError("invalid_custom_domain")
    hostname = _ascii_hostname(candidate)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise BrandingValidationError("custom_domain_ip_not_allowed")
    labels = hostname.split(".")
    if len(labels) < 2 or hostname in _RESERVED_SUFFIXES or labels[-1] in _RESERVED_SUFFIXES:
        raise BrandingValidationError("custom_domain_reserved")
    normalized_platform_hosts = {_ascii_hostname(host) for host in platform_hosts}
    if any(hostname == host or hostname.endswith("." + host) for host in normalized_platform_hosts):
        raise BrandingValidationError("custom_domain_platform_reserved")
    return hostname


def platform_default_hosts() -> frozenset[str]:
    """Configured platform hosts, not values copied from an HTTP request."""
    raw = (get_wecom_oauth_settings().admin_domain or "").strip()
    production = get_auth_settings().app_env.strip().lower() == "production"
    hosts: set[str] = set()
    if raw:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return frozenset(hosts)
        if parsed.hostname:
            try:
                hosts.add(_ascii_hostname(parsed.hostname))
            except BrandingValidationError:
                pass
    # TestClient's default host and loopback hosts are development-only,
    # never public production fallbacks. An unconfigured production service
    # therefore rejects every Host instead of accidentally accepting localhost.
    if not production:
        hosts.update({"localhost", "127.0.0.1", "::1", "testserver"})
    return frozenset(hosts)


def platform_public_base_url() -> str:
    """Build a platform base URL only from configured deployment settings."""
    raw = (get_wecom_oauth_settings().admin_domain or "").strip()
    if not raw:
        raise BrandingValidationError("platform_domain_unconfigured")
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise BrandingValidationError("platform_domain_unconfigured")
    host = _ascii_hostname(parsed.hostname or "")
    scheme = parsed.scheme or "https"
    if scheme not in {"http", "https"}:
        raise BrandingValidationError("platform_domain_unconfigured")
    if scheme != "https" and get_auth_settings().app_env.strip().lower() == "production":
        raise BrandingValidationError("platform_domain_unconfigured")
    return f"{scheme}://{host}"


def verification_record_name(hostname: str) -> str:
    return f"_crowntime-verify.{hostname}"


def domain_fingerprint(hostname: str) -> str:
    """A non-reversible, short audit reference; never include a TXT token."""
    return hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:16]


def _image_payload(content: bytes, declared_mime: str, *, favicon: bool) -> ImagePayload:
    """Verify real image data then re-encode it without upload metadata.

    SVG is intentionally not supported in the first release: an SVG sanitizer
    is not a MIME/type check, and accepting one without a reviewed sanitizer
    would create an active-content upload surface.
    """
    maximum = MAX_FAVICON_BYTES if favicon else MAX_LOGO_BYTES
    if not content or len(content) > maximum:
        raise BrandingValidationError("favicon_file_too_large" if favicon else "logo_file_too_large")
    allowed = FAVICON_MIME_BY_FORMAT if favicon else LOGO_MIME_BY_FORMAT
    if declared_mime.lower() not in set(allowed.values()):
        raise BrandingValidationError("favicon_unsupported_media_type" if favicon else "logo_unsupported_media_type")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = image.format or ""
                width, height = image.size
                if image_format not in allowed or allowed[image_format] != declared_mime.lower():
                    raise BrandingValidationError("favicon_mime_mismatch" if favicon else "logo_mime_mismatch")
                if width < 1 or height < 1 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                    raise BrandingValidationError("favicon_dimensions_invalid" if favicon else "logo_dimensions_invalid")
                image.verify()
        with Image.open(BytesIO(content)) as image:
            image.load()
            normalized = image.copy()
    except BrandingValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, UnidentifiedImageError, ValueError) as exc:
        raise BrandingValidationError("favicon_invalid_image" if favicon else "logo_invalid_image") from exc

    result = BytesIO()
    if image_format == "JPEG":
        if normalized.mode not in {"RGB", "L"}:
            normalized = normalized.convert("RGB")
        normalized.save(result, format="JPEG", quality=90, optimize=False)
    elif image_format == "WEBP":
        normalized.save(result, format="WEBP", quality=90, method=4)
    elif image_format == "ICO":
        normalized.save(result, format="ICO")
    else:
        normalized.save(result, format="PNG", optimize=False)
    encoded = result.getvalue()
    if not encoded or len(encoded) > maximum:
        raise BrandingValidationError("favicon_file_too_large" if favicon else "logo_file_too_large")
    return ImagePayload(mime_type=allowed[image_format], content=encoded, width=width, height=height)


def validate_logo_upload(content: bytes, declared_mime: str) -> ImagePayload:
    return _image_payload(content, declared_mime, favicon=False)


def validate_favicon_upload(content: bytes, declared_mime: str) -> ImagePayload:
    return _image_payload(content, declared_mime, favicon=True)


def get_or_create_branding(db: Session, tenant_id: str) -> TenantBranding:
    config = db.scalar(select(TenantBranding).where(TenantBranding.tenant_id == tenant_id))
    if config is None:
        config = TenantBranding(id=secrets.token_hex(16), tenant_id=tenant_id)
        db.add(config)
        db.flush()
    return config


def get_branding(db: Session, tenant_id: str) -> TenantBranding | None:
    return db.scalar(select(TenantBranding).where(TenantBranding.tenant_id == tenant_id))


def set_logo(db: Session, tenant_id: str, payload: ImagePayload) -> TenantBranding:
    config = get_or_create_branding(db, tenant_id)
    config.logo_content = payload.content
    config.logo_mime_type = payload.mime_type
    config.logo_updated_at = utc_now()
    db.flush()
    return config


def clear_logo(db: Session, tenant_id: str) -> TenantBranding:
    config = get_or_create_branding(db, tenant_id)
    config.logo_content = None
    config.logo_mime_type = None
    config.logo_updated_at = utc_now()
    db.flush()
    return config


def set_favicon(db: Session, tenant_id: str, payload: ImagePayload) -> TenantBranding:
    config = get_or_create_branding(db, tenant_id)
    config.favicon_content = payload.content
    config.favicon_mime_type = payload.mime_type
    config.favicon_updated_at = utc_now()
    db.flush()
    return config


def clear_favicon(db: Session, tenant_id: str) -> TenantBranding:
    config = get_or_create_branding(db, tenant_id)
    config.favicon_content = None
    config.favicon_mime_type = None
    config.favicon_updated_at = utc_now()
    db.flush()
    return config


def configure_domain(db: Session, tenant_id: str, hostname: str) -> tuple[TenantBranding, str]:
    """Replace the tenant's one domain and return its one-time TXT value."""
    normalized = normalize_custom_hostname(hostname, platform_hosts=platform_default_hosts())
    conflict = db.scalar(
        select(TenantBranding.tenant_id).where(
            TenantBranding.custom_domain == normalized,
            TenantBranding.tenant_id != tenant_id,
        )
    )
    if conflict is not None:
        raise DomainConflictError("custom_domain_already_bound")
    raw_token = secrets.token_urlsafe(32)
    config = get_or_create_branding(db, tenant_id)
    config.custom_domain = normalized
    config.domain_state = "pending_verification"
    config.domain_enabled = False
    config.verification_token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    config.verification_requested_at = utc_now()
    config.verification_verified_at = None
    config.domain_last_checked_at = None
    config.domain_failure_code = None
    config.certificate_status = "not_requested"
    config.certificate_expires_at = None
    config.certificate_last_checked_at = None
    db.flush()
    return config, raw_token


def _doh_endpoint() -> str:
    # This is a fixed operator setting, never based on a customer hostname.
    # Cloudflare's standards-compatible DoH JSON endpoint is read-only.
    endpoint = "https://cloudflare-dns.com/dns-query"
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BrandingValidationError("dns_verifier_unavailable")
    return endpoint


def _txt_answer_values(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []
    values: list[str] = []
    for answer in answers:
        value = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(value, str):
            continue
        # A single TXT segment is JSON-style quoted by the DoH JSON API.
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        values.append(value)
    return values


def verify_dns_txt(hostname: str, token_hash: str) -> DnsCheck:
    """Read DNS through DoH and compare only SHA-256 digests in memory."""
    try:
        response = httpx.get(
            _doh_endpoint(),
            params={"name": verification_record_name(hostname), "type": "TXT"},
            headers={"Accept": "application/dns-json"},
            timeout=5.0,
            follow_redirects=False,
        )
        if response.status_code != 200:
            return DnsCheck(verified=False, reason="dns_unavailable")
        answers = _txt_answer_values(response.json())
    except Exception:
        # No exception text: a proxy/DNS client may include request data.
        return DnsCheck(verified=False, reason="dns_unavailable")
    for answer in answers:
        candidate_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        if hmac.compare_digest(candidate_hash, token_hash):
            return DnsCheck(verified=True, reason=None)
    return DnsCheck(verified=False, reason="dns_record_not_found")


def verify_domain_ownership(db: Session, tenant_id: str) -> tuple[TenantBranding, DnsCheck]:
    """Record a DNS check, including a safe failed result, before returning."""
    config = get_branding(db, tenant_id)
    if config is None or not config.custom_domain or not config.verification_token_hash:
        raise DomainStateError("custom_domain_not_configured")
    if config.domain_state != "pending_verification":
        raise DomainStateError("custom_domain_not_pending_verification")
    result = verify_dns_txt(config.custom_domain, config.verification_token_hash)
    config.domain_last_checked_at = utc_now()
    config.domain_failure_code = result.reason
    if result.verified:
        config.domain_state = "verified"
        config.verification_verified_at = utc_now()
        # The managed edge controller sees this pending state and requests a
        # certificate; tenant traffic remains disabled until it reports issued.
        config.certificate_status = "pending"
        config.domain_failure_code = None
    db.flush()
    return config, result


def enable_domain(db: Session, tenant_id: str) -> TenantBranding:
    config = get_branding(db, tenant_id)
    if config is None or not config.custom_domain:
        raise DomainStateError("custom_domain_not_configured")
    now = utc_now()
    if config.domain_state not in {"verified", "active"}:
        raise DomainStateError("custom_domain_not_verified")
    if config.certificate_status != "issued" or config.certificate_expires_at is None or _as_utc(config.certificate_expires_at) <= now:
        raise DomainStateError("custom_domain_certificate_not_ready")
    config.domain_state = "active"
    config.domain_enabled = True
    config.domain_failure_code = None
    db.flush()
    return config


def disable_domain(db: Session, tenant_id: str) -> TenantBranding:
    config = get_branding(db, tenant_id)
    if config is None or not config.custom_domain:
        raise DomainStateError("custom_domain_not_configured")
    config.domain_enabled = False
    if config.domain_state == "active":
        config.domain_state = "verified"
    db.flush()
    return config


def unbind_domain(db: Session, tenant_id: str) -> TenantBranding:
    config = get_branding(db, tenant_id)
    if config is None or not config.custom_domain:
        raise DomainStateError("custom_domain_not_configured")
    config.custom_domain = None
    config.domain_state = None
    config.domain_enabled = False
    config.verification_token_hash = None
    config.verification_requested_at = None
    config.verification_verified_at = None
    config.domain_last_checked_at = None
    config.domain_failure_code = None
    config.certificate_status = "not_requested"
    config.certificate_expires_at = None
    config.certificate_last_checked_at = None
    db.flush()
    return config


def record_certificate_status(
    db: Session,
    tenant_id: str,
    *,
    status: str,
    expires_at: datetime | None = None,
    failure_code: str | None = None,
) -> TenantBranding:
    """Persist one trusted managed-edge status without handling key material."""
    if status not in _CERTIFICATE_STATES - {"not_requested"}:
        raise BrandingValidationError("invalid_certificate_status")
    config = get_branding(db, tenant_id)
    if config is None or not config.custom_domain or config.domain_state not in _DOMAIN_STATES:
        raise DomainStateError("custom_domain_not_configured")
    now = utc_now()
    if status == "issued":
        if expires_at is None or _as_utc(expires_at) <= now:
            raise BrandingValidationError("invalid_certificate_expiry")
        failure_code = None
    elif expires_at is not None:
        raise BrandingValidationError("certificate_expiry_only_for_issued")
    if failure_code is not None and re.fullmatch(r"[a-z0-9_]{1,64}", failure_code) is None:
        raise BrandingValidationError("invalid_certificate_failure_code")
    config.certificate_status = status
    config.certificate_expires_at = _as_utc(expires_at) if expires_at else None
    config.certificate_last_checked_at = now
    config.domain_failure_code = failure_code
    if status in {"failed", "expired"}:
        # A stale/failed edge must never continue routing based on an old row.
        config.domain_enabled = False
        if config.domain_state == "active":
            config.domain_state = "verified"
    db.flush()
    return config


def effective_custom_domain_active(db: Session, config: TenantBranding | None, *, at: datetime | None = None) -> bool:
    if config is None or not config.custom_domain:
        return False
    now = _as_utc(at or utc_now())
    return bool(
        config.domain_state == "active"
        and config.domain_enabled
        and config.certificate_status == "issued"
        and config.certificate_expires_at is not None
        and _as_utc(config.certificate_expires_at) > now
        and has_entitlement(db, config.tenant_id, CUSTOM_DOMAIN, at=now)
    )


def active_tenant_for_custom_host(db: Session, hostname: str) -> str | None:
    """Resolve exactly one enabled hostname; unknown/invalid hosts return None."""
    try:
        normalized = _ascii_hostname(hostname)
    except BrandingValidationError:
        return None
    row = db.execute(
        select(TenantBranding, Tenant)
        .join(Tenant, Tenant.id == TenantBranding.tenant_id)
        .where(
            TenantBranding.custom_domain == normalized,
            Tenant.lifecycle_status == "active",
        )
    ).one_or_none()
    if row is None:
        return None
    config, _tenant = row
    return config.tenant_id if effective_custom_domain_active(db, config) else None


def tenant_for_active_session(db: Session, session_id: str | None) -> str | None:
    if not session_id:
        return None
    now = utc_now()
    row = db.execute(
        select(AdminSession.tenant_id)
        .join(AdminUser, AdminUser.id == AdminSession.admin_user_id)
        .where(
            AdminSession.id == session_id,
            AdminSession.expires_at > now,
            AdminSession.is_revoked.is_(False),
            AdminSession.session_scope == "admin",
            AdminUser.status == "active",
            AdminUser.tenant_id == AdminSession.tenant_id,
        )
    ).scalar_one_or_none()
    return row


def effective_logo_payload(db: Session, tenant_id: str | None) -> tuple[str, bytes]:
    if tenant_id and has_entitlement(db, tenant_id, CUSTOM_BRANDING):
        config = get_branding(db, tenant_id)
        if config and config.logo_content and config.logo_mime_type:
            return config.logo_mime_type, bytes(config.logo_content)
    return "image/svg+xml", _DEFAULT_LOGO_PATH.read_bytes()


def effective_favicon_payload(db: Session, tenant_id: str | None) -> tuple[str, bytes]:
    if tenant_id and has_entitlement(db, tenant_id, CUSTOM_BRANDING):
        config = get_branding(db, tenant_id)
        if config and config.favicon_content and config.favicon_mime_type:
            return config.favicon_mime_type, bytes(config.favicon_content)
    return "image/svg+xml", _DEFAULT_FAVICON_PATH.read_bytes()


def tenant_public_base_url(db: Session, tenant_id: str) -> str:
    """Return an approved base URL; never derive one from Host/X-Forwarded-Host."""
    config = get_branding(db, tenant_id)
    if effective_custom_domain_active(db, config):
        return f"https://{config.custom_domain}"
    return platform_public_base_url()


def branding_status(db: Session, tenant_id: str) -> dict[str, object]:
    config = get_branding(db, tenant_id)
    branding_entitled = has_entitlement(db, tenant_id, CUSTOM_BRANDING)
    domain_entitled = has_entitlement(db, tenant_id, CUSTOM_DOMAIN)
    return {
        "custom_branding_entitled": branding_entitled,
        "custom_domain_entitled": domain_entitled,
        "upgrade_required": not (branding_entitled and domain_entitled),
        "logo_configured": bool(config and config.logo_content),
        "favicon_configured": bool(config and config.favicon_content),
        "custom_domain": config.custom_domain if config else None,
        "domain_state": config.domain_state if config else None,
        "domain_enabled": bool(config and config.domain_enabled),
        "effective_domain_active": effective_custom_domain_active(db, config),
        "verification_record_name": verification_record_name(config.custom_domain) if config and config.custom_domain else None,
        "certificate_status": config.certificate_status if config and config.custom_domain else None,
        "certificate_expires_at": config.certificate_expires_at if config else None,
        "certificate_last_checked_at": config.certificate_last_checked_at if config else None,
        "domain_last_checked_at": config.domain_last_checked_at if config else None,
        "domain_failure_code": config.domain_failure_code if config else None,
        "updated_at": config.updated_at if config else None,
    }


def configure_domain_safely(db: Session, tenant_id: str, hostname: str) -> tuple[TenantBranding, str]:
    """Map a concurrent DB uniqueness collision to the public conflict code."""
    try:
        return configure_domain(db, tenant_id, hostname)
    except IntegrityError as exc:
        raise DomainConflictError("custom_domain_already_bound") from exc


class BrandingHostMiddleware:
    """Accept only configured platform or fully active customer Hosts.

    A reverse proxy remains responsible for TLS termination, but application
    routing cannot rely on that proxy being perfect.  This exact database
    lookup has no positive cache: a disable, expiry, certificate failure, or
    entitlement downgrade takes effect on the next request rather than after a
    tenant-ambiguous cache TTL.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host_headers = [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == b"host"
        ]
        if len(host_headers) != 1:
            await PlainTextResponse("Misdirected Request", status_code=421)(scope, receive, send)
            return
        try:
            hostname = hostname_from_host_header(host_headers[0])
        except BrandingValidationError:
            await PlainTextResponse("Misdirected Request", status_code=421)(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        if hostname in platform_default_hosts():
            state["branding_custom_domain_tenant_id"] = None
            await self.app(scope, receive, send)
            return

        db = None
        provider = None
        close_direct_session = False
        try:
            app = scope.get("app")
            factory = getattr(getattr(app, "state", None), "branding_session_factory", None)
            if factory is not None:
                db = factory()
                close_direct_session = True
            else:
                # Reuse FastAPI's database dependency so TestClient overrides
                # exercise the same host-routing code as production.
                from app.db.session import get_db

                dependency = getattr(app, "dependency_overrides", {}).get(get_db, get_db)
                provided = dependency()
                if isinstance(provided, Session):
                    db = provided
                else:
                    provider = provided
                    db = next(provider)
            tenant_id = active_tenant_for_custom_host(db, hostname)
        except Exception:
            tenant_id = None
        finally:
            if provider is not None:
                provider.close()
            elif db is not None and close_direct_session:
                db.close()
        if tenant_id is None:
            await PlainTextResponse("Misdirected Request", status_code=421)(scope, receive, send)
            return
        state["branding_custom_domain_tenant_id"] = tenant_id
        await self.app(scope, receive, send)
