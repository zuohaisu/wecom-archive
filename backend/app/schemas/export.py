"""Input/output value objects for evidence-export generation (RND-315)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class ExportFormat(str, Enum):
    EXCEL = "excel"
    PDF = "pdf"


@dataclass(frozen=True)
class ExportSelection:
    """Tenant-scoped message filters.

    At least one filter is required by the export service so callers cannot
    accidentally create an unbounded tenant-wide evidence export.
    """

    roomid: Optional[str] = None
    participant_id: Optional[str] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    # Public WeCom message identifiers, never database surrogate IDs.  These
    # are the identifiers exposed by timeline/search APIs and are stable
    # across UI handoffs into the shared export page.
    message_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportResult:
    content: bytes
    filename: str
    content_type: str
    record_count: int
