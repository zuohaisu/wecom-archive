"""
Unified WeCom media acquisition pipeline (RND-147 image + RND-199
voice/video/file/emotion).

This is the SOLE implementation of candidate selection (fresh vs.
stale-"downloaded" repair), the media_files pending/downloaded/failed
state machine, the write-to-.part-then-atomically-publish safety
discipline, and the non-blocking concurrent-run lock pattern for every
message type this pipeline downloads — image included. There is
deliberately no second, image-specific copy of any of this: an earlier
revision of RND-199 kept image on its own separate, pre-existing script
(scripts/download_wecom_image_media_once.py) parameterized alongside this
generic module; independent QA flagged that as two parallel pipelines
implementing the same responsibilities (candidate selection, locking,
retry, stale repair, persistence) and required consolidation onto one.
scripts/download_wecom_media_once.py is the single CLI entry point built
on top of this module, for every supported type.

Reuses the same shared primitives image already used before this
consolidation: the storage provider abstraction (app.media_storage),
WeCom SDK media chunk retrieval (app.sdk.wecom_sdk), and the existing
media_files schema (no new columns).

emotion is a real, named WeCom message type but is classified UNSUPPORTED
in app.message_type_registry — an intentional, tested rendering contract
(tests/test_media_classification.py::test_classify_media_unsupported_msgtype)
this ticket does not change, since UI rendering is out of scope. Its
media bytes are ordinary images (stickers/animated emoji), though, so
this module downloads and stores it like any other supported type,
independent of app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES (which
must stay registry-gated to SUPPORTED/PARTIAL for the Qiniu migration
tool — see that constant's own guard) via _KEY_CATEGORY_OVERRIDES below.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Query, Session

from app import media_storage
from app.db.models import ArchiveMessage, MediaFile
from app.media_storage import (
    LocalStorageProvider,
    MediaStorageProvider,
    build_tenant_media_key,
    detect_media_signature_from_bytes,
)
from app.sdk import wecom_sdk

# WeCom msgtype -> the detect_media_signature_from_bytes() media_type
# category this pipeline must see for that msgtype's downloaded bytes to
# be accepted. image/voice/video/file bytes are detected as their own
# category; emotion's bytes are ordinary image files, so its accepted
# category is "image" even though the WeCom message type is "emotion" —
# content is never trusted from the msgtype/sdkfileid, only from the
# bytes themselves.
_SIGNATURE_CATEGORY_BY_MSGTYPE = {
    "image": "image",
    "voice": "voice",
    "video": "video",
    "file": "file",
    "emotion": "image",
}

# Object-key path segment for a msgtype this module downloads that is not
# already registered in app.media_storage.MEDIA_TYPE_KEY_CATEGORIES.
# emotion is UNSUPPORTED in MessageTypeRegistry (see module docstring), so
# it is deliberately kept out of that registry-gated table and given its
# own segment here instead.
_KEY_CATEGORY_OVERRIDES = {"emotion": "emotions"}

# The full set of msgtypes this module can download — image/voice/video/
# file (already SUPPORTED_MIGRATION_MEDIA_TYPES members) plus emotion.
GENERIC_DOWNLOAD_MSGTYPES = frozenset(_SIGNATURE_CATEGORY_BY_MSGTYPE)

_REPAIR_SCAN_BATCH_SIZE = 500


def key_category_for_msgtype(msgtype: str) -> str:
    """Object-key path segment for msgtype. Raises KeyError for a msgtype
    this module does not download — callers must only invoke this after
    confirming msgtype is in GENERIC_DOWNLOAD_MSGTYPES."""
    if msgtype in media_storage.MEDIA_TYPE_KEY_CATEGORIES:
        return media_storage.MEDIA_TYPE_KEY_CATEGORIES[msgtype]
    return _KEY_CATEGORY_OVERRIDES[msgtype]


# ---------------------------------------------------------------------------
# Candidate queries
# ---------------------------------------------------------------------------


def build_candidate_query(
    session: Session,
    tenant_id: str,
    msgtypes,
    retry: bool,
    since_ms: Optional[int] = None,
    newest_first: bool = False,
) -> Query:
    """Tenant-scoped query for rows eligible for a fresh download attempt,
    across every msgtype in `msgtypes` at once: decrypted successfully, a
    msgtype in `msgtypes`, and a non-empty sdkfileid. "Actionable" means no
    media_files row yet, an existing "pending" row (safe to resume — it
    never completed), or (only with retry=True) an existing "failed" row.
    Never includes "downloaded" rows — those are handled separately by
    build_downloaded_repair_query so a servability re-check can decide
    whether they need repair.

    The outer join is deliberate (not an inner join later filtered down):
    tenant_id must be part of the JOIN's ON clause so a message with no
    media row at all still matches (as a NULL MediaFile side) and is
    treated as a fresh candidate, while a stray media_files row belonging
    to a different tenant sharing this archive_message_id never
    participates.

    since_ms, when set, restricts to ArchiveMessage.msgtime >= since_ms
    (epoch-ms) — recency filtering so a run's --limit budget isn't
    consumed by old, possibly platform-expired candidates. newest_first
    orders by msgtime descending (id descending as a stable tie-breaker)
    instead of the default ascending-id order.
    """
    query = (
        session.query(ArchiveMessage)
        .outerjoin(
            MediaFile,
            and_(
                MediaFile.archive_message_id == ArchiveMessage.id,
                MediaFile.tenant_id == tenant_id,
            ),
        )
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtype.in_(msgtypes),
            ArchiveMessage.sdkfileid.isnot(None),
            ArchiveMessage.sdkfileid != "",
        )
    )
    eligible_statuses = ["pending", "failed"] if retry else ["pending"]
    query = query.filter(
        or_(MediaFile.id.is_(None), MediaFile.download_status.in_(eligible_statuses))
    )
    if since_ms is not None:
        query = query.filter(ArchiveMessage.msgtime >= since_ms)
    if newest_first:
        return query.order_by(ArchiveMessage.msgtime.desc(), ArchiveMessage.id.desc())
    return query.order_by(ArchiveMessage.id)


def build_downloaded_repair_query(session: Session, tenant_id: str, msgtypes) -> Query:
    """Tenant-scoped query for rows currently marked "downloaded" across
    every msgtype in `msgtypes` — candidates for the servability re-check:
    a downloaded row whose file is missing, outside the storage root, or
    has a disallowed extension must be treated as stale and repairable,
    not skipped forever just because its status says "downloaded"."""
    return (
        session.query(ArchiveMessage, MediaFile)
        .join(MediaFile, MediaFile.archive_message_id == ArchiveMessage.id)
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            MediaFile.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtype.in_(msgtypes),
            ArchiveMessage.sdkfileid.isnot(None),
            ArchiveMessage.sdkfileid != "",
            MediaFile.download_status == "downloaded",
        )
        .order_by(ArchiveMessage.id)
    )


def is_downloaded_media_file_stale(media_file) -> bool:
    """True when a "downloaded" media_files row claims to be servable but
    its file is not — missing, outside the configured storage root, or a
    disallowed extension — i.e. this row is stale/broken and safe to
    repair. Reuses media_storage.resolve_downloadable_media_state, the
    same per-row-backend-aware predicate the media route uses, so "stale"
    here means precisely "the media route could not serve this file". A
    MediaStorageUnavailable/MediaStorageConfigurationError (the provider
    could not confirm either way — e.g. a transient outage during the
    scan) is treated as "not stale": this repair scan must never reset a
    perfectly fine row just because the backend was briefly unreachable at
    scan time."""
    try:
        return media_storage.resolve_downloadable_media_state(media_file) != "servable"
    except (media_storage.MediaStorageUnavailable, media_storage.MediaStorageConfigurationError):
        return False


def _scan_for_stale_downloaded(
    session: Session,
    tenant_id: str,
    msgtypes,
    needed: int,
    batch_size: int = _REPAIR_SCAN_BATCH_SIZE,
) -> List[Tuple[ArchiveMessage, MediaFile]]:
    """Scan "downloaded" rows in ascending-id batches of `batch_size`,
    servability-checking each one, until `needed` stale rows are found or
    the table is exhausted.

    `needed` (the caller's remaining --limit budget) must never be used as
    the SQL LIMIT for this query — doing so would silently drop any stale
    row that happens to sort after enough valid/servable "downloaded" rows
    to fill that limit, so a genuinely broken row could be skipped forever
    no matter how many times this runs. Instead, `batch_size` is a fixed
    internal fetch size independent of `needed`; the servability filter
    runs in Python on each batch, and only rows that fail it ever count
    against `needed`. Pagination (rather than one unbounded fetch) keeps
    memory bounded for tenants with a large "downloaded" set."""
    stale: List[Tuple[ArchiveMessage, MediaFile]] = []
    if needed <= 0:
        return stale

    last_id = 0
    while len(stale) < needed:
        batch = (
            build_downloaded_repair_query(session, tenant_id, msgtypes)
            .filter(ArchiveMessage.id > last_id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for msg, media_file in batch:
            if is_downloaded_media_file_stale(media_file):
                stale.append((msg, media_file))
                if len(stale) >= needed:
                    break
        last_id = batch[-1][0].id
        if len(batch) < batch_size:
            break
    return stale


def select_candidates(
    session: Session,
    tenant_id: str,
    msgtypes,
    retry: bool,
    limit: int,
    since_ms: Optional[int] = None,
    newest_first: bool = False,
) -> Tuple[List[ArchiveMessage], List[Tuple[ArchiveMessage, MediaFile]], int]:
    """Return (actionable, stale_repairs, total_eligible).

    actionable: fresh-download candidates (see build_candidate_query),
    filtered by since_ms and ordered per newest_first, up to `limit`.
    stale_repairs: (message, media_file) pairs currently marked
    "downloaded" that fail the servability check, filling whatever budget
    remains after `actionable` — capped so the repair scan can never push
    the total above `limit` nor crowd out fresh candidates.
    total_eligible: count of *all* fresh candidates ignoring both `limit`
    and since_ms (for --count-only reporting)."""
    total_eligible = build_candidate_query(session, tenant_id, msgtypes, retry).count()
    actionable = (
        build_candidate_query(
            session, tenant_id, msgtypes, retry, since_ms=since_ms, newest_first=newest_first
        )
        .limit(limit)
        .all()
    )

    remaining_budget = limit - len(actionable)
    stale_repairs = _scan_for_stale_downloaded(session, tenant_id, msgtypes, remaining_budget)

    return actionable, stale_repairs, total_eligible


def count_candidates_with_existing_media_row(session: Session, tenant_id: str, msgtypes) -> int:
    """Count of tenant-scoped, decrypted messages (msgtype in `msgtypes`)
    that already have *any* media_files row for this tenant (any
    download_status) — safe, aggregate-only visibility for --count-only
    reporting; never touches sdkfileid/paths."""
    return (
        session.query(ArchiveMessage)
        .join(MediaFile, MediaFile.archive_message_id == ArchiveMessage.id)
        .filter(
            ArchiveMessage.tenant_id == tenant_id,
            MediaFile.tenant_id == tenant_id,
            ArchiveMessage.decrypt_status == "success",
            ArchiveMessage.msgtype.in_(msgtypes),
            ArchiveMessage.sdkfileid.isnot(None),
            ArchiveMessage.sdkfileid != "",
        )
        .count()
    )


# ---------------------------------------------------------------------------
# Storage references and media_files state transitions
# ---------------------------------------------------------------------------


def target_storage_refs(tenant_id: str, msgtype: str, archive_message_id: int) -> Tuple[str, str]:
    """Return (base_ref_without_extension, part_ref) under
    tenants/<tenant_id>/<category>/ where category is msgtype's own
    key-category segment (see key_category_for_msgtype). Deterministic per
    (tenant_id, msgtype, archive_message_id): a retried download of the
    same message overwrites the same objects instead of accumulating
    duplicates. The .part ref is fixed regardless of the eventual detected
    extension so on-failure cleanup is unambiguous."""
    category = key_category_for_msgtype(msgtype)
    base = build_tenant_media_key(tenant_id, category, str(archive_message_id))
    part_ref = build_tenant_media_key(tenant_id, category, str(archive_message_id), suffix=".part")
    return base, part_ref


def get_or_reset_media_file(
    session: Session, tenant_id: str, sdkfileid: str, archive_message_id: int
) -> Optional[MediaFile]:
    """Return the media_files row for (tenant_id, sdkfileid), creating it
    (or resetting an existing pending/failed/stale-downloaded row) to
    download_status="pending" before an attempt begins — msgtype-
    independent, since a media_files row's identity is (tenant_id,
    sdkfileid), never the message type.

    media_files.sdkfileid is unique per tenant, not globally — the lookup
    is scoped by tenant_id. An existing row is only ever safe to reuse
    when its archive_message_id already matches the message about to be
    processed; if it differs (an inconsistent (msg, media_file) pairing
    within the same tenant), reusing it would silently reassign another
    message's media row, so this returns None. Callers must treat None as
    "fail this candidate safely" and must not touch the conflicting row."""
    row = (
        session.query(MediaFile)
        .filter(MediaFile.tenant_id == tenant_id, MediaFile.sdkfileid == sdkfileid)
        .first()
    )
    if row is None:
        row = MediaFile(
            tenant_id=tenant_id,
            sdkfileid=sdkfileid,
            archive_message_id=archive_message_id,
            download_status="pending",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    if row.archive_message_id != archive_message_id:
        return None

    row.download_status = "pending"
    row.local_path = None
    row.oss_key = None
    row.storage_backend = None
    row.storage_ref = None
    row.file_size = None
    session.commit()
    session.refresh(row)
    return row


def _safe_delete(provider: MediaStorageProvider, storage_ref: Optional[str]) -> None:
    """Best-effort remove; never raises, never prints the storage ref."""
    provider.delete(storage_ref)


def download_one(
    lib,
    handle,
    storage: MediaStorageProvider | Path,
    tenant_id: str,
    archive_message_id: int,
    msgtype: str,
    sdkfileid: str,
    timeout: int,
) -> Tuple[str, Optional[str], Optional[int]]:
    """Download one message's media.

    Returns (outcome, detail, file_size): outcome is "downloaded" or
    "failed"; detail is the final storage reference (string) when
    downloaded, or a short internal diagnostic tag (never an
    identifier/path/payload fragment) when failed; file_size is the exact
    byte length of the downloaded payload (len(data)) on success, else
    None.

    file_size is computed from the in-memory payload *before* upload, not
    by a post-publish remote provider.size_bytes() stat call — the caller
    must persist this value directly rather than re-querying the provider
    on the success path. A transient stat/metadata failure after a
    successful upload must never be able to look like an upload failure
    and trigger deletion of the just-published object; not calling stat()
    at all on this path is what guarantees that.

    The byte-signature gate accepts any allow-listed signature whose
    detected category (from the bytes themselves, never the caller-
    supplied msgtype/sdkfileid) matches _SIGNATURE_CATEGORY_BY_MSGTYPE[msgtype]
    — e.g. a "voice" message's downloaded bytes must actually look like a
    voice format, not merely *some* recognized format. The object key's
    category segment is msgtype-specific (see target_storage_refs).

    The .part temp file is always cleaned up on any failure path — a
    single try/finally guard covers download errors, write failures,
    unsupported byte signatures, and rename failures alike."""
    provider: MediaStorageProvider
    if isinstance(storage, MediaStorageProvider):
        provider = storage
    else:
        provider = LocalStorageProvider(storage)

    base_ref, part_ref = target_storage_refs(tenant_id, msgtype, archive_message_id)
    allowed_category = _SIGNATURE_CATEGORY_BY_MSGTYPE[msgtype]
    outcome = "failed"
    detail: Optional[str] = "unknown_error"
    file_size: Optional[int] = None

    try:
        try:
            chunks = bytearray()
            for chunk in wecom_sdk.iter_media_chunks(lib, handle, sdkfileid, timeout=timeout):
                chunks.extend(chunk)
            data = bytes(chunks)
        except wecom_sdk.SdkMediaError:
            detail = "sdk_error"
            return outcome, detail, file_size
        except Exception:
            detail = "download_error"
            return outcome, detail, file_size

        if not data:
            detail = "empty_payload"
            return outcome, detail, file_size

        try:
            part_ref = provider.save_bytes(part_ref, data)
        except OSError:
            detail = "write_error"
            return outcome, detail, file_size
        except ValueError:
            detail = "write_error"
            return outcome, detail, file_size

        match = detect_media_signature_from_bytes(data)
        if match is None or match.media_type != allowed_category:
            detail = "unsupported_type"
            return outcome, detail, file_size

        final_ref = f"{base_ref}{match.extension}"
        try:
            final_ref = provider.replace(part_ref, final_ref)
        except (FileNotFoundError, OSError, ValueError):
            detail = "rename_error"
            return outcome, detail, file_size

        outcome, detail, file_size = "downloaded", final_ref, len(data)
        return outcome, detail, file_size
    finally:
        if outcome != "downloaded":
            _safe_delete(provider, part_ref)
