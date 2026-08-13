"""RND-315 evidence-export service coverage using fixed synthetic message data."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from app.message_type_registry import describe_message_type
from app.schemas.export import ExportFormat, ExportSelection
from app.services import export_service


class _Query:
    def __init__(self, source, rows):
        self.source = source
        self.rows = rows
        self.filters = []

    def filter(self, *conditions):
        self.filters.extend(conditions)
        return self

    def order_by(self, *_columns):
        return self

    def limit(self, value):
        self.row_limit = value
        return self

    def all(self):
        # The fake models database isolation faithfully: each query's explicit
        # tenant predicate selects only its tenant's fixed rows.
        tenant_values = [
            condition.right.value
            for condition in self.filters
            if getattr(getattr(condition, "left", None), "key", None) == "tenant_id"
            and hasattr(getattr(condition, "right", None), "value")
        ]
        tenant_id = tenant_values[-1] if tenant_values else None
        rows = list(self.rows[self.source].get(tenant_id, []))
        msgid_values = [
            condition.right.value
            for condition in self.filters
            if getattr(getattr(condition, "left", None), "key", None) == "msgid"
            and hasattr(getattr(condition, "right", None), "value")
        ]
        if msgid_values and rows and hasattr(rows[0], "msgid"):
            allowed = set(msgid_values[-1])
            rows = [row for row in rows if row.msgid in allowed]
        return rows[: getattr(self, "row_limit", len(rows))]


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *columns):
        source = columns[0].key
        return _Query(source, self.rows)


def _message(message_id, tenant_id, **values):
    defaults = {
        "id": message_id,
        "msgid": f"msg-{message_id}",
        "tenant_id": tenant_id,
        "sender": "staff_alice",
        "roomid": "room_demo_001",
        "msgtime": 1735689600000,
        "msgtype": "text",
        "content_text": "fixed synthetic hello",
        "structured_content": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _session_with_two_tenants():
    message_a = _message(1, "tenant-a")
    message_b = _message(2, "tenant-b", content_text="other tenant secret")
    return _Session(
        {
            "id": {"tenant-a": [message_a], "tenant-b": [message_b]},
            "message_id": {"tenant-a": [(1, "contact_bob")], "tenant-b": [(2, "contact_eve")]},
            "wecom_userid": {
                "tenant-a": [("staff_alice", "Alice"), ("contact_bob", "Bob")],
                "tenant-b": [("staff_alice", "Mallory"), ("contact_eve", "Eve")],
            },
            "archive_message_id": {"tenant-a": [], "tenant-b": []},
            "roomid": {"tenant-a": [], "tenant-b": []},
        }
    )


def test_excel_generation_is_parseable_and_contains_projected_content() -> None:
    result = export_service.generate_export(
        _session_with_two_tenants(),
        "tenant-a",
        ExportSelection(roomid="room_demo_001"),
        ExportFormat.EXCEL,
    )

    workbook = load_workbook(BytesIO(result.content), read_only=True)
    rows = list(workbook["Evidence export"].iter_rows(values_only=True))
    assert rows[0] == (
        "Time (Beijing)",
        "Message ID",
        "Sender",
        "Conversation",
        "Participants",
        "Type",
        "Content",
    )
    assert rows[1][0] == "2025-01-01 08:00:00 CST"
    assert rows[1][1] == "msg-1"
    assert rows[1][2] == "Alice (staff_alice)"
    assert rows[1][3] == "Group chat · mo_001"
    assert rows[1][4] == "Alice (staff_alice), Bob (contact_bob)"
    assert rows[1][5:] == ("text", "fixed synthetic hello")
    info = dict(workbook["Export info"].iter_rows(values_only=True))
    assert info["Tenant ID"] == "tenant-a"
    assert info["Record count"] == "1"
    assert result.filename == "evidence-export.xlsx"
    assert result.record_count == 1


def test_pdf_generation_has_valid_pdf_structure_and_readable_content() -> None:
    result = export_service.generate_export(
        _session_with_two_tenants(),
        "tenant-a",
        ExportSelection(message_ids=("msg-1",)),
        ExportFormat.PDF,
    )

    # ReportLab is the approved PDF library. Its uncompressed output exposes
    # text operands for deterministic structural/content validation.
    assert result.content.startswith(b"%PDF-")
    assert result.content.rstrip().endswith(b"%%EOF")
    assert b"/Type /Page" in result.content
    pdf_text_operand = b"".join(b"\\000" + bytes((ord(char),)) for char in "fixed synthetic hello")
    assert pdf_text_operand in result.content
    assert result.filename == "evidence-export.pdf"
    assert result.record_count == 1


def test_unknown_type_uses_registry_name_not_raw_type_code() -> None:
    message = _message(1, "tenant-a", msgtype="future_protocol_type", content_text=None)
    projected = export_service._project_message(message, {}, {}, {})

    assert projected["type"] == describe_message_type("future_protocol_type")["normalized_type"]
    assert projected["content"] == "[unknown]"
    assert "future_protocol_type" not in projected["content"]


def test_export_never_projects_decrypted_payload_or_encrypted_envelope() -> None:
    message = _message(
        1,
        "tenant-a",
        decrypted_payload={"secret": "must not export"},
        raw_encrypted_payload={"ciphertext": "must not export"},
        encrypt_chat_msg="must not export",
    )
    projected = export_service._project_message(message, {}, {}, {})
    excel = export_service._render_excel([projected])
    pdf = export_service._render_pdf([projected])

    assert b"must not export" not in excel
    assert b"must not export" not in pdf


def test_media_export_is_metadata_only_and_does_not_scale_with_binary_payload() -> None:
    message = _message(
        1,
        "tenant-a",
        msgtype="file",
        content_text="ignored for media",
        structured_content={"fields": {"filename": "fixed-contract.pdf"}},
        media_binary=b"x" * 10_000_000,
    )
    projected = export_service._project_message(message, {}, {}, {1: ("file", 42)})
    small = export_service._render_excel([projected])
    message.media_binary = b"y" * 20_000_000
    large = export_service._render_excel([export_service._project_message(message, {}, {}, {1: ("file", 42)})])

    assert projected["content"] == "[file: fixed-contract.pdf, 42 bytes]"
    assert len(large) == len(small)
    assert b"x" * 100 not in large


def test_tenant_isolation_excludes_other_tenant_message_and_name() -> None:
    result = export_service.generate_export(
        _session_with_two_tenants(),
        "tenant-a",
        ExportSelection(start_ms=0),
        ExportFormat.EXCEL,
    )
    values = "\n".join(
        str(value)
        for row in load_workbook(BytesIO(result.content), read_only=True).active.iter_rows(values_only=True)
        for value in row
        if value is not None
    )

    assert "fixed synthetic hello" in values
    assert "other tenant secret" not in values
    assert "Mallory" not in values


def test_selection_must_be_bounded() -> None:
    try:
        export_service.generate_export(_session_with_two_tenants(), "tenant-a", ExportSelection(), ExportFormat.EXCEL)
    except ValueError as exc:
        assert "selection" in str(exc)
    else:
        raise AssertionError("unbounded export unexpectedly accepted")
