"""Generate tenant-scoped evidence exports without persisting a file (RND-315).

Only normalized message fields are selected: the encrypted envelope and
``decrypted_payload`` are intentionally absent from every query and renderer.
"""

from __future__ import annotations

from copy import copy
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy.orm import Session

from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import ArchiveMessage, ArchiveMessageRecipient, Contact, MediaFile
from app.display_names import resolve_person_display_name, resolve_room_display_name
from app.message_type_registry import describe_message_type
from app.schemas.export import ExportFormat, ExportResult, ExportSelection

_BEIJING = ZoneInfo("Asia/Shanghai")
_HEADERS = ("Time (Beijing)", "Sender", "Recipient / conversation", "Type", "Content")
_MEDIA_CATEGORIES = frozenset({"media"})


def generate_export(
    db: Session,
    tenant_id: str,
    selection: ExportSelection,
    export_format: ExportFormat,
) -> ExportResult:
    """Return an in-memory PDF or XLSX for the tenant-scoped selection.

    The caller owns authorization, audit logging, and delivery. This service
    neither writes files nor reads encrypted/raw message columns.
    """
    _validate_selection(selection)
    rows = _load_export_rows(db, tenant_id, selection)
    if export_format == ExportFormat.EXCEL:
        return ExportResult(
            content=_render_excel(rows),
            filename="evidence-export.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
    if export_format == ExportFormat.PDF:
        return ExportResult(
            content=_render_pdf(rows),
            filename="evidence-export.pdf",
            content_type="application/pdf",
        )
    raise ValueError(f"Unsupported export format: {export_format!r}")


def _validate_selection(selection: ExportSelection) -> None:
    if not (selection.roomid or selection.message_ids or selection.start_ms is not None or selection.end_ms is not None):
        raise ValueError("An export selection must include a room, time range, or message IDs")
    if selection.start_ms is not None and selection.end_ms is not None and selection.start_ms > selection.end_ms:
        raise ValueError("start_ms must not be after end_ms")


def _load_export_rows(
    db: Session, tenant_id: str, selection: ExportSelection
) -> list[dict[str, str]]:
    """Load a bounded, normalized projection and its display metadata.

    This deliberately mirrors the timeline service's page projection: it
    reads only display-safe message columns and resolves recipients/names in
    tenant-scoped batches.
    """
    query = db.query(
        ArchiveMessage.id,
        ArchiveMessage.sender,
        ArchiveMessage.roomid,
        ArchiveMessage.msgtime,
        ArchiveMessage.msgtype,
        ArchiveMessage.content_text,
        ArchiveMessage.structured_content,
    ).filter(ArchiveMessage.tenant_id == tenant_id)
    if selection.roomid:
        query = query.filter(ArchiveMessage.roomid == selection.roomid)
    if selection.message_ids:
        query = query.filter(ArchiveMessage.id.in_(selection.message_ids))
    if selection.start_ms is not None:
        query = query.filter(ArchiveMessage.msgtime >= selection.start_ms)
    if selection.end_ms is not None:
        query = query.filter(ArchiveMessage.msgtime <= selection.end_ms)
    messages = query.order_by(ArchiveMessage.msgtime.asc(), ArchiveMessage.id.asc()).all()
    if not messages:
        return []

    message_ids = [message.id for message in messages]
    recipients_by_message: dict[int, list[str]] = {}
    for message_id, receiver_id in (
        db.query(ArchiveMessageRecipient.message_id, ArchiveMessageRecipient.receiver_userid)
        .filter(
            ArchiveMessageRecipient.tenant_id == tenant_id,
            ArchiveMessageRecipient.message_id.in_(message_ids),
        )
        .all()
    ):
        recipients_by_message.setdefault(message_id, []).append(receiver_id)

    participant_ids = {message.sender for message in messages if message.sender}
    for recipients in recipients_by_message.values():
        participant_ids.update(recipients)
    display_names = {
        userid: name
        for userid, name in (
            db.query(Contact.wecom_userid, Contact.name)
            .filter(Contact.tenant_id == tenant_id, Contact.wecom_userid.in_(participant_ids))
            .all()
            if participant_ids
            else []
        )
    }
    room_display_names = load_group_chat_display_names(
        db, tenant_id, (message.roomid for message in messages)
    )
    media_by_message = {
        message_id: (file_type, file_size)
        for message_id, file_type, file_size in (
            db.query(MediaFile.archive_message_id, MediaFile.file_type, MediaFile.file_size)
            .filter(
                MediaFile.tenant_id == tenant_id,
                MediaFile.archive_message_id.in_(message_ids),
            )
            .all()
        )
    }

    return [
        _project_message(
            message,
            recipients_by_message,
            display_names,
            media_by_message,
            room_display_names,
        )
        for message in messages
    ]


def _project_message(
    message: Any,
    recipients_by_message: dict[int, list[str]],
    display_names: dict[str, str],
    media_by_message: dict[int, tuple[str | None, int | None]],
    room_display_names: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    recipients = recipients_by_message.get(message.id, [])
    room_display_names = room_display_names or {}
    if message.roomid:
        conversation = resolve_room_display_name(
            message.roomid, room_display_names.get(message.roomid)
        )
    elif recipients:
        conversation = ", ".join(
            resolve_person_display_name(recipient, display_names.get(recipient))
            for recipient in recipients
        )
    else:
        conversation = "Direct conversation"

    type_meta = describe_message_type(message.msgtype)
    type_name = type_meta["normalized_type"]
    content = message.content_text or ""
    if type_meta["category"] in _MEDIA_CATEGORIES:
        content = _media_reference(message, type_name, media_by_message.get(message.id))
    elif not content:
        # A registry-derived name is a readable, stable placeholder for
        # unsupported/non-text types; never expose a raw protocol type code.
        content = f"[{type_name}]"

    return {
        "time": _format_beijing_time(message.msgtime),
        "sender": resolve_person_display_name(message.sender, display_names.get(message.sender)),
        "conversation": conversation,
        "type": type_name,
        "content": content,
    }


def _media_reference(
    message: Any,
    type_name: str,
    media: tuple[str | None, int | None] | None,
) -> str:
    filename = _normalized_filename(message.structured_content)
    file_type, file_size = media or (None, None)
    filename = filename or file_type or "unnamed media"
    size = f", {file_size} bytes" if file_size is not None else ""
    return f"[{type_name}: {filename}{size}]"


def _normalized_filename(structured_content: Any) -> str | None:
    """Return only a normalized filename/title field, never raw payload data."""
    if not isinstance(structured_content, dict):
        return None
    fields = structured_content.get("fields")
    if not isinstance(fields, dict):
        return None
    for key in ("filename", "file_name", "name", "title"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _format_beijing_time(msgtime: int | None) -> str:
    if msgtime is None:
        return "Unknown time"
    value = datetime.fromtimestamp(msgtime / 1000, tz=timezone.utc).astimezone(_BEIJING)
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def _render_excel(rows: list[dict[str, str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Evidence export"
    sheet.append(_HEADERS)
    for row in rows:
        sheet.append([row["time"], row["sender"], row["conversation"], row["type"], row["content"]])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in zip("ABCDE", (25, 22, 30, 20, 60)):
        sheet.column_dimensions[column].width = width
    for cell in sheet[1]:
        font = copy(cell.font)
        font.bold = True
        cell.font = font
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _render_pdf(rows: list[dict[str, str]]) -> bytes:
    output = BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        pageCompression=0,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("EvidenceBody", parent=styles["BodyText"], fontName="STSong-Light", fontSize=8, leading=10)
    heading = ParagraphStyle("EvidenceHeading", parent=styles["Heading2"], fontName="STSong-Light", fontSize=14)
    table_data = [[Paragraph(header, body) for header in _HEADERS]]
    for row in rows:
        table_data.append(
            [Paragraph(_pdf_text(row[key]), body) for key in ("time", "sender", "conversation", "type", "content")]
        )
    table = LongTable(table_data, colWidths=(34 * mm, 30 * mm, 45 * mm, 28 * mm, 110 * mm), repeatRows=1)
    table.setStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9EAF7")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])
    document.build([Paragraph("Evidence export", heading), Spacer(1, 4 * mm), table])
    return output.getvalue()


def _pdf_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
