#!/usr/bin/env python3
"""Rebuild the AI support knowledge-base index (RND-356 / T2) from the
current backend/app/ai_kb/manifest.json (RND-355). Safe to run repeatedly:
each run is a full rebuild into a brand-new index version; the previous
active version is marked superseded, never deleted, so it stays available
for manual rollback (see app.services.ai.ingestion.roll_back_to)."""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai_kb.manifest_schema import ManifestValidationError
from app.services.ai.ingestion import build_index
from app.services.ai.llm_provider import ai_support_is_enabled


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] ai_kb_reindex configuration_missing", flush=True)
        return 1

    if not ai_support_is_enabled():
        # A self-hosted deployment that disabled AI support has no reason to
        # spend a build cycle materializing a RAG index nobody will query.
        print("[INFO] ai_kb_reindex skipped ai_support_disabled", flush=True)
        return 0

    try:
        with Session(create_engine(database_url)) as db:
            result = build_index(db, triggered_by=os.environ.get("AI_KB_REINDEX_TRIGGER", "scheduled"))
    except ManifestValidationError as exc:
        # Fail closed: an invalid manifest must never partially index or
        # fall back to a stale index. Issue text only (no file content).
        print(f"[FAIL] ai_kb_reindex manifest_invalid issues={len(exc.issues)}", flush=True)
        return 1
    except Exception:  # noqa: BLE001 - never expose DB URLs or doc content
        print("[FAIL] ai_kb_reindex processing_failed", flush=True)
        return 1

    print(
        "[INFO] ai_kb_reindex completed "
        f"index_version_id={result.index_version_id} "
        f"ingested={len(result.ingested_source_ids)} "
        f"chunks={result.chunk_count} "
        f"rejected={len(result.rejected)}",
        flush=True,
    )
    if result.rejected:
        reasons = ", ".join(f"{source_id}:{reason}" for source_id, reason in result.rejected)
        print(f"[INFO] ai_kb_reindex rejected_sources {reasons}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
