"""
Core application logic for the one-shot decrypt/normalise worker (RND-222).

Extracted from scripts/decrypt_wecom_messages_once.py so it can be called
and tested directly, independent of env parsing, SDK lifecycle, and CLI
printing/exit codes (all of which stay in the script — see its module
docstring). scripts/decrypt_wecom_messages_once.py re-exports every name
here for backward compatibility with existing test imports.

Tenant scope (RND-222 audit fix): run_decrypt_once() takes tenant_id as a
required positional parameter — never optional, never None. The pending/
failed record scan, the recipient-repair scan, the revoke-reconciliation
repair scan, and the post-commit pending_remaining count are ALL filtered
to this tenant_id. Before this fix, this worker was the one archive-side
component that scanned every tenant's pending/failed rows in a single run
— a multi-tenant deployment would RSA-decrypt other tenants' rows with
this corp's private key, which cannot succeed, permanently marking them
"failed" (see the RND-222 ticket's tenant scope audit for the full
analysis). Production is currently single-tenant, so this fix does not
change observed output there; see the ticket for the declared new failure
mode (TenantWecomConfig missing now fails the whole run, matching sync
and media's existing behavior).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient
from app.message_type_registry import ParserStrategy, get_parser_strategy
from app.revoke_reconciliation import (
    reconcile_pending_revocations,
    reconcile_revoke_event,
)
from app.sdk import wecom_sdk as _default_sdk
from app.structured_message_parser import parse_structured_content


class DecryptCommitError(Exception):
    """Raised when the final session.commit() fails. Carries the original
    exception's message so the shell can print it verbatim; never raised
    for any other failure in this module (every other error is counted
    into the returned summary instead — see module docstring)."""


@dataclass
class DecryptRunSummary:
    scanned: int = 0
    success: int = 0
    failed: int = 0
    unsupported: int = 0
    pending_remaining: int = 0
    key_mismatch: int = 0
    rsa_failed: int = 0
    recipient_upsert_failed: int = 0
    recipients_repaired: int = 0
    revoke_event_seen: int = 0
    revoke_reconcile_failed: int = 0
    revocations_reconciled: int = 0
    return_codes: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RSA decrypt
# ---------------------------------------------------------------------------


def _rsa_decrypt_encrypt_key(
    private_key: rsa.RSAPrivateKey, encrypt_random_key: str
) -> "str | None":
    """RSA-decrypt the WeCom encrypt_random_key field.

    WeCom base64-encodes the RSA ciphertext.  The C SDK demo uses PKCS#1 v1.5
    padding.  Returns the UTF-8 decrypted encrypt_key.

    Returns None on failure.
    """
    try:
        ciphertext = base64.b64decode(encrypt_random_key)
        plaintext = private_key.decrypt(
            ciphertext,
            padding.PKCS1v15(),
        )
        return plaintext.decode("utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Decrypt helpers (C SDK)
# ---------------------------------------------------------------------------


def _decrypt_message(
    lib, encrypt_key: str, encrypt_msg: str, sdk=_default_sdk
) -> "tuple[int, str | None]":
    """
    Call the C SDK DecryptData and return (return_code, decrypted_json_string).

    On success (return_code == 0), the second element is the decrypted JSON
    string.  On failure, the second element is None.

    sdk defaults to the real app.sdk.wecom_sdk module and is trailing/
    keyword so existing callers that predate this parameter — e.g.
    scripts/backfill_revoke_associations_once.py's
    `_decrypt_message(lib, encrypt_key, row.encrypt_chat_msg)` — keep
    working unchanged, always against the real SDK.
    """
    slice_ptr = sdk.new_slice(lib)
    if not slice_ptr:
        return -2, None  # custom error: alloc failed

    try:
        ret = sdk.decrypt_data(lib, encrypt_key, encrypt_msg, slice_ptr)
        if ret != 0:
            return ret, None

        slice_len = sdk.get_slice_len(lib, slice_ptr)
        if slice_len <= 0:
            return ret, None  # success but empty — treat as no data

        raw = sdk.get_content_from_slice(lib, slice_ptr)
        if raw is None:
            return ret, None

        return ret, raw.decode("utf-8")
    finally:
        try:
            sdk.free_slice(lib, slice_ptr)
        except Exception:
            pass


def _normalise_fields(
    decrypted: dict,
) -> dict:
    """
    Extract normalised fields from the decrypted WeCom message dict.

    Expected WeCom decrypted JSON structure:
    {
        "msgtype": "text",
        "from": "userid",
        "tolist": ["userid1", "userid2"],
        "roomid": "room_id_or_empty",
        "msgtime": 1779901200000,
        "text": {"content": "message body"}
    }

    For non-text messages, msgtype is a different string and the payload
    field varies (e.g. "image" -> {"image": {"md5sum": ...}}).

    RND-196: which extraction to run is now selected by asking
    app.message_type_registry which ParserStrategy msgtype maps to,
    instead of a literal `msgtype == "text"` check — the extraction logic
    itself (below) is unchanged; only the dispatch is registry-driven, so
    a future msgtype the registry assigns ParserStrategy.TEXT_CONTENT
    would be handled here with zero code changes.
    """
    msgtype = decrypted.get("msgtype", "") or ""
    sender = decrypted.get("from", "") or None
    roomid = decrypted.get("roomid", "") or None
    msgtime = decrypted.get("msgtime", None)
    tolist = decrypted.get("tolist", [])

    is_text_content = get_parser_strategy(msgtype) == ParserStrategy.TEXT_CONTENT

    # Extract content_text only for text messages
    content_text = None
    if is_text_content:
        text_payload = decrypted.get("text", {}) or {}
        content_text = text_payload.get("content", "") or None

    # Extract sdkfileid if present (for media messages)
    sdkfileid = None
    if not is_text_content:
        # media messages store sdkfileid in the msgtype-specific payload.
        # RND-197 QA fix: a malformed historical row can hold a non-dict
        # value here (e.g. a bare string) — coerce defensively instead of
        # letting payload.get() raise and fail the whole decrypt run.
        payload = decrypted.get(msgtype, {}) or {}
        if not isinstance(payload, dict):
            payload = {}
        sdkfileid = payload.get("sdkfileid", None)

    # RND-197: structured field extraction (link/location/markdown/news/
    # weapp) + raw preservation (card/docmsg/audio_doc) for the basic
    # structured message types. None for msgtypes outside that scope
    # (text/media/control/composite/unknown) -- see
    # app.structured_message_parser.parse_structured_content docstring.
    # Deliberately independent of the SF-1 constraint above: this stores
    # only the type-specific sub-payload, never the full decrypted
    # envelope.
    structured_content = parse_structured_content(msgtype, decrypted)

    return {
        "msgtype": msgtype,
        "sender": sender,
        "roomid": roomid,
        "msgtime": msgtime,
        "tolist": tolist if tolist else None,
        "content_text": content_text,
        "sdkfileid": sdkfileid,
        "structured_content": structured_content,
    }


def _upsert_recipients(
    session: Session,
    message_id: int,
    tolist: list,
    tenant_id: "str | None" = None,
) -> None:
    """
    Insert ArchiveMessageRecipient rows for each entry in tolist.

    Deduplicates against rows already persisted for this message AND
    against repeats within tolist itself (RND-179 fix): the previous
    version queried `existing` once up front and never updated it as rows
    were added within this same call, so a tolist containing the same
    receiver twice (observed in real WeCom fanout payloads) inserted a
    duplicate archive_message_recipients row per repeat.

    The "already persisted" check is tenant-scoped (QA fix — RND-179): a
    plain message_id match previously let a malformed cross-tenant row
    (same message_id, wrong tenant_id, e.g. also "contact_a") count as
    "already there" and silently block insertion of the real tenant-owned
    row — a message could then never be repaired even though it correctly
    holds zero recipients for *its own* tenant. Matches how every other
    recipient read in this codebase is tenant-scoped (see
    _load_recipient_userids_map in app/reachability_audit.py and
    build_missing_recipient_repair_query below).
    """
    seen = {
        r.receiver_userid
        for r in session.query(ArchiveMessageRecipient)
        .filter(
            ArchiveMessageRecipient.message_id == message_id,
            ArchiveMessageRecipient.tenant_id == tenant_id,
        )
        .all()
    }
    for recipient in tolist:
        if recipient and recipient not in seen:
            session.add(
                ArchiveMessageRecipient(
                    message_id=message_id,
                    receiver_userid=recipient,
                    receiver_type="user",
                    tenant_id=tenant_id,
                )
            )
            seen.add(recipient)


# ---------------------------------------------------------------------------
# Recipient-persistence recovery (RND-179)
#
# RND-178's Message Reachability Audit found that a recipient-upsert
# failure is intentionally non-fatal to decrypt_status (see the call site
# in run_decrypt_once below) — decrypt success and recipient persistence
# are allowed to diverge so a transient recipient-write problem never
# blocks decryption. But that left a gap: this worker only ever
# reprocesses archive_messages rows with decrypt_status in ("pending",
# "failed"), so a message that hit exactly that failure — decrypt
# succeeded, recipient upsert didn't — was never revisited again. The
# tolist column already holds everything needed to recover it; this scan
# finds and repairs exactly that gap, using the same _upsert_recipients()
# persistence/dedup path as the live decrypt loop rather than a parallel
# implementation.
#
# Mirrors the established build_downloaded_repair_query /
# build_candidate_query pattern in app/media_download.py (RND-147/RND-199,
# the unified media download pipeline): a pure query-construction function
# plus a thin driver.
# ---------------------------------------------------------------------------


def build_missing_recipient_repair_query(session: Session, tenant_id: "str | None" = None):
    """
    Coarse candidate query: already-decrypted messages with zero
    *tenant-scoped* archive_message_recipients rows. Pure query
    construction — no execution.

    "Has recipients" is checked with a tenant_id match against the parent
    message (ArchiveMessageRecipient.tenant_id == ArchiveMessage.tenant_id),
    not merely a message_id match (QA fix — RND-179): the Reachability
    Audit (RND-178, see _load_recipient_userids_map in
    app/reachability_audit.py) already treats a recipient row carrying the
    wrong tenant_id as not belonging to the message for reachability
    purposes, since message_id alone doesn't prove tenant ownership for
    malformed/corrupt rows. Using a plain message_id match here would let
    such a stray row falsely mark a genuinely-unrepaired message as
    "already has recipients" and skip it forever, contradicting what the
    audit reports for the same message.

    Deliberately does NOT try to filter out an empty tolist at the SQL
    layer: SQLAlchemy's JSON/JSONB column type binds a Python `None` value
    as the JSON literal `null` (not SQL NULL) by default, so
    `tolist.isnot(None)` cannot reliably distinguish "no tolist" from "a
    JSON-null tolist" across backends. Same coarse-SQL-then-precise-Python
    split used by build_downloaded_repair_query() in
    app/media_download.py — repair_missing_recipients() below
    does the exact, per-row tolist check.
    """
    has_recipient = (
        session.query(ArchiveMessageRecipient.id)
        .filter(
            ArchiveMessageRecipient.message_id == ArchiveMessage.id,
            ArchiveMessageRecipient.tenant_id == ArchiveMessage.tenant_id,
        )
        .exists()
    )
    query = session.query(ArchiveMessage).filter(
        ArchiveMessage.decrypt_status == "success",
        ~has_recipient,
    )
    if tenant_id is not None:
        query = query.filter(ArchiveMessage.tenant_id == tenant_id)
    return query.order_by(ArchiveMessage.id)


def repair_missing_recipients(session: Session, tenant_id: "str | None" = None) -> int:
    """
    Recover archive_message_recipients rows for every candidate from
    build_missing_recipient_repair_query() that actually has at least one
    valid (non-blank) recipient in tolist — the precise check the coarse
    SQL candidate query cannot make (see its docstring).

    A message is only counted as repaired if a tenant-scoped recipient row
    for it actually exists after the upsert (QA fix — RND-179): a tolist
    made up entirely of blank/falsy entries (e.g. `[""]`) previously still
    incremented the repaired count even though _upsert_recipients() (by
    design) inserts nothing for a falsy entry, over-reporting success and
    weakening the operator-facing repair count as a diagnostic signal. Such
    a message is left alone and stays exactly as unreachable as the audit
    already correctly reports it (unreachable_missing_recipient), because
    there is no real data to recover it from.

    Idempotent — a message already holding recipient rows never matches
    the candidate query again, so calling this on every worker run is
    always safe.

    Returns the number of messages repaired.
    """
    repaired = 0
    for message in build_missing_recipient_repair_query(session, tenant_id).all():
        tolist = message.tolist or []
        if not any(tolist):
            continue
        _upsert_recipients(session, message.id, tolist, message.tenant_id)
        session.flush()
        has_persisted_recipient = (
            session.query(ArchiveMessageRecipient.id)
            .filter(
                ArchiveMessageRecipient.message_id == message.id,
                ArchiveMessageRecipient.tenant_id == message.tenant_id,
            )
            .first()
            is not None
        )
        if has_persisted_recipient:
            repaired += 1
    return repaired


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


def run_decrypt_once(
    session: Session,
    tenant_id: str,
    lib,
    private_key: rsa.RSAPrivateKey,
    expected_pubkey_ver: int,
    sdk=_default_sdk,
) -> DecryptRunSummary:
    """Decrypt every pending/failed archive_messages row for *tenant_id*,
    run the recipient/revocation repair scans, and commit.

    tenant_id is required and is never treated as "scan every tenant" —
    see module docstring for the RND-222 tenant-scope audit this closes.

    Never prints and never calls sys.exit: a commit failure raises
    DecryptCommitError (the one fatal outcome); every other failure mode
    (RSA failure, key mismatch, unsupported msgtype, non-fatal recipient/
    revoke-repair failures) is counted into the returned summary instead,
    exactly as scripts/decrypt_wecom_messages_once.py's original main()
    counted them before this extraction.
    """
    summary = DecryptRunSummary()

    records = (
        session.query(ArchiveMessage)
        .filter(
            ArchiveMessage.decrypt_status.in_(["pending", "failed"]),
            ArchiveMessage.tenant_id == tenant_id,
        )
        .order_by(ArchiveMessage.id)
        .all()
    )

    summary.scanned = len(records)

    for record in records:
        encrypt_key_raw = record.encrypt_random_key
        encrypt_msg = record.encrypt_chat_msg

        # SF-2: Guard against missing encrypted fields
        if not encrypt_key_raw or not encrypt_msg:
            record.decrypt_status = "failed"
            summary.failed += 1
            summary.return_codes[-1] = summary.return_codes.get(-1, 0) + 1
            continue

        # --- Key version check ---
        if record.publickey_ver != expected_pubkey_ver:
            record.decrypt_status = "failed"
            summary.failed += 1
            summary.key_mismatch += 1
            continue

        # --- RSA-decrypt encrypt_random_key ---
        encrypt_key = _rsa_decrypt_encrypt_key(private_key, encrypt_key_raw)
        if encrypt_key is None:
            record.decrypt_status = "failed"
            summary.failed += 1
            summary.rsa_failed += 1
            continue

        # --- C SDK DecryptData ---
        ret, decrypted_str = _decrypt_message(lib, encrypt_key, encrypt_msg, sdk=sdk)
        summary.return_codes[ret] = summary.return_codes.get(ret, 0) + 1

        if ret != 0 or decrypted_str is None:
            record.decrypt_status = "failed"
            summary.failed += 1
            continue

        # --- Parse decrypted JSON ---
        try:
            decrypted = json.loads(decrypted_str)
        except (json.JSONDecodeError, ValueError):
            record.decrypt_status = "failed"
            summary.failed += 1
            continue

        # --- Normalise fields ---
        try:
            normalised = _normalise_fields(decrypted)
        except Exception:
            record.decrypt_status = "failed"
            summary.failed += 1
            continue

        msgtype = normalised["msgtype"]

        # --- Update row (do NOT persist full decrypted_payload — SF-1) ---
        record.msgtype = normalised["msgtype"]
        record.sender = normalised["sender"]
        record.roomid = normalised["roomid"]
        record.msgtime = normalised["msgtime"]
        record.tolist = normalised["tolist"]
        record.sdkfileid = normalised["sdkfileid"]
        record.content_text = normalised["content_text"]
        # RND-197: scoped per-type structured payload only (never the
        # full decrypted envelope) — additive, does not touch SF-1.
        record.structured_content = normalised["structured_content"]
        record.decrypt_status = "success"

        # Upsert recipient rows — inherit tenant_id from the parent message.
        # Non-fatal by design (a recipient-persistence failure must not
        # block decrypt success), but silently swallowing it here used to
        # leave direct-conversation messages permanently unreachable with
        # no operator-visible signal (RND-178 finding). Counted below so
        # it shows up in the safe operational summary instead.
        tolist = normalised["tolist"] or []
        try:
            _upsert_recipients(session, record.id, tolist, record.tenant_id)
        except Exception:
            summary.recipient_upsert_failed += 1

        # RND-201: process this row's revoke association immediately
        # (links to its original right away if that row already
        # exists in this tenant; otherwise persists a pending
        # association for the repair scan below to pick up later).
        # No-op (returns None) for every non-revoke msgtype.
        if msgtype == "revoke":
            try:
                summary.revoke_event_seen += 1
                reconcile_revoke_event(session, record)
            except Exception:
                summary.revoke_reconcile_failed += 1

        if msgtype == "text":
            summary.success += 1
        else:
            summary.unsupported += 1

    # --- Repair recipient rows for previously-affected messages (RND-179)
    # — self-healing scan, safe to run on every invocation. Must happen in
    # the same session/commit as the loop above so a single worker run
    # leaves the database fully consistent. Tenant-scoped (RND-222 audit
    # fix) — this tenant's run must never touch another tenant's backlog.
    summary.recipients_repaired = repair_missing_recipients(session, tenant_id)

    # --- Repair pending revoke associations (RND-201) — self-healing scan
    # covering "original arrived in an earlier sweep, revoke arrived just
    # now" (already handled by the immediate reconcile_revoke_event() call
    # above) as well as "revoke arrived in an earlier sweep/worker run,
    # original just decrypted in THIS sweep" — the case the per-row call
    # above cannot see, since it only runs at the moment the revoke row
    # itself is processed. Tenant-scoped (RND-222 audit fix), same as
    # repair_missing_recipients above and the main query at the top of
    # this function.
    summary.revocations_reconciled = reconcile_pending_revocations(session, tenant_id)

    # --- Commit all changes ---
    try:
        session.commit()
    except Exception as exc:
        raise DecryptCommitError(str(exc)) from exc

    # --- Count remaining pending records (tenant-scoped, RND-222 audit
    # fix) — non-fatal: a failure here must not fail an otherwise-
    # successful run, matching the original script's behavior.
    try:
        summary.pending_remaining = (
            session.query(ArchiveMessage)
            .filter(
                ArchiveMessage.decrypt_status == "pending",
                ArchiveMessage.tenant_id == tenant_id,
            )
            .count()
        )
    except Exception:
        summary.pending_remaining = -1  # unable to determine

    return summary
