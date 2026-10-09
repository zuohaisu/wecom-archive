"""RND-340 tenant-scoped customer-group metadata and display contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.group_chat_metadata import load_group_chat_display_names
from app.db.models import GroupChatMetadata, Tenant
from app.services.group_chat_metadata import (
    apply_group_chat_lookup,
    GroupChatSyncSummary,
    backfill_group_chat_metadata,
    normalize_group_chat_name,
)
from app.wecom_contacts import GroupChatMetadataLookup


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, GroupChatMetadata.__table__])
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE archive_messages ("
                "id INTEGER PRIMARY KEY, tenant_id TEXT, roomid TEXT)"
            )
        )
    with Session(engine) as session:
        yield session


def _lookup(name: object, status: str = "resolved") -> GroupChatMetadataLookup:
    return GroupChatMetadataLookup(name=name if isinstance(name, str) else None, status=status)


@pytest.mark.parametrize(
    "value",
    (
        "",
        " \t\n ",
        "\x00",
        "Sales\x00Team",
        "\u200b\u200e",
        "Sales\u200bTeam",
        "\ud800",
        "a" * 129,
    ),
)
def test_normalize_group_chat_name_rejects_blank_control_invisible_and_excessive_values(
    value: str,
) -> None:
    assert normalize_group_chat_name(value) is None


def test_normalize_group_chat_name_compacts_whitespace_and_preserves_ordinary_unicode() -> None:
    assert normalize_group_chat_name("  Sales\u3000Team  ") == "Sales Team"
    assert normalize_group_chat_name("研发组") == "研发组"


def test_apply_creates_updates_and_skips_same_effective_name_without_write_churn(db: Session) -> None:
    roomid = "test-room-1"
    created = apply_group_chat_lookup(
        db,
        tenant_id="tenant-a",
        roomid=roomid,
        lookup=_lookup("Initial Name"),
        checked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db.commit()
    assert (created.action, created.status) == ("created", "resolved")

    row = db.query(GroupChatMetadata).one()
    before_checked_at = row.last_checked_at
    skipped = apply_group_chat_lookup(
        db,
        tenant_id="tenant-a",
        roomid=roomid,
        lookup=_lookup(" Initial  Name "),
        checked_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert (skipped.action, skipped.status) == ("skipped", "resolved")
    assert row.last_checked_at == before_checked_at
    assert not db.is_modified(row)

    updated = apply_group_chat_lookup(
        db,
        tenant_id="tenant-a",
        roomid=roomid,
        lookup=_lookup("Renamed"),
    )
    assert (updated.action, updated.status) == ("updated", "resolved")
    assert row.display_name == "Renamed"


def test_invalid_name_never_erases_existing_valid_group_name(db: Session) -> None:
    apply_group_chat_lookup(
        db, tenant_id="tenant-a", roomid="test-room-2", lookup=_lookup("Usable Name")
    )
    db.commit()

    outcome = apply_group_chat_lookup(
        db,
        tenant_id="tenant-a",
        roomid="test-room-2",
        lookup=_lookup("\u200b\u200e"),
    )
    assert (outcome.action, outcome.status) == ("updated", "invalid_name")
    assert db.query(GroupChatMetadata).one().display_name == "Usable Name"


def test_same_roomid_is_strictly_tenant_isolated_for_write_and_lookup(db: Session) -> None:
    apply_group_chat_lookup(
        db, tenant_id="tenant-a", roomid="shared-room", lookup=_lookup("Tenant A")
    )
    apply_group_chat_lookup(
        db, tenant_id="tenant-b", roomid="shared-room", lookup=_lookup("Tenant B")
    )
    db.commit()

    assert load_group_chat_display_names(db, "tenant-a", ["shared-room"]) == {
        "shared-room": "Tenant A"
    }
    assert load_group_chat_display_names(db, "tenant-b", ["shared-room"]) == {
        "shared-room": "Tenant B"
    }


def test_backfill_is_bounded_deduplicated_and_idempotent(db: Session) -> None:
    db.execute(
        text(
            "INSERT INTO archive_messages (id, tenant_id, roomid) VALUES "
            "(1, 'tenant-a', 'room-a'), (2, 'tenant-a', 'room-a'), "
            "(3, 'tenant-a', 'room-b'), (4, 'tenant-b', 'room-a')"
        )
    )
    db.commit()

    def first_lookup(_token: str, roomid: str) -> GroupChatMetadataLookup:
        return _lookup({"room-a": "Alpha", "room-b": "Beta"}[roomid])

    first = backfill_group_chat_metadata(
        db, tenant_id="tenant-a", access_token="test-token", limit=10, lookup=first_lookup
    )
    db.commit()
    assert (first.scanned, first.resolved, first.created, first.updated, first.skipped) == (2, 2, 2, 0, 0)

    second = backfill_group_chat_metadata(
        db, tenant_id="tenant-a", access_token="test-token", limit=10, lookup=first_lookup
    )
    assert (second.scanned, second.resolved, second.created, second.updated, second.skipped) == (2, 2, 0, 0, 2)

    changed = backfill_group_chat_metadata(
        db,
        tenant_id="tenant-a",
        access_token="test-token",
        limit=10,
        lookup=lambda _token, roomid: _lookup("Renamed" if roomid == "room-a" else "Beta"),
    )
    assert (changed.created, changed.updated, changed.skipped) == (0, 1, 1)
    assert load_group_chat_display_names(db, "tenant-a", ["room-a"]) == {"room-a": "Renamed"}


def test_backfill_reports_unresolved_and_safe_error_categories(db: Session) -> None:
    db.execute(
        text("INSERT INTO archive_messages (id, tenant_id, roomid) VALUES (1, 'tenant-a', 'room-x')")
    )
    db.commit()
    summary = backfill_group_chat_metadata(
        db,
        tenant_id="tenant-a",
        access_token="test-token",
        limit=1,
        lookup=lambda _token, _roomid: _lookup(None, "permission_denied"),
    )
    assert (summary.scanned, summary.resolved, summary.unresolved, summary.errors) == (1, 0, 1, 0)
    assert db.query(GroupChatMetadata).one().sync_status == "permission_denied"


SENTINELS = (
    "SENTINEL_MESSAGE_BODY",
    "SENTINEL_STRUCTURED_CONTENT",
    "SENTINEL_PASSWORD",
    "SENTINEL_PASSWORD_HASH",
    "SENTINEL_TOKEN",
    "SENTINEL_SECRET",
    "SENTINEL_SIGNED_URL",
    "SENTINEL_STORAGE_KEY",
    "/sentinel/fs/path",
    "SENTINEL_SEARCH_TEXT",
    "SENTINEL_TRACEBACK",
    "SENTINEL_SENDER",
    "SENTINEL_RECIPIENT",
    "SENTINEL_ROOM",
    "SENTINEL_RAW_MSGID",
)


def assert_no_sentinels(obj, path: str = "$") -> None:
    """Recursively assert https://github.com/zuohaisu/wecom-archive/wiki/Agent-Data-Minimization §5's sentinel set."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert not any(s in str(key) for s in SENTINELS), f"{path}.{key}"
            assert_no_sentinels(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            assert_no_sentinels(value, f"{path}[{index}]")
    else:
        assert not any(s in str(obj) for s in SENTINELS), path


def _sentinel_tree() -> dict[str, object]:
    return {
        f"nested_{sentinel}": [sentinel, {f"value_{sentinel}": sentinel}]
        for sentinel in SENTINELS
    }


def test_group_chat_client_uses_official_post_contract_and_keeps_sensitive_values_out_of_logs(
    monkeypatch, caplog
) -> None:
    from app import wecom_contacts

    calls = []

    class Response:
        def json(self):
            return {"errcode": 0, "group_chat": {"name": "SENTINEL_SECRET"}}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, url, params=None, json=None):
            calls.append((url, params, json))
            return Response()

    monkeypatch.setattr(wecom_contacts.httpx, "Client", lambda timeout=None: Client())
    result = wecom_contacts.fetch_group_chat_metadata("SENTINEL_TOKEN", "SENTINEL_ROOM")

    assert result == GroupChatMetadataLookup(name="SENTINEL_SECRET", status="resolved")
    assert calls == [
        (
            "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/groupchat/get",
            {"access_token": "SENTINEL_TOKEN"},
            {"chat_id": "SENTINEL_ROOM", "need_name": 1},
        )
    ]
    assert "SENTINEL_TOKEN" not in caplog.text
    assert "SENTINEL_ROOM" not in caplog.text
    assert "SENTINEL_SECRET" not in caplog.text


def test_console_timeline_consumes_server_group_display_name_contract() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "web"
        / "static"
        / "console"
        / "timeline.js"
    ).read_text(encoding="utf-8")
    assert "data.room_display_name" in source
    assert "timeline-header" in source


@pytest.mark.parametrize(
    ("errcode", "expected"),
    [(40050, "not_found"), (48002, "permission_denied"), (45009, "rate_limited")],
)
def test_group_chat_client_classifies_official_error_codes(monkeypatch, errcode, expected) -> None:
    from app import wecom_contacts

    class Response:
        def json(self):
            return {"errcode": errcode, "errmsg": "provider text is never persisted"}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(wecom_contacts.httpx, "Client", lambda timeout=None: Client())
    assert wecom_contacts.fetch_group_chat_metadata("test-token", "test-room").status == expected


def test_group_chat_client_separates_transport_from_malformed_responses(monkeypatch) -> None:
    """Provider payload parsing failures are not transport failures (QA-002)."""
    from app import wecom_contacts

    events = iter(
        (
            ("request_error", wecom_contacts.httpx.TimeoutException("timeout")),
            ("json_error", ValueError("not json")),
            ("json", ["not an object"]),
            ("json", {"errcode": 0}),
            ("json", {"errcode": 0, "group_chat": []}),
            ("json", {"errcode": 0, "group_chat": {}}),
        )
    )

    class Response:
        def __init__(self, event):
            self.event = event

        def json(self):
            kind, value = self.event
            if kind == "json_error":
                raise value
            return value

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, *args, **kwargs):
            event = next(events)
            if event[0] == "request_error":
                raise event[1]
            return Response(event)

    monkeypatch.setattr(wecom_contacts.httpx, "Client", lambda timeout=None: Client())
    statuses = [
        wecom_contacts.fetch_group_chat_metadata("test-token", "test-room").status
        for _ in range(6)
    ]
    assert statuses == [
        "transport_error",
        "malformed_response",
        "malformed_response",
        "malformed_response",
        "malformed_response",
        "missing_name",
    ]


def _load_group_metadata_cli():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "sync_group_chat_metadata_once.py"
    spec = importlib.util.spec_from_file_location("rnd340_group_metadata_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_group_metadata_managed_surfaces_recursively_exclude_sentinels(
    db: Session, monkeypatch, caplog, capsys
) -> None:
    """Exercise RND-340 outputs against https://github.com/zuohaisu/wecom-archive/wiki/Agent-Data-Minimization §5.

    The customer-group title and raw archive room correlation are explicit
    RND-340 persistence requirements, so this fixture uses safe ordinary
    values for those two stored fields.  Every forbidden sentinel is instead
    nested in the raw provider response's ignored fields and must be absent
    from all managed output surfaces.
    """
    from app import wecom_contacts
    from app.schemas.timeline import ConversationMessagesOut

    class Response:
        def json(self):
            return {
                "errcode": 0,
                "group_chat": {"name": "Verified group"},
                "provider_only": _sentinel_tree(),
            }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(wecom_contacts.httpx, "Client", lambda timeout=None: Client())
    with caplog.at_level(logging.INFO, logger="app.wecom_contacts"):
        resolved = wecom_contacts.fetch_group_chat_metadata(
            "SENTINEL_TOKEN", "SENTINEL_ROOM"
        )

        class FailingClient(Client):
            def post(self, *args, **kwargs):
                raise RuntimeError("SENTINEL_TRACEBACK")

        monkeypatch.setattr(
            wecom_contacts.httpx, "Client", lambda timeout=None: FailingClient()
        )
        failed = wecom_contacts.fetch_group_chat_metadata(
            "SENTINEL_TOKEN", "SENTINEL_ROOM"
        )

    assert resolved == GroupChatMetadataLookup(name="Verified group", status="resolved")
    assert failed == GroupChatMetadataLookup(name=None, status="transport_error")
    applied = apply_group_chat_lookup(
        db,
        tenant_id="tenant-a",
        roomid="safe-test-room",
        lookup=resolved,
    )
    db.commit()
    assert applied.status == "resolved"
    metadata = db.query(GroupChatMetadata).one()

    api_model = ConversationMessagesOut(
        messages=[],
        pagination={"has_older": False},
        room_display_name="Verified group",
        provider_only=_sentinel_tree(),
    )
    api_response = (
        api_model.model_dump() if hasattr(api_model, "model_dump") else api_model.dict()
    )

    cli = _load_group_metadata_cli()
    monkeypatch.setattr(cli, "_parse_args", lambda: SimpleNamespace(limit=1))
    monkeypatch.setattr(
        cli,
        "_require_env",
        lambda name: {
            "DATABASE_URL": "SENTINEL_SIGNED_URL",
            "WECOM_CORP_ID": "safe-corp",
            "WECOM_EXTERNAL_CONTACT_SECRET": "SENTINEL_SECRET",
        }[name],
    )
    monkeypatch.setattr(cli, "get_wecom_token", lambda *args, **kwargs: "SENTINEL_TOKEN")
    monkeypatch.setattr(cli, "create_engine", lambda _url: object())

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def commit(self):
            return None

    monkeypatch.setattr(cli, "Session", lambda _engine: FakeSession())
    monkeypatch.setattr(cli, "_require_tenant_id", lambda *_args: "safe-tenant")
    monkeypatch.setattr(
        cli,
        "backfill_group_chat_metadata",
        lambda *_args, **_kwargs: GroupChatSyncSummary(scanned=1, resolved=1, created=1),
    )
    assert cli.main() == 0
    cli_success_output = capsys.readouterr().out

    def _raise_sentinel_error(*_args, **_kwargs):
        raise RuntimeError("SENTINEL_TRACEBACK")

    monkeypatch.setattr(cli, "get_wecom_token", _raise_sentinel_error)
    assert cli.main() == 1
    cli_failure_output = capsys.readouterr().out

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to exercise the real timeline script")
    timeline_path = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "console" / "timeline.js"
    node_harness = r'''
const fs = require("fs");
const vm = require("vm");
const sentinels = [
  "SENTINEL_MESSAGE_BODY", "SENTINEL_STRUCTURED_CONTENT", "SENTINEL_PASSWORD",
  "SENTINEL_PASSWORD_HASH", "SENTINEL_TOKEN", "SENTINEL_SECRET", "SENTINEL_SIGNED_URL",
  "SENTINEL_STORAGE_KEY", "/sentinel/fs/path", "SENTINEL_SEARCH_TEXT", "SENTINEL_TRACEBACK",
  "SENTINEL_SENDER", "SENTINEL_RECIPIENT", "SENTINEL_ROOM", "SENTINEL_RAW_MSGID"
];
const providerOnly = Object.fromEntries(sentinels.map((s) => ["nested_" + s, [s, {["value_" + s]: s}]]));
const header = {textContent: "", attributes: {}};
const body = {innerHTML: "", attributes: {}, scrollTop: 0, scrollHeight: 0, clientHeight: 0};
const history = {innerHTML: "", attributes: {}};
const elements = {"timeline-header": header, "timeline-body": body, "timeline-history-status": history};
const consoleCalls = [];
const storage = () => ({values: {}, setItem(k, v) { this.values[k] = String(v); }, getItem(k) { return this.values[k] || null; }});
global.document = {body: {innerHTML: "", attributes: {}}, getElementById: (id) => elements[id] || null};
global.localStorage = storage();
global.sessionStorage = storage();
global.console = {log: (...args) => consoleCalls.push(args), info: (...args) => consoleCalls.push(args), warn: (...args) => consoleCalls.push(args), error: (...args) => consoleCalls.push(args)};
global.I18N = {t: () => "Timeline"};
global.handleUnauth = () => false;
global.timelineMode = "";
global.timelineEntityId = "";
global.timelineConvType = "group";
global.timelineConvId = "safe-room";
global.timelineRequestGen = 1;
global.focusPending = false;
global.focusMsgId = "";
global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({room_display_name: "Verified group", messages: [], pagination: {has_older: false, next_before: null}, provider_only: providerOnly})});
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
global.renderTimeline = () => {};
global.startHistoryObserver = () => {};
fetchTimelinePage(null, true).then(() => process.stdout.write(JSON.stringify({
  document_body: document.body.innerHTML,
  text_nodes: [header.textContent, body.innerHTML, history.innerHTML],
  attributes: [document.body.attributes, header.attributes, body.attributes, history.attributes],
  local_storage: localStorage.values,
  session_storage: sessionStorage.values,
  console_calls: consoleCalls
}))).catch((error) => { process.stderr.write(String(error)); process.exit(1); });
'''
    completed = subprocess.run(
        [node, "-e", node_harness, str(timeline_path)],
        check=True,
        text=True,
        capture_output=True,
    )

    persisted_columns = {
        column.name: getattr(metadata, column.name)
        for column in GroupChatMetadata.__table__.columns
    }
    own_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.wecom_contacts"
    ]
    assert_no_sentinels(
        {
            "provider_result": {"name": resolved.name, "status": resolved.status},
            "persistence": persisted_columns,
            "audit_payloads": [],
            "api_response": api_response,
            "logs": own_logs,
            "cli_output": [cli_success_output, cli_failure_output],
            "frontend": json.loads(completed.stdout),
        }
    )
