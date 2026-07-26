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
#
# RND-202: audio_archive (meeting_voice_call recordings) is downloaded
# through this exact SAME pipeline — the enterprise call-recording bytes
# WeCom's GetMediaData returns are ordinary voice-format audio (AMR/SILK/
# WAV/MP3), the same formats a regular "voice" message uses, so the
# accepted signature category is "voice" even though the WeCom message
# type is "audio_archive" — mirrors the emotion/image precedent above
# exactly. The object-key path segment stays distinct (see
# app.media_storage.MEDIA_TYPE_KEY_CATEGORIES["audio_archive"]) so call
# recordings never share a directory with regular voice messages on disk.
_SIGNATURE_CATEGORY_BY_MSGTYPE = {
    "image": "image",
    "voice": "voice",
    "video": "video",
    "file": "file",
    "emotion": "image",
    "audio_archive": "voice",
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

# RND-200: outer msgtypes whose media lives in *nested* items rather than
# ArchiveMessage.sdkfileid — see iter_nested_media_refs /
# build_nested_media_candidate_query below. Never a member of
# GENERIC_DOWNLOAD_MSGTYPES: a mixed/chatrecord message has no single
# top-level sdkfileid for build_candidate_query's existing scalar-column
# filter to match.
NESTED_MEDIA_MSGTYPES = frozenset({"mixed", "chatrecord"})

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
# Nested media (mixed/chatrecord, RND-200)
#
# A mixed/chatrecord ArchiveMessage row has no single sdkfileid — it can
# embed any number of media-bearing nested items, each with its own
# sdkfileid, discovered only by walking structured_content (populated by
# app.structured_message_parser.parse_mixed_message /
# parse_chatrecord_message at decrypt time). build_candidate_query above
# cannot select these rows (its ArchiveMessage.sdkfileid.isnot(None)
# filter is always false for these two msgtypes), so a parallel, item-
# level candidate path is needed — reusing every other primitive in this
# module unchanged (get_or_reset_media_file, download_one, the storage
# provider, the pending/downloaded/failed state machine) rather than a
# second implementation.
# ---------------------------------------------------------------------------


def iter_nested_media_refs(structured_content) -> List[dict]:
    """Extract the media_refs list a mixed/chatrecord message's
    structured_content carries (see parse_mixed_message/
    parse_chatrecord_message) — a flat list of {"path","type","sdkfileid"}
    for every media-bearing nested item, regardless of nesting depth.

    Tolerates missing/malformed structured_content (returns []) and
    silently drops any entry that isn't a well-formed ref with a type this
    module knows how to download — never raises, since this runs over
    historical rows a future parser version may have shaped slightly
    differently."""
    if not isinstance(structured_content, dict):
        return []
    refs = structured_content.get("media_refs")
    if not isinstance(refs, list):
        return []
    out: List[dict] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        sdkfileid = ref.get("sdkfileid")
        item_type = ref.get("type")
        path = ref.get("path")
        if (
            isinstance(sdkfileid, str)
            and sdkfileid
            and isinstance(path, str)
            and path
            and item_type in _SIGNATURE_CATEGORY_BY_MSGTYPE
        ):
            out.append({"path": path, "type": item_type, "sdkfileid": sdkfileid})
    return out


def build_nested_media_candidate_query(
    session: Session, tenant_id: str, since_ms: Optional[int] = None
) -> Query:
    """Tenant-scoped coarse candidate query for mixed/chatrecord messages
    that may still have undownloaded nested media — decrypted successfully,
    msgtype in NESTED_MEDIA_MSGTYPES, structured_content present. The
    precise per-item eligibility check (which nested sdkfileids still lack
    a media_files row) happens in Python via iter_nested_media_refs +
    get_or_reset_media_file, the same coarse-SQL-then-precise-Python split
    build_missing_recipient_repair_query uses in
    scripts/decrypt_wecom_messages_once.py — a JSONB-path SQL query here
    would need to special-case arbitrary nesting depth, whereas the
    already-recursive Python parser output does not.

    since_ms mirrors build_candidate_query's own recency filter
    (ArchiveMessage.msgtime >= since_ms) — unlike that function, there is
    no newest_first ordering option here: select_nested_media_candidates
    paginates this query by ascending id for bounded-memory batching (see
    its docstring), and reordering by msgtime desc would break that
    cursor. Always ascending-id; a caller that needs newest-first nested
    processing is not yet supported (--newest-first is a documented no-op
    for the nested pass — see scripts/download_wecom_media_once.py)."""
    query = session.query(ArchiveMessage).filter(
        ArchiveMessage.tenant_id == tenant_id,
        ArchiveMessage.decrypt_status == "success",
        ArchiveMessage.msgtype.in_(NESTED_MEDIA_MSGTYPES),
        ArchiveMessage.structured_content.isnot(None),
    )
    if since_ms is not None:
        query = query.filter(ArchiveMessage.msgtime >= since_ms)
    return query.order_by(ArchiveMessage.id)


def select_nested_media_candidates(
    session: Session,
    tenant_id: str,
    limit: int,
    retry: bool = False,
    since_ms: Optional[int] = None,
    batch_size: int = _REPAIR_SCAN_BATCH_SIZE,
) -> Tuple[List[Tuple[ArchiveMessage, dict]], int]:
    """Return (item_candidates, total_messages_scanned).

    item_candidates is a flat list of (message, ref) pairs — one entry per
    still-eligible nested media item, up to `limit` items (not messages: a
    single mixed message with 5 images contributes up to 5 entries).
    "Eligible" mirrors build_candidate_query's own pending/failed
    semantics exactly: a sdkfileid with no media_files row yet, or an
    existing "pending" row, is always eligible; an existing "failed" row
    is eligible only when retry=True; a "downloaded" row is never
    eligible. Computed in Python since it must be checked per nested
    sdkfileid, not per message.

    Coarse candidate messages are fetched in ascending-id batches of
    `batch_size` (mirroring _scan_for_stale_downloaded's own pagination
    rationale) instead of one unbounded .all() — cost scales with `limit`
    reached, not with the tenant's total mixed/chatrecord history.
    total_messages_scanned is a separate, cheap .count() over the same
    (unpaginated, unlimited) coarse query — for --count-only reporting,
    matching build_candidate_query/select_candidates' own
    count()-for-total vs. limit()-for-actionable split.

    Two nested items sharing one sdkfileid (a duplicate reference within
    one message, or the identical file forwarded into two different
    messages within the SAME batch) are deduplicated within this call —
    only the first occurrence is ever returned as a candidate, so a
    retried/redundant download never overwrites the one media_files row
    that sdkfileid can have. A cross-message duplicate whose second
    occurrence falls in a LATER batch, or a later call entirely, still
    surfaces as get_or_reset_media_file's existing "media_identity_
    conflict" outcome (archive_message_id mismatch) — a pre-existing,
    schema-level limit of one archive_message_id owner per
    (tenant_id, sdkfileid) this function does not attempt to lift."""
    total_messages_scanned = build_nested_media_candidate_query(session, tenant_id, since_ms).count()

    item_candidates: List[Tuple[ArchiveMessage, dict]] = []
    seen_sdkfileids: set = set()
    last_id = 0

    while len(item_candidates) < limit:
        batch = (
            build_nested_media_candidate_query(session, tenant_id, since_ms)
            .filter(ArchiveMessage.id > last_id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        batch_refs_by_msg = [(msg, iter_nested_media_refs(msg.structured_content)) for msg in batch]
        batch_sdkfileids = {
            ref["sdkfileid"] for _msg, refs in batch_refs_by_msg for ref in refs
        }
        existing_status_by_sdkfileid = {}
        if batch_sdkfileids:
            existing_status_by_sdkfileid = dict(
                session.query(MediaFile.sdkfileid, MediaFile.download_status).filter(
                    MediaFile.tenant_id == tenant_id,
                    MediaFile.sdkfileid.in_(batch_sdkfileids),
                )
            )

        for msg, refs in batch_refs_by_msg:
            for ref in refs:
                sdkfileid = ref["sdkfileid"]
                if sdkfileid in seen_sdkfileids:
                    continue
                status = existing_status_by_sdkfileid.get(sdkfileid)
                if status == "downloaded":
                    continue
                if status == "failed" and not retry:
                    continue
                seen_sdkfileids.add(sdkfileid)
                item_candidates.append((msg, ref))
                if len(item_candidates) >= limit:
                    return item_candidates, total_messages_scanned

        last_id = batch[-1].id
        if len(batch) < batch_size:
            break

    return item_candidates, total_messages_scanned


# ---------------------------------------------------------------------------
# Storage references and media_files state transitions
# ---------------------------------------------------------------------------


def target_storage_refs(
    tenant_id: str, msgtype: str, archive_message_id: int, item_key: Optional[str] = None
) -> Tuple[str, str]:
    """Return (base_ref_without_extension, part_ref) under
    tenants/<tenant_id>/<category>/ where category is msgtype's own
    key-category segment (see key_category_for_msgtype). Deterministic per
    (tenant_id, msgtype, archive_message_id, item_key): a retried download
    of the same message (and, for a nested item, the same item_key)
    overwrites the same objects instead of accumulating duplicates. The
    .part ref is fixed regardless of the eventual detected extension so
    on-failure cleanup is unambiguous.

    item_key (RND-200) distinguishes multiple nested media items belonging
    to the same mixed/chatrecord archive_message_id — e.g. two images in
    one message must not resolve to the same object key. None (every
    existing non-composite caller) preserves the original identifier
    exactly, so this is fully backward compatible."""
    category = key_category_for_msgtype(msgtype)
    identifier = str(archive_message_id) if item_key is None else f"{archive_message_id}_{item_key}"
    base = build_tenant_media_key(tenant_id, category, identifier)
    part_ref = build_tenant_media_key(tenant_id, category, identifier, suffix=".part")
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
    item_key: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[int]]:
    """Download one message's media.

    item_key (RND-200): pass the nested item's path (see
    iter_nested_media_refs) when downloading a mixed/chatrecord nested
    media item, so its object key does not collide with a sibling item's —
    see target_storage_refs. msgtype here must be the nested item's own
    effective type (e.g. "image"), never the outer "mixed"/"chatrecord" —
    the byte-signature gate below indexes _SIGNATURE_CATEGORY_BY_MSGTYPE by
    msgtype and has no entry for either composite type. None (default)
    preserves existing non-composite callers unchanged.

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

    base_ref, part_ref = target_storage_refs(tenant_id, msgtype, archive_message_id, item_key=item_key)
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
