#!/usr/bin/env python3
"""Run the fixed AI-support retrieval evaluation set (RND-359 / T5) against
a freshly built index and gate on its launch thresholds. Exit code 0 only
if every blocker (over-permission, hit rate, abstention rate) clears —
see app.services.ai.eval for the exact thresholds and rationale."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai_kb.manifest_schema import ManifestValidationError
from app.db.models import AiEvalRun
from app.services.ai.eval import load_eval_dataset, run_eval
from app.services.ai.ingestion import build_index

_DATASET_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "ai_eval_dataset.json"


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] ai_kb_eval configuration_missing", flush=True)
        return 1

    started_at = datetime.now(timezone.utc)
    try:
        with Session(create_engine(database_url)) as db:
            index_result = build_index(db, triggered_by="eval")
            dataset_version, cases = load_eval_dataset(_DATASET_PATH)
            summary = run_eval(db, dataset_version, cases, index_version_id=index_result.index_version_id)
            finished_at = datetime.now(timezone.utc)

            db.add(
                AiEvalRun(
                    dataset_version=summary.dataset_version,
                    metrics={
                        "total_cases": summary.total_cases,
                        "hit_rate": summary.hit_rate,
                        "abstention_rate": summary.abstention_rate,
                        "over_permission_count": summary.over_permission_count,
                        "avg_latency_ms": summary.avg_latency_ms,
                    },
                    passed=summary.passed,
                    triggered_by=os.environ.get("AI_KB_EVAL_TRIGGER", "manual"),
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            db.commit()
    except ManifestValidationError as exc:
        print(f"[FAIL] ai_kb_eval manifest_invalid issues={len(exc.issues)}", flush=True)
        return 1
    except Exception:  # noqa: BLE001 - never expose DB URLs or doc content
        print("[FAIL] ai_kb_eval processing_failed", flush=True)
        return 1

    print(
        "[INFO] ai_kb_eval completed "
        f"dataset_version={summary.dataset_version} "
        f"total_cases={summary.total_cases} "
        f"hit_rate={summary.hit_rate:.2f} "
        f"abstention_rate={summary.abstention_rate:.2f} "
        f"over_permission_count={summary.over_permission_count} "
        f"avg_latency_ms={summary.avg_latency_ms:.0f} "
        f"passed={summary.passed}",
        flush=True,
    )
    if not summary.passed:
        print(f"[FAIL] ai_kb_eval blockers={summary.blockers}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
