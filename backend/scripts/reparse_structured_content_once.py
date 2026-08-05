#!/usr/bin/env python3
"""
One-shot historical backfill: re-decrypt and re-parse selected structured
archive_messages rows whose persisted structured_content predates a parser
fix (RND-257, extended for sphfeed support).

Background: RND-243 fixed app.structured_message_parser's chatrecord/mixed
nested-item parser (ChatRecord-prefixed child types now normalize to their
canonical type instead of being echoed as raw-JSON text; child msgtime
seconds->ms). That fix only affects messages decrypted/re-parsed AFTER it
shipped. Historical rows that already reached decrypt_status="success"
under the old, buggy parser still carry the broken shape.

Feasibility (corrected — see RND-257 ticket comment for the full writeup):
ArchiveMessage.decrypted_payload is NEVER populated (SF-1, a permanent,
tested data-minimization constraint — see app.services.decrypt_worker's
module docstring and tests/test_decrypt_structured_content.py's SF-1
regression guard). There is no cached decrypted payload to re-feed into
the parser. What IS always retained is the encrypted envelope
(raw_encrypted_payload/encrypt_random_key/encrypt_chat_msg — "always
retained so decryption can be re-run after a key rotation", see
ArchiveMessage's docstring), so this script re-runs RSA-decrypt + WeCom
SDK DecryptData for each candidate row, then re-parses the result through
app.structured_message_parser.parse_structured_content, and persists ONLY
structured_content — never decrypted_payload, decrypt_status, or any
other column (SF-1 stays intact; this is additive, exactly like RND-197's
structured_content column itself).

This is a THIN driver around existing, tested primitives — never a
second, independent decrypt or parse implementation:
  - app.services.decrypt_worker._rsa_decrypt_encrypt_key /
    _decrypt_message / _normalise_fields (same functions
    run_decrypt_once uses for live rows)
  - app.services.decrypt_isolation (RND-231 subprocess isolation) via
    _decrypt_message's lib_path parameter — a SIGSEGV in WeCom SDK's
    DecryptData (RND-208; confirmed msgtype-agnostic, see
    docs/RND-208-decryptdata-sigsegv.md §5) is contained to one row and
    counted, never aborts the batch.
  - app.structured_message_parser.parse_structured_content — the RND-243
    fix itself; this script changes none of its logic.

Candidate rows: decrypt_status="success", an explicitly reparseable
``msgtype``, and structured_content still in that type's known stale shape.
The default remains the historical RND-257 chatrecord/mixed repair; pass
``--msgtype sphfeed`` to backfill Video Channels messages after that parser
ships. A row already fixed (by a previous run of this script, or
coincidentally already correct) never matches again — idempotent, safe to
re-run any number of times.

Mode: unlike scripts/backfill_revoke_associations_once.py (dry-run by
default, --apply to write), this script WRITES by default and --dry-run
is the opt-in preview mode — matching scripts/decrypt_wecom_messages_once.py
and scripts/download_wecom_media_once.py's own default-apply convention,
and the small, well-understood blast radius here (RND-257 verified only
~37 historical rows total in production).

Usage (from backend/):
    python scripts/reparse_structured_content_once.py                    # apply, all tenants
    python scripts/reparse_structured_content_once.py --dry-run           # preview only, no writes
    python scripts/reparse_structured_content_once.py --tenant X          # scope to one tenant
    python scripts/reparse_structured_content_once.py --limit 10          # cap rows processed this run
    python scripts/reparse_structured_content_once.py --since 1700000000000 --until 1800000000000
    python scripts/reparse_structured_content_once.py --msgtype sphfeed   # apply Video Channels backfill
    python scripts/reparse_structured_content_once.py --msgtype sphfeed --dry-run
    python scripts/reparse_structured_content_once.py --download          # also trigger the existing
                                                                           # nested-media download scan

Required environment variables:
    DATABASE_URL              PostgreSQL connection string
    WECOM_SDK_LIB_PATH        Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_CORP_ID             WeCom corporation ID
    WECOM_ARCHIVE_SECRET      WeCom conversation archive secret
    WECOM_PRIVATE_KEY_PATH    Absolute path to RSA private key PEM file
    WECOM_PUBLIC_KEY_VERSION  Expected publickey_ver for current private key
Required even for --dry-run: the preview must actually re-decrypt and
re-parse each candidate to report what WOULD change; only the final
commit is skipped.

Exit codes:
    0  Success (including "nothing to do")
    1  Fatal initialisation failure (env, SDK load/init, DB connect)

Safety constraints:
    - Only structured_content is ever written. decrypted_payload,
      decrypt_status, media_files, recipients, and every other column are
      never read for writing and never assigned.
    - No decrypted message content, titles, or raw item text is printed —
      not even in --dry-run. Only shape-level counters (item/media_refs
      counts, outcome tags) are safe to print, matching every other
      *_once.py script's "no content, no payloads" constraint.
    - No encrypted payloads, private keys, or decrypted encrypt_key are
      printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, Session

from app.db.models import ArchiveMessage
from app.media_download import NESTED_MEDIA_MSGTYPES
from app.sdk import wecom_sdk
from app.services.decrypt_isolation import SIGSEGV_SENTINEL
from scripts.decrypt_wecom_messages_once import (
    _decrypt_message,
    _load_private_key,
    _normalise_fields,
    _rsa_decrypt_encrypt_key,
)
from app.sdk import wecom_sdk as _default_sdk

_DEFAULT_BATCH_SIZE = 500

# Keep the existing RND-257 behavior as the default. New types are opt-in so
# adding a parser never causes this maintenance command to re-decrypt an
# unrelated archive population merely because an operator ran it with no
# arguments.
_DEFAULT_REPARSE_MSGTYPES = frozenset(NESTED_MEDIA_MSGTYPES)
_REPARSEABLE_MSGTYPES = _DEFAULT_REPARSE_MSGTYPES | frozenset({"sphfeed"})


def _validated_msgtypes(msgtypes: Optional[tuple[str, ...]]) -> frozenset[str]:
    selected = frozenset(msgtypes) if msgtypes else _DEFAULT_REPARSE_MSGTYPES
    if not selected:
        raise ValueError("at least one msgtype is required")
    unknown = selected - _REPARSEABLE_MSGTYPES
    if unknown:
        raise ValueError(f"unsupported reparse msgtype(s): {sorted(unknown)!r}")
    return selected


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Old broken-shape detection
# ---------------------------------------------------------------------------


def _iter_item_nodes(items):
    """Walk a chatrecord/mixed structured_content['fields']['items'] tree,
    yielding every node (including nested children, mixed-in-mixed /
    chatrecord-in-mixed) — mirrors the recursive node shape
    app.structured_message_parser._parse_nested_item produces
    (type/text/fields/media/sender/timestamp/children)."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        children = item.get("children")
        if isinstance(children, list):
            yield from _iter_item_nodes(children)


def _has_raw_json_echo(structured_content: dict) -> bool:
    """True if any nested item's `text` is a JSON-parseable string whose
    parsed value carries a `sdkfileid` key — the old parser's signature
    "raw-JSON echoed as text" bug this script exists to fix (see RND-243's
    test_parse_chatrecord_message_chatrecord_image_no_longer_echoes_raw_json
    regression test for the fixed-parser contract)."""
    fields = structured_content.get("fields")
    if not isinstance(fields, dict):
        return False
    items = fields.get("items")
    if not isinstance(items, list):
        return False
    for node in _iter_item_nodes(items):
        text = node.get("text")
        if not isinstance(text, str) or not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "sdkfileid" in parsed:
            return True
    return False


def is_stale_structured_content(structured_content, msgtype: Optional[str] = None) -> bool:
    """Return whether a selected type still needs a historical reparse.

    ``None`` preserves the historical chatrecord/mixed detector contract for
    callers and tests.  Video Channels rows need a deliberately narrower
    rule: before this parser existed they have no ``fields`` object; once it
    succeeds, even a degraded-but-valid fields object is current and must not
    be decrypted again on every maintenance run.
    """
    if msgtype == "sphfeed":
        return not (
            isinstance(structured_content, dict)
            and isinstance(structured_content.get("fields"), dict)
        )

    # chatrecord/mixed: stale when unset, missing/empty media_refs, or still
    # carrying a raw-JSON echo from the pre-RND-243 nested parser.
    if not isinstance(structured_content, dict):
        return True
    media_refs = structured_content.get("media_refs")
    if not isinstance(media_refs, list) or not media_refs:
        return True
    return _has_raw_json_echo(structured_content)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def find_reparse_candidates(
    session: Session,
    tenant_id: "str | None" = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    msgtypes: Optional[tuple[str, ...]] = None,
) -> Query:
    """Coarse tenant-scoped query for selected successful archive rows.

    The precise type-specific stale check happens in Python via
    :func:`is_stale_structured_content`; keeping SQL broad avoids embedding
    PostgreSQL- and SQLite-specific JSON predicates in a safety tool.
    """
    selected_msgtypes = _validated_msgtypes(msgtypes)
    query = session.query(ArchiveMessage).filter(
        ArchiveMessage.msgtype.in_(selected_msgtypes),
        ArchiveMessage.decrypt_status == "success",
    )
    if tenant_id is not None:
        query = query.filter(ArchiveMessage.tenant_id == tenant_id)
    if since_ms is not None:
        query = query.filter(ArchiveMessage.msgtime >= since_ms)
    if until_ms is not None:
        query = query.filter(ArchiveMessage.msgtime <= until_ms)
    return query.order_by(ArchiveMessage.id)


def select_reparse_candidates(
    session: Session,
    tenant_id: "str | None" = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    limit: Optional[int] = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    msgtypes: Optional[tuple[str, ...]] = None,
) -> Tuple[List[ArchiveMessage], int]:
    """Return (stale_candidates, total_messages_scanned) for selected types."""
    selected_msgtypes = tuple(_validated_msgtypes(msgtypes))
    total_scanned = find_reparse_candidates(
        session, tenant_id, since_ms, until_ms, selected_msgtypes
    ).count()

    candidates: List[ArchiveMessage] = []
    last_id = 0
    while limit is None or len(candidates) < limit:
        batch = (
            find_reparse_candidates(session, tenant_id, since_ms, until_ms, selected_msgtypes)
            .filter(ArchiveMessage.id > last_id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break
        for row in batch:
            if is_stale_structured_content(row.structured_content, row.msgtype):
                candidates.append(row)
                if limit is not None and len(candidates) >= limit:
                    break
        last_id = batch[-1].id
        if len(batch) < batch_size:
            break
    return candidates, total_scanned


# ---------------------------------------------------------------------------
# Re-decrypt + re-parse one row
# ---------------------------------------------------------------------------


def reparse_one_message(
    private_key: rsa.RSAPrivateKey,
    lib,
    expected_pubkey_ver: int,
    row: ArchiveMessage,
    lib_path: "str | None" = None,
    sdk=_default_sdk,
) -> "tuple[str, dict | None]":
    """Re-decrypt ONE historical chatrecord/mixed row's still-retained
    encrypted envelope and re-run parse_structured_content on the result —
    reuses the exact RSA-decrypt + WeCom SDK DecryptData +
    app.structured_message_parser pipeline app.services.decrypt_worker.
    run_decrypt_once uses for live rows (same pattern as
    scripts/backfill_revoke_associations_once.py's
    recover_historical_revoke_structured_content), never a second,
    independent decrypt/parse implementation.

    Returns (outcome, structured_content_or_None). outcome is one of:
    "reparsed", "key_mismatch", "missing_envelope", "rsa_failed",
    "sdk_decrypt_failed", "sigsegv", "invalid_json". structured_content is
    only non-None for "reparsed" — every other outcome means the caller
    must leave the row completely untouched. This function never assigns
    anything onto `row` itself and never touches decrypted_payload/
    decrypt_status (SF-1) — persistence is entirely the caller's
    responsibility.
    """
    if row.publickey_ver != expected_pubkey_ver:
        return "key_mismatch", None
    if not row.encrypt_random_key or not row.encrypt_chat_msg:
        return "missing_envelope", None

    encrypt_key = _rsa_decrypt_encrypt_key(private_key, row.encrypt_random_key)
    if encrypt_key is None:
        return "rsa_failed", None

    ret, decrypted_str = _decrypt_message(
        lib, encrypt_key, row.encrypt_chat_msg, sdk=sdk, lib_path=lib_path
    )
    if ret == SIGSEGV_SENTINEL:
        return "sigsegv", None
    if ret != 0 or decrypted_str is None:
        return "sdk_decrypt_failed", None

    try:
        decrypted = json.loads(decrypted_str)
    except (json.JSONDecodeError, ValueError):
        return "invalid_json", None

    normalised = _normalise_fields(decrypted)
    return "reparsed", normalised["structured_content"]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


@dataclass
class ReparseSummary:
    scanned: int = 0
    stale_detected: int = 0
    reparsed: int = 0
    skipped_unchanged: int = 0
    media_refs_added: int = 0
    key_mismatch: int = 0
    missing_envelope: int = 0
    rsa_failed: int = 0
    sdk_decrypt_failed: int = 0
    sigsegv: int = 0
    invalid_json: int = 0
    outcome_counts: dict = field(default_factory=dict)


def run_reparse_once(
    session: Session,
    private_key: rsa.RSAPrivateKey,
    lib,
    expected_pubkey_ver: int,
    tenant_id: "str | None" = None,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    sdk=_default_sdk,
    lib_path: "str | None" = None,
    msgtypes: Optional[tuple[str, ...]] = None,
) -> ReparseSummary:
    """Re-decrypt + re-parse every stale selected-type candidate in scope,
    writing only structured_content. dry_run=True runs the exact
    same re-decrypt/re-parse/compare logic (so the reported counts reflect
    what WOULD happen) but rolls back instead of committing — same
    dry-run-via-rollback convention scripts/backfill_revoke_associations_
    once.py uses, rather than skipping the write logic itself.

    Idempotent: a row whose freshly-reparsed structured_content is
    identical to what it already had is counted as skipped_unchanged, not
    reparsed, and (since select_reparse_candidates re-derives staleness
    from the CURRENT column value) a row this call actually fixes never
    matches the candidate query again on a subsequent invocation.
    """
    summary = ReparseSummary()
    candidates, total_scanned = select_reparse_candidates(
        session,
        tenant_id=tenant_id,
        since_ms=since_ms,
        until_ms=until_ms,
        limit=limit,
        msgtypes=msgtypes,
    )
    summary.scanned = total_scanned
    summary.stale_detected = len(candidates)

    for row in candidates:
        outcome, structured_content = reparse_one_message(
            private_key, lib, expected_pubkey_ver, row, lib_path=lib_path, sdk=sdk
        )
        summary.outcome_counts[outcome] = summary.outcome_counts.get(outcome, 0) + 1

        if outcome != "reparsed":
            if outcome == "sigsegv":
                summary.sigsegv += 1
            elif outcome == "key_mismatch":
                summary.key_mismatch += 1
            elif outcome == "missing_envelope":
                summary.missing_envelope += 1
            elif outcome == "rsa_failed":
                summary.rsa_failed += 1
            elif outcome == "invalid_json":
                summary.invalid_json += 1
            else:
                summary.sdk_decrypt_failed += 1
            continue

        if structured_content == row.structured_content:
            summary.skipped_unchanged += 1
            continue

        had_media_before = bool(
            isinstance(row.structured_content, dict) and row.structured_content.get("media_refs")
        )
        row.structured_content = structured_content
        summary.reparsed += 1
        has_media_after = bool(
            isinstance(structured_content, dict) and structured_content.get("media_refs")
        )
        if has_media_after and not had_media_before:
            summary.media_refs_added += 1

    session.flush()
    if dry_run:
        session.rollback()
    else:
        session.commit()

    return summary


# ---------------------------------------------------------------------------
# --download: trigger the existing RND-200 nested-media download scan
# ---------------------------------------------------------------------------


def trigger_nested_media_download_scan() -> int:
    """Invoke the existing, sole media-download entry point
    (scripts/download_wecom_media_once.py) as a subprocess so it picks up
    the sdkfileids this run just registered into media_refs — reuses that
    script's own lock/SDK/storage lifecycle unchanged rather than
    duplicating any of it here (RND-257 explicitly does not add new
    download logic). Inherits the current environment (DATABASE_URL,
    WECOM_*, STORAGE_LOCAL_PATH, ...) unchanged. Returns the subprocess's
    exit code; never raises."""
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_wecom_media_once.py")
    result = subprocess.run([sys.executable, script_path], env=os.environ.copy())
    return result.returncode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="Scope the scan to a single tenant_id (default: every tenant)")
    parser.add_argument(
        "--msgtype",
        dest="msgtypes",
        action="append",
        choices=sorted(_REPARSEABLE_MSGTYPES),
        help="Type to reparse; repeat for multiple types. Default: chatrecord and mixed.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max stale rows to reparse this run (default: no cap)")
    parser.add_argument("--since", type=int, default=None, help="Only consider rows with msgtime >= this epoch-ms cutoff")
    parser.add_argument("--until", type=int, default=None, help="Only consider rows with msgtime <= this epoch-ms cutoff")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Re-decrypt and re-parse every candidate to report what WOULD change, but write nothing.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="After reparsing (skipped entirely in --dry-run), trigger scripts/download_wecom_media_once.py "
        "so newly-registered media_refs.sdkfileid values get downloaded.",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        print("[FAIL] --limit must be a positive integer", flush=True)
        sys.exit(1)

    database_url = _require_env("DATABASE_URL")
    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")
    private_key_path = _require_env("WECOM_PRIVATE_KEY_PATH")
    expected_pubkey_ver_str = _require_env("WECOM_PUBLIC_KEY_VERSION")

    try:
        expected_pubkey_ver = int(expected_pubkey_ver_str)
    except ValueError:
        print(
            f"[FAIL] WECOM_PUBLIC_KEY_VERSION is not a valid integer: {expected_pubkey_ver_str!r}",
            flush=True,
        )
        sys.exit(1)

    try:
        private_key = _load_private_key(private_key_path)
    except FileNotFoundError:
        print(f"[FAIL] Private key file not found: {os.path.basename(private_key_path)}", flush=True)
        sys.exit(1)
    except Exception as exc:
        print(f"[FAIL] Failed to load private key: {exc}", flush=True)
        sys.exit(1)

    engine = create_engine(database_url)

    try:
        lib = wecom_sdk.load_sdk(lib_path)
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)
    except OSError as exc:
        print(f"[FAIL] Failed to load SDK library: {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk(lib)
        wecom_sdk.configure_sdk_decrypt_data(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (DecryptData path): {exc}", flush=True)
        sys.exit(1)

    handle = wecom_sdk.new_sdk(lib)
    if not handle:
        print("[FAIL] NewSdk() returned a null handle", flush=True)
        sys.exit(1)

    init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
    if init_ret != 0:
        print(f"[FAIL] Init() failed (return code {init_ret})", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass
        sys.exit(1)

    with Session(engine) as session:
        summary = run_reparse_once(
            session,
            private_key,
            lib,
            expected_pubkey_ver,
            tenant_id=args.tenant,
            since_ms=args.since,
            until_ms=args.until,
            limit=args.limit,
            dry_run=args.dry_run,
            lib_path=lib_path,
            msgtypes=tuple(args.msgtypes) if args.msgtypes else None,
        )

    try:
        wecom_sdk.destroy_sdk(lib, handle)
    except Exception:
        pass

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"[INFO] reparse mode: {mode}", flush=True)
    print(f"[INFO] reparse tenant: {args.tenant or 'ALL'}", flush=True)
    print(
        f"[INFO] reparse msgtypes: {','.join(args.msgtypes or sorted(_DEFAULT_REPARSE_MSGTYPES))}",
        flush=True,
    )
    print(f"[INFO] reparse scanned: {summary.scanned}", flush=True)
    print(f"[INFO] reparse stale_detected: {summary.stale_detected}", flush=True)
    print(f"[INFO] reparse reparsed: {summary.reparsed}", flush=True)
    print(f"[INFO] reparse media_refs_added: {summary.media_refs_added}", flush=True)
    print(f"[INFO] reparse skipped_unchanged: {summary.skipped_unchanged}", flush=True)
    if summary.key_mismatch:
        print(f"[INFO] reparse key_mismatch: {summary.key_mismatch}", flush=True)
    if summary.missing_envelope:
        print(f"[INFO] reparse missing_envelope: {summary.missing_envelope}", flush=True)
    if summary.rsa_failed:
        print(f"[INFO] reparse rsa_failed: {summary.rsa_failed}", flush=True)
    if summary.sdk_decrypt_failed:
        print(f"[INFO] reparse sdk_decrypt_failed: {summary.sdk_decrypt_failed}", flush=True)
    if summary.sigsegv:
        print(f"[INFO] reparse sigsegv: {summary.sigsegv}", flush=True)
    if summary.invalid_json:
        print(f"[INFO] reparse invalid_json: {summary.invalid_json}", flush=True)

    if args.download:
        if args.dry_run:
            print("[INFO] reparse --download skipped: --dry-run wrote nothing to trigger a download for", flush=True)
        else:
            print("[INFO] reparse triggering nested media download scan...", flush=True)
            download_exit_code = trigger_nested_media_download_scan()
            print(f"[INFO] reparse download_scan_exit_code: {download_exit_code}", flush=True)

    print("[PASS] reparse_structured_content_once completed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
