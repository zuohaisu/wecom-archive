#!/usr/bin/env python3
"""
One-shot backfill for historical WeCom revoke events (RND-201).

scripts/decrypt_wecom_messages_once.py only calls
app.revoke_reconciliation.reconcile_revoke_event() for a "revoke" row at
the moment it transitions out of decrypt_status in ("pending", "failed").
Any revoke-type archive_messages row that already had decrypt_status=
"success" BEFORE RND-201 shipped was never passed through that function
and so has no message_revocations row at all — this script is the
one-time catch-up for exactly those rows. Going forward, every new
revoke event is reconciled automatically by the ordinary decrypt
pipeline; if this script finds nothing to do, that is the expected
steady state, not a failure.

This is a THIN driver around app.revoke_reconciliation's two canonical
entry points — reconcile_revoke_event() and reconcile_pending_
revocations() — never a second, independent matching implementation.

Historical recovery (RND-201 round 2 QA fix): a revoke row decrypted
BEFORE this ticket's revoke parser existed has structured_content = NULL
— app.structured_message_parser.parse_structured_content() used to
return None unconditionally for msgtype="revoke" — so its pre_msgid was
never extracted or persisted, and reconciling such a row directly (the
round 1 behavior) permanently classifies it "malformed" even though the
association was actually recoverable. This script now re-decrypts each
such row's still-retained encrypted envelope (raw_encrypted_payload/
encrypt_random_key/encrypt_chat_msg — always kept for exactly this kind
of re-run, see ArchiveMessage's docstring) through the SAME RSA + WeCom
SDK DecryptData + parse_structured_content() pipeline
scripts/decrypt_wecom_messages_once.py uses, and persists ONLY
structured_content on that row before handing it to
reconcile_revoke_event() as usual. No other column on the row is ever
touched, and a row whose pre_msgid still cannot be recovered (RSA
failure, key-version mismatch, SDK error, malformed decrypted JSON) is
left completely alone — never fabricated, never guessed. Recovery needs
WeCom SDK credentials; if they are not configured (or --skip-historical-
recovery is passed), this pass is skipped and the rest of the script's
reconciliation behavior is unaffected.

Idempotent — safe to run any number of times. A revoke-type row that
already has a message_revocations row (from a previous run of this
script, or from the live decrypt pipeline) is a no-op via
reconcile_revoke_event()'s own existing-row check; find_unreconciled_
revoke_events() below excludes it from the candidate scan entirely. A
row whose structured_content was already successfully recovered by an
earlier run of this script is likewise excluded from the historical-
recovery candidate scan (find_historical_undecrypted_revoke_events()
only selects structured_content IS NULL rows).

Tenant-aware — --tenant-id scopes the historical-recovery scan, the
association candidate scan, and the pending-repair pass to one tenant;
omitted, it processes every tenant (matching the tenant-agnostic
convention already used by decrypt_wecom_messages_once.py's own main
loop and its repair_missing_recipients() pass). Never touches any
archive_messages row outside the exact candidate sets these scans find —
no other message is read or written.

Dry-run by default — reports what WOULD happen without writing anything
(the transaction is rolled back, never committed) — this includes the
historical-recovery structured_content writes. Pass --apply to actually
persist. This script is never invoked automatically by any
worker/systemd timer; an operator must run it explicitly.

Usage (from backend/):
    python scripts/backfill_revoke_associations_once.py                          # dry-run, all tenants
    python scripts/backfill_revoke_associations_once.py --tenant-id X            # dry-run, one tenant
    python scripts/backfill_revoke_associations_once.py --apply                  # actually persist
    python scripts/backfill_revoke_associations_once.py --skip-historical-recovery  # association reconciliation only

Required environment variables:
    DATABASE_URL   PostgreSQL connection string

Optional environment variables (historical recovery only — recovery is
skipped, not a fatal error, when any of these is missing):
    WECOM_SDK_LIB_PATH        Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_CORP_ID             WeCom corporation ID
    WECOM_ARCHIVE_SECRET      WeCom conversation archive secret
    WECOM_PRIVATE_KEY_PATH    Absolute path to RSA private key PEM file
    WECOM_PUBLIC_KEY_VERSION  Expected publickey_ver for current private key

Exit codes:
    0  Success (dry-run or applied)
    1  Fatal initialisation failure (env, DB connect)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event, or_
from sqlalchemy.orm import Session
from sqlalchemy.types import JSON

from app.db.models import ArchiveMessage, MessageRevocation
from app.revoke_reconciliation import (
    display_status,
    reconcile_pending_revocations,
    reconcile_revoke_event,
)
from app.sdk import wecom_sdk
from scripts.decrypt_wecom_messages_once import (
    _decrypt_message,
    _load_private_key,
    _normalise_fields,
    _rsa_decrypt_encrypt_key,
)


def _configure_sqlite_for_savepoints_if_needed(engine) -> None:
    """app.revoke_reconciliation.reconcile_revoke_event() uses
    Session.begin_nested() (a SAVEPOINT) for conflict-safe inserts.
    Production always runs this against Postgres, which needs no special
    handling -- but pysqlite's own implicit transaction management can
    make a SAVEPOINT RELEASE behave like a premature commit of the outer
    transaction unless SQLAlchemy's documented sqlite recipe is applied,
    which would silently break --dry-run's rollback guarantee if this
    script were ever pointed at a sqlite DATABASE_URL (e.g. local dev).
    A no-op for every other dialect."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Missing required environment variable: {name}", flush=True)
        sys.exit(1)
    return value


def find_unreconciled_revoke_events(session: Session, tenant_id: "str | None" = None):
    """Coarse candidate query: every decrypt_status='success' msgtype=
    'revoke' row with no message_revocations row yet — the historical
    backlog this script exists to catch up. A row already reconciled (by
    a previous run of this script, or by the live decrypt pipeline)
    never matches this query again, so re-running is always cheap. Pure
    query construction — no execution — mirrors the build_*_repair_query
    convention in app/media_download.py and scripts/decrypt_wecom_
    messages_once.py.

    Deliberately excludes a row whose structured_content is still unset
    (SQL NULL or JSON null — see find_historical_undecrypted_revoke_
    events()'s docstring for why both are checked). Reconciling such a
    row would call app.revoke_reconciliation.reconcile_revoke_event()
    with no target_msgid to extract, which permanently creates a
    "malformed" MessageRevocation row (malformed is terminal by design —
    see that module) — blocking a LATER successful historical-recovery
    attempt (e.g. once WeCom SDK credentials are fixed) from ever taking
    effect, reproducing the exact bug this script exists to fix (RND-201
    round 2 QA finding). A row genuinely lacking a usable pre_msgid in
    its real decrypted payload still gets classified "malformed"
    correctly once recovery actually runs on it — recovery always
    produces a non-NULL structured_content in that case too (a dict with
    fields=None + a parse_warnings entry, not an unset column) — so this
    exclusion only ever defers a row, never causes it to be skipped
    forever. In the live decrypt pipeline (as opposed to this script),
    structured_content is always set before reconcile_revoke_event() is
    ever called for a row, so this exclusion has no effect there.
    """
    has_revocation = (
        session.query(MessageRevocation.id)
        .filter(
            MessageRevocation.tenant_id == ArchiveMessage.tenant_id,
            MessageRevocation.revoke_event_message_id == ArchiveMessage.id,
        )
        .exists()
    )
    query = session.query(ArchiveMessage).filter(
        ArchiveMessage.msgtype == "revoke",
        ArchiveMessage.decrypt_status == "success",
        ~has_revocation,
        ~or_(
            ArchiveMessage.structured_content.is_(None),
            ArchiveMessage.structured_content.is_(JSON.NULL),
        ),
    )
    if tenant_id is not None:
        query = query.filter(ArchiveMessage.tenant_id == tenant_id)
    return query.order_by(ArchiveMessage.id)


def find_historical_undecrypted_revoke_events(
    session: Session, tenant_id: "str | None" = None
):
    """Coarse candidate query: every decrypt_status='success' msgtype=
    'revoke' row with structured_content still unset — decrypted before
    RND-201's revoke parser existed, so pre_msgid was never extracted.
    Re-decryptable from the still-retained encrypted envelope (see module
    docstring). A row this script (or a future decrypt-pipeline run)
    already recovered structured_content for never matches this query
    again.

    "Unset" is deliberately checked two ways: real SQL NULL (a row that
    predates migration 0008 and has never had this column written at
    all) AND the JSON literal null (what every row actually gets when
    scripts/decrypt_wecom_messages_once.py assigns
    record.structured_content = None through the ORM -- SQLAlchemy's
    JSONB type persists an explicit Python None as JSON "null" by
    default, not SQL NULL, since the column is not declared with
    none_as_null=True). Matching only IS NULL would silently find zero
    historical rows in production despite thousands of them existing.
    """
    query = session.query(ArchiveMessage).filter(
        ArchiveMessage.msgtype == "revoke",
        ArchiveMessage.decrypt_status == "success",
        or_(
            ArchiveMessage.structured_content.is_(None),
            ArchiveMessage.structured_content.is_(JSON.NULL),
        ),
    )
    if tenant_id is not None:
        query = query.filter(ArchiveMessage.tenant_id == tenant_id)
    return query.order_by(ArchiveMessage.id)


def _load_optional_sdk_env():
    """Best-effort load of the WeCom SDK + RSA private key for historical
    revoke recovery. Returns None — never raises, never exits — if any
    required credential is missing or fails to load, since recovery is
    optional: the rest of this script's reconciliation work must still
    run without WeCom SDK access at all (e.g. a pure dry-run association
    audit). Returns (private_key, lib, handle, expected_pubkey_ver) on
    success; caller is responsible for wecom_sdk.destroy_sdk(lib, handle)
    when done.
    """
    required = (
        "WECOM_SDK_LIB_PATH",
        "WECOM_CORP_ID",
        "WECOM_ARCHIVE_SECRET",
        "WECOM_PRIVATE_KEY_PATH",
        "WECOM_PUBLIC_KEY_VERSION",
    )
    values = {name: os.environ.get(name, "").strip() for name in required}
    if not all(values.values()):
        return None

    try:
        expected_pubkey_ver = int(values["WECOM_PUBLIC_KEY_VERSION"])
    except ValueError:
        return None

    try:
        private_key = _load_private_key(values["WECOM_PRIVATE_KEY_PATH"])
    except Exception:
        return None

    try:
        lib = wecom_sdk.load_sdk(values["WECOM_SDK_LIB_PATH"])
        wecom_sdk.configure_sdk(lib)
        wecom_sdk.configure_sdk_decrypt_data(lib)
        handle = wecom_sdk.new_sdk(lib)
        if not handle:
            return None
        init_ret = wecom_sdk.init_sdk(
            lib, handle, values["WECOM_CORP_ID"], values["WECOM_ARCHIVE_SECRET"]
        )
        if init_ret != 0:
            return None
    except Exception:
        return None

    return private_key, lib, handle, expected_pubkey_ver


def recover_historical_revoke_structured_content(
    private_key, lib, expected_pubkey_ver: int, row: ArchiveMessage
) -> "tuple[str, dict | None]":
    """Re-decrypt ONE historical revoke row's still-retained encrypted
    envelope, reusing the exact same RSA-decrypt + WeCom SDK DecryptData
    + app.structured_message_parser pipeline scripts/decrypt_wecom_
    messages_once.py uses for live rows — never a second, independent
    implementation of WeCom decryption or revoke field extraction.

    Returns (outcome, structured_content_or_None). outcome is one of:
    "recovered", "key_mismatch", "missing_envelope", "rsa_failed",
    "sdk_decrypt_failed", "invalid_json". structured_content is only
    non-None for "recovered" — every other outcome means the caller must
    leave the row completely untouched (no fabricated pre_msgid).
    """
    if row.publickey_ver != expected_pubkey_ver:
        return "key_mismatch", None
    if not row.encrypt_random_key or not row.encrypt_chat_msg:
        return "missing_envelope", None

    encrypt_key = _rsa_decrypt_encrypt_key(private_key, row.encrypt_random_key)
    if encrypt_key is None:
        return "rsa_failed", None

    ret, decrypted_str = _decrypt_message(lib, encrypt_key, row.encrypt_chat_msg)
    if ret != 0 or decrypted_str is None:
        return "sdk_decrypt_failed", None

    try:
        decrypted = json.loads(decrypted_str)
    except (json.JSONDecodeError, ValueError):
        return "invalid_json", None

    normalised = _normalise_fields(decrypted)
    return "recovered", normalised["structured_content"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Scope the scan to a single tenant (default: every tenant)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist changes. Default is dry-run: nothing is written.",
    )
    parser.add_argument(
        "--skip-historical-recovery",
        action="store_true",
        help=(
            "Skip re-decrypting historical revoke rows (structured_content "
            "IS NULL) even if WeCom SDK credentials are configured -- "
            "association reconciliation only."
        ),
    )
    args = parser.parse_args()

    database_url = _require_env("DATABASE_URL")
    engine = create_engine(database_url)
    _configure_sqlite_for_savepoints_if_needed(engine)

    recovered = 0
    recovery_key_mismatch = 0
    recovery_failed = 0
    recovery_skipped_no_sdk = 0

    with Session(engine) as session:
        # --- Historical recovery pass (RND-201 round 2 QA fix) — must
        # run BEFORE the reconciliation pass below, in the same session,
        # so a just-recovered row's pre_msgid is visible to
        # reconcile_revoke_event() in the very same script invocation
        # instead of requiring a second run.
        historical_candidates = (
            [] if args.skip_historical_recovery
            else find_historical_undecrypted_revoke_events(session, args.tenant_id).all()
        )
        if historical_candidates:
            sdk_env = _load_optional_sdk_env()
            if sdk_env is None:
                recovery_skipped_no_sdk = len(historical_candidates)
            else:
                private_key, lib, handle, expected_pubkey_ver = sdk_env
                try:
                    for row in historical_candidates:
                        outcome, structured_content = recover_historical_revoke_structured_content(
                            private_key, lib, expected_pubkey_ver, row
                        )
                        if outcome == "recovered":
                            row.structured_content = structured_content
                            recovered += 1
                        elif outcome == "key_mismatch":
                            recovery_key_mismatch += 1
                        else:
                            recovery_failed += 1
                    session.flush()
                finally:
                    try:
                        wecom_sdk.destroy_sdk(lib, handle)
                    except Exception:
                        pass

        candidates = find_unreconciled_revoke_events(session, args.tenant_id).all()
        candidate_ids = [m.id for m in candidates]

        skipped = 0
        for revoke_message in candidates:
            revocation = reconcile_revoke_event(session, revoke_message)
            if revocation is None:
                # Defensive only — find_unreconciled_revoke_events() only
                # selects msgtype="revoke" rows, so reconcile_revoke_event()
                # (which returns None solely for a non-revoke row) should
                # never actually hit this branch.
                skipped += 1
        session.flush()

        # Also re-run the general pending-repair scan: an earlier revoke
        # event (already reconciled, status="pending") may have its
        # original arrive as a side effect of nothing this script does —
        # but running this here means one backfill invocation catches up
        # both directions in one pass, exactly like the live decrypt
        # pipeline's own repair scan does every invocation.
        reconcile_pending_revocations(session, args.tenant_id)
        session.flush()

        final_statuses: dict = {}
        if candidate_ids:
            final_statuses = {
                row.revoke_event_message_id: row.status
                for row in session.query(MessageRevocation)
                .filter(MessageRevocation.revoke_event_message_id.in_(candidate_ids))
                .all()
            }

        linked = sum(1 for s in final_statuses.values() if s == "linked")
        malformed = sum(1 for s in final_statuses.values() if s == "malformed")
        # "pending" vs "original_missing" is the same request-time aging
        # distinction the timeline API applies (see
        # app.revoke_reconciliation.display_status) — reported here too so
        # an operator sees the same split the live UI would show.
        pending_rows = [
            row
            for row in session.query(MessageRevocation)
            .filter(
                MessageRevocation.revoke_event_message_id.in_(candidate_ids),
                MessageRevocation.status == "pending",
            )
            .all()
        ] if candidate_ids else []
        pending = sum(1 for row in pending_rows if display_status(row) == "pending")
        missing = sum(1 for row in pending_rows if display_status(row) == "original_missing")

        if args.apply:
            session.commit()
        else:
            session.rollback()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[INFO] backfill mode: {mode}", flush=True)
    print(f"[INFO] backfill tenant_id: {args.tenant_id or 'ALL'}", flush=True)
    print(f"[INFO] backfill historical_candidates_scanned: {len(historical_candidates)}", flush=True)
    print(f"[INFO] backfill historical_recovered: {recovered}", flush=True)
    if recovery_key_mismatch:
        print(f"[INFO] backfill historical_recovery_key_mismatch: {recovery_key_mismatch}", flush=True)
    if recovery_failed:
        print(f"[INFO] backfill historical_recovery_failed: {recovery_failed}", flush=True)
    if recovery_skipped_no_sdk:
        print(
            f"[INFO] backfill historical_recovery_skipped_no_sdk: {recovery_skipped_no_sdk} "
            "(WeCom SDK credentials not fully configured, or --skip-historical-recovery passed)",
            flush=True,
        )
    print(f"[INFO] backfill candidates_scanned: {len(candidates)}", flush=True)
    print(f"[INFO] backfill linked: {linked}", flush=True)
    print(f"[INFO] backfill pending: {pending}", flush=True)
    print(f"[INFO] backfill missing: {missing}", flush=True)
    print(f"[INFO] backfill malformed: {malformed}", flush=True)
    print(f"[INFO] backfill skipped: {skipped}", flush=True)
    print("[PASS] backfill_revoke_associations_once completed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
