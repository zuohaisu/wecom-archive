"""Controlled, tenant-scoped WeCom contact-avatar synchronization (RND-371).

Upstream avatar URLs are treated as untrusted, short-lived inputs. They are
never returned by an API, logged, or retained in the database: this module
accepts only documented WeCom CDN hosts over HTTPS (http:// URLs on those
hosts are upgraded to https before the allow-list check), verifies the
response before caching a small image through the existing private storage
boundary, and records only an opaque storage reference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth import get_wecom_token
from app.conversation_membership import _collect_staff_ids
from app.db.models import Contact, ExternalContact
from app.media_storage import (
    MediaStorageConfigurationError,
    MediaStorageOperationError,
    MediaStorageUnavailable,
    build_tenant_media_key,
    detect_image_type_from_bytes,
    get_configured_write_backend_name,
    get_media_storage_provider,
)
from app import wecom_contacts

logger = logging.getLogger(__name__)

AvatarKind = Literal["internal", "external"]

# The official WeCom samples return qlogo.cn URLs. The suffix allow-list is
# deliberately narrower than "any HTTPS URL": avatar data must never turn
# this worker into an arbitrary network client.
_ALLOWED_WECOM_AVATAR_HOST_SUFFIXES = ("qlogo.cn", "qpic.cn")
_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_AVATAR_TIMEOUT = httpx.Timeout(connect=3.0, read=7.0, write=3.0, pool=3.0)
_CONTENT_TYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_CONTENT_TYPE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class AvatarPresentation:
    """The only avatar information eligible for browser-facing schemas."""

    url: Optional[str]
    status: str
    synced_at: Optional[datetime]


@dataclass
class InternalAvatarSyncSummary:
    selected: int = 0
    ready: int = 0
    unavailable: int = 0


class AvatarFetchError(ValueError):
    """A bounded classification which deliberately contains no source URL."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_url(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _upgrade_wecom_http(url: str) -> str:
    """Upgrade http:// URLs on the trusted WeCom CDN hosts to https.

    WeCom's ``externalcontact/get`` returns external-contact avatar URLs as
    ``http://wx.qlogo.cn/...``, while this module's download path is strictly
    https-only. The same CDN also serves those images over https, so upgrade
    only the allow-listed hosts and leave every other URL untouched — the
    https-only + host allow-list boundary stays in force for all other
    sources.
    """
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() == "http" and any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _ALLOWED_WECOM_AVATAR_HOST_SUFFIXES
    ):
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return url


def _source_is_allowed(source_url: str) -> bool:
    parsed = urlsplit(source_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        return False
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _ALLOWED_WECOM_AVATAR_HOST_SUFFIXES
    )


def _download_avatar(source_url: str) -> tuple[bytes, str, str]:
    """Fetch one small, verified WeCom avatar without following redirects."""
    if not _source_is_allowed(source_url):
        raise AvatarFetchError("invalid")

    try:
        with httpx.Client(
            timeout=_AVATAR_TIMEOUT,
            follow_redirects=False,
            headers={"Accept": "image/jpeg,image/png,image/gif,image/webp"},
        ) as client:
            with client.stream("GET", source_url) as response:
                if response.status_code != 200:
                    raise AvatarFetchError("unavailable")
                header_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                expected_suffix = _CONTENT_TYPE_SUFFIXES.get(header_type)
                if expected_suffix is None:
                    raise AvatarFetchError("invalid")
                raw_length = response.headers.get("content-length")
                if raw_length:
                    try:
                        if int(raw_length) > _MAX_AVATAR_BYTES:
                            raise AvatarFetchError("invalid")
                    except ValueError:
                        # A malformed length cannot make the stream unbounded;
                        # the hard byte counter below remains authoritative.
                        pass
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_AVATAR_BYTES:
                        raise AvatarFetchError("invalid")
                    chunks.append(chunk)
    except AvatarFetchError:
        raise
    except httpx.HTTPError:
        raise AvatarFetchError("unavailable") from None

    data = b"".join(chunks)
    suffix = detect_image_type_from_bytes(data)
    if not data or suffix is None or suffix != expected_suffix:
        raise AvatarFetchError("invalid")
    return data, suffix, _CONTENT_TYPE_BY_SUFFIX[suffix]


def _clear_cached_avatar(profile: Contact | ExternalContact) -> None:
    old_backend = profile.avatar_storage_backend
    old_ref = profile.avatar_storage_ref
    if old_backend and old_ref:
        try:
            get_media_storage_provider(old_backend).delete(old_ref)
        except (MediaStorageConfigurationError, MediaStorageOperationError, MediaStorageUnavailable, OSError):
            # Removing an inaccessible object is best effort. Clearing the DB
            # reference below still fail-closes every browser request.
            pass
    profile.avatar_storage_backend = None
    profile.avatar_storage_ref = None
    profile.avatar_content_type = None


def _mark_avatar(profile: Contact | ExternalContact, *, source: str, status: str) -> None:
    _clear_cached_avatar(profile)
    profile.avatar_source = source
    profile.avatar_status = status
    profile.avatar_synced_at = _now()


def sync_avatar_from_source(
    profile: Contact | ExternalContact,
    *,
    tenant_id: str,
    identity_kind: AvatarKind,
    stable_identity_id: str,
    source: str,
    source_url: object,
) -> bool:
    """Cache an avatar or safely invalidate the previous cache entry.

    Every failure is represented in row state and returns ``False``. It is
    intentionally safe to call inside a contact-sync savepoint: an avatar
    outage must not roll back names, remarks, messages, or contact identity.
    """
    url = _clean_url(source_url)
    if not url:
        _mark_avatar(profile, source=source, status="missing")
        return False
    url = _upgrade_wecom_http(url)

    if not _source_is_allowed(url):
        _mark_avatar(profile, source=source, status="invalid")
        return False

    try:
        data, suffix, content_type = _download_avatar(url)
        backend = get_configured_write_backend_name()
        storage_ref = build_tenant_media_key(
            tenant_id,
            "avatars",
            f"{identity_kind}-{stable_identity_id}",
            suffix=suffix,
        )
        stored_ref = get_media_storage_provider(backend).save_bytes(storage_ref, data)
    except AvatarFetchError as exc:
        _mark_avatar(profile, source=source, status=exc.status)
        return False
    except (MediaStorageConfigurationError, MediaStorageOperationError, MediaStorageUnavailable, OSError, ValueError):
        _mark_avatar(profile, source=source, status="unavailable")
        return False

    old_backend = profile.avatar_storage_backend
    old_ref = profile.avatar_storage_ref
    profile.avatar_storage_backend = backend
    profile.avatar_storage_ref = stored_ref
    profile.avatar_content_type = content_type
    profile.avatar_source = source
    profile.avatar_status = "ready"
    profile.avatar_synced_at = _now()
    if old_backend and old_ref and (old_backend != backend or old_ref != stored_ref):
        try:
            get_media_storage_provider(old_backend).delete(old_ref)
        except (MediaStorageConfigurationError, MediaStorageOperationError, MediaStorageUnavailable, OSError):
            pass
    return True


def sync_external_contact_avatar(
    contact: ExternalContact, tenant_id: str, detail: dict
) -> bool:
    external = detail.get("external_contact") if isinstance(detail, dict) else None
    source_url = external.get("avatar") if isinstance(external, dict) else None
    return sync_avatar_from_source(
        contact,
        tenant_id=tenant_id,
        identity_kind="external",
        stable_identity_id=contact.external_userid,
        source="wecom_externalcontact",
        source_url=source_url,
    )


def sync_internal_contact_avatar(
    session: Session,
    tenant_id: str,
    wecom_userid: str,
    profile: wecom_contacts.MemberProfile,
) -> bool:
    """Persist an internal identity's current avatar without changing names."""
    contact = (
        session.query(Contact)
        .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid == wecom_userid)
        .first()
    )
    if contact is None:
        contact = Contact(
            tenant_id=tenant_id,
            wecom_userid=wecom_userid,
            # This is the same WeCom canonical name RND-130 uses only when
            # creating a previously unseen identity; never overwrite a
            # pre-existing display-name contract for an avatar refresh.
            name=profile.name,
        )
        session.add(contact)
        session.flush()

    if not profile.active:
        _mark_avatar(contact, source="wecom_member", status="inactive")
        return False
    return sync_avatar_from_source(
        contact,
        tenant_id=tenant_id,
        identity_kind="internal",
        stable_identity_id=wecom_userid,
        source="wecom_member",
        source_url=profile.avatar_url,
    )


def reconcile_internal_contact_avatars(
    session: Session,
    tenant_id: str,
    corp_id: str,
    oauth_secret: str,
    *,
    limit: int = 100,
) -> InternalAvatarSyncSummary:
    """Refresh a bounded, oldest-first subset of archive staff avatars."""
    summary = InternalAvatarSyncSummary()
    if not oauth_secret.strip():
        return summary
    try:
        token = get_wecom_token(corp_id, oauth_secret, cache_key=f"{corp_id}:member_avatar")
    except Exception:  # noqa: BLE001 -- credentials/provider failure is non-fatal background work
        summary.unavailable += 1
        return summary

    staff_ids = _collect_staff_ids(session, tenant_id)
    existing = {
        row.wecom_userid: row.avatar_synced_at
        for row in session.query(Contact.wecom_userid, Contact.avatar_synced_at)
        .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid.in_(staff_ids))
        .all()
    } if staff_ids else {}
    candidates = sorted(
        staff_ids,
        key=lambda uid: (
            existing.get(uid) is not None,
            existing.get(uid).timestamp() if existing.get(uid) is not None else 0,
            uid,
        ),
    )[: max(1, limit)]

    for wecom_userid in candidates:
        summary.selected += 1
        member = wecom_contacts.fetch_member_profile(token, wecom_userid)
        if member is None:
            summary.unavailable += 1
            continue
        if sync_internal_contact_avatar(session, tenant_id, wecom_userid, member):
            summary.ready += 1
    return summary


def _presentation(kind: AvatarKind, profile: Contact | ExternalContact | None) -> AvatarPresentation:
    if profile is None:
        return AvatarPresentation(url=None, status="missing", synced_at=None)
    status = profile.avatar_status if isinstance(profile.avatar_status, str) else "missing"
    synced_at = profile.avatar_synced_at if isinstance(profile.avatar_synced_at, datetime) else None
    ready = (
        status == "ready"
        and isinstance(profile.avatar_storage_backend, str)
        and bool(profile.avatar_storage_backend)
        and isinstance(profile.avatar_storage_ref, str)
        and bool(profile.avatar_storage_ref)
        and isinstance(profile.avatar_content_type, str)
        and bool(profile.avatar_content_type)
    )
    return AvatarPresentation(
        url=f"/api/admin/avatars/{kind}/{profile.id}" if ready else None,
        status="ready" if ready else status,
        synced_at=synced_at,
    )


def internal_avatar_presentations(
    session: Session, tenant_id: str, wecom_userids: Iterable[str]
) -> dict[str, AvatarPresentation]:
    ids = {value for value in wecom_userids if isinstance(value, str) and value}
    if not ids:
        return {}
    try:
        rows = session.query(Contact).filter(
            Contact.tenant_id == tenant_id, Contact.wecom_userid.in_(ids)
        ).all()
    except OperationalError:
        # A partially upgraded deployment must degrade image-only output,
        # not turn a contacts/timeline page into a 500. Readiness still
        # reports the schema mismatch before normal traffic is admitted.
        rows = []
    by_id = {row.wecom_userid: _presentation("internal", row) for row in rows}
    return {identity_id: by_id.get(identity_id, _presentation("internal", None)) for identity_id in ids}


def external_avatar_presentation(contact: ExternalContact) -> AvatarPresentation:
    return _presentation("external", contact)


def external_avatar_presentations(
    session: Session, tenant_id: str, external_userids: Iterable[str]
) -> dict[str, AvatarPresentation]:
    ids = {value for value in external_userids if isinstance(value, str) and value}
    if not ids:
        return {}
    try:
        rows = session.query(ExternalContact).filter(
            ExternalContact.tenant_id == tenant_id,
            ExternalContact.external_userid.in_(ids),
        ).all()
    except OperationalError:
        rows = []
    return {row.external_userid: _presentation("external", row) for row in rows}
