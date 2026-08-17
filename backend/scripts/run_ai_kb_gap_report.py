#!/usr/bin/env python3
"""Generate the periodic AI knowledge/product-improvement candidate report
(RND-359 / T5) as a Markdown file under docs/ai/reports/. Never creates,
modifies, or closes a Linear issue — output is a draft for a human to read
and act on."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.ai.gap_report import collect_gap_report_data, render_gap_report_markdown

_REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "docs", "ai", "reports")


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("[FAIL] ai_kb_gap_report configuration_missing", flush=True)
        return 1

    now = datetime.now(timezone.utc)
    try:
        with Session(create_engine(database_url)) as db:
            data = collect_gap_report_data(db)
    except Exception:  # noqa: BLE001 - never expose DB URLs or chat content
        print("[FAIL] ai_kb_gap_report processing_failed", flush=True)
        return 1

    markdown = render_gap_report_markdown(data, generated_at=now.strftime("%Y-%m-%d %H:%M UTC"))

    os.makedirs(_REPORTS_DIR, exist_ok=True)
    output_path = os.path.join(_REPORTS_DIR, f"gap-report-{now.strftime('%Y-%m-%d')}.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(f"[INFO] ai_kb_gap_report completed output={output_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
