"""RND-363: conversation-page delete selection UI and API contract tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AdminUser, ArchiveMessage, AuditLog, MediaFile, Tenant
from tests._node_runner import run_node
from tests._rnd216_web_shims import review_console_js_source

_TENANT_A = "tenant-a"
NODE = shutil.which("node")
_BACKEND = Path(__file__).resolve().parent.parent
_DELETE_JS = (_BACKEND / "app/web/static/console/delete-timeline.js").read_text(encoding="utf-8")
_CONSOLE_JS = review_console_js_source()

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    fts_index = next(
        index
        for index in ArchiveMessage.__table__.indexes
        if index.name == "ix_archive_messages_content_text_fts"
    )
    ArchiveMessage.__table__.indexes.remove(fts_index)
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                Tenant.__table__,
                AdminUser.__table__,
                ArchiveMessage.__table__,
                MediaFile.__table__,
                AuditLog.__table__,
            ],
        )
    finally:
        ArchiveMessage.__table__.indexes.add(fts_index)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Tenant(id=_TENANT_A, name="A", slug="tenant-a"))
        session.add(
            AdminUser(
                id="admin-1",
                tenant_id=_TENANT_A,
                wecom_user_id="staff_owner",
                name="Owner",
                role="owner",
            )
        )
        session.commit()
    return factory


def _insert_message(db, **kwargs):
    _insert_message._counter += 1
    values = {
        "id": kwargs.pop("id", _insert_message._counter),
        "msgid": kwargs.pop("msgid", "msg-%d" % _insert_message._counter),
        "seq": 1,
        "publickey_ver": 1,
        "encrypt_random_key": "k",
        "encrypt_chat_msg": "c",
        "decrypt_status": "success",
        "tenant_id": kwargs.pop("tenant_id", _TENANT_A),
    }
    values.update(kwargs)
    with db() as session:
        message = ArchiveMessage(**values)
        session.add(message)
        session.commit()
        session.refresh(message)
        return message


_insert_message._counter = 0


def _run(extra: str):
    result = run_node(
        f"""
var deleteMode=false,deleteSelection={{}},deleteStatus=null,timelineMsgs=[],selConvId=null;
function renderTimeline(){{}}
var __els={{}};
function __stub(id){{if(!__els[id]){{__els[id]={{hidden:false,textContent:'',disabled:false,focus:function(){{}},classList:{{toggle:function(){{}},add:function(){{}},remove:function(){{}}}}}};}}return __els[id];}}
var document={{getElementById:__stub,querySelector:function(){{return null;}}}};
var fetch=function(url,opts){{return Promise.resolve({{ok:true,json:function(){{return Promise.resolve({{deleted:2,already_deleted:0,not_found:0,deleted_message_ids:['m1','m2']}});}}}});}};
/* I18N_CORE_START{_CONSOLE_JS.split('/* I18N_CORE_START',1)[1].split('I18N_CORE_END */',1)[0]}I18N_CORE_END */
I18N.setLocale('zh-CN');
{_DELETE_JS}
{extra}
"""
    )
    assert result.returncode == 0, f"node harness failed: {result.stderr}"
    return result.stdout


def test_delete_mode_requires_eligibility_and_toggles_cleanly() -> None:
    out = _run(
        """
deleteStatus={can_delete:true,deletion_locked:false};selConvId='conv-1';
toggleDeleteMode();
console.log('on='+deleteMode);
toggleMessageForDelete('m1');toggleMessageForDelete('m2');toggleMessageForDelete('m1');
console.log('count='+countDeleteSelection());
toggleDeleteMode();
console.log('off='+deleteMode+' cleared='+Object.keys(deleteSelection).length);
deleteStatus={can_delete:false,deletion_locked:false};
toggleDeleteMode();
console.log('denied='+deleteMode);
deleteStatus={can_delete:true,deletion_locked:true};
toggleDeleteMode();
console.log('locked='+deleteMode);
"""
    )
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == [
        "on=true",
        "count=1",
        "off=false cleared=0",
        "denied=false",
        "locked=false",
    ]


def test_select_all_loaded_and_selection_clear() -> None:
    out = _run(
        """
deleteStatus={can_delete:true,deletion_locked:false};selConvId='conv-1';
timelineMsgs=[{msgid:'m1'},{msgid:'m2'},{msgid:'m3'}];
toggleDeleteMode();
toggleSelectAllLoaded();
console.log('all='+timelineMsgs.every(function(m){return deleteSelection[m.msgid];})+' count='+countDeleteSelection());
toggleSelectAllLoaded();
console.log('none='+(countDeleteSelection()===0));
clearDeleteSelection();
console.log('cleared='+Object.keys(deleteSelection).length);
"""
    )
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == ["all=true count=3", "none=true", "cleared=0"]


def test_delete_removes_only_reported_msgids_and_keeps_order() -> None:
    out = _run(
        """
deleteStatus={can_delete:true,deletion_locked:false};selConvId='conv-1';
timelineMsgs=[{msgid:'m1'},{msgid:'m2'},{msgid:'m3'},{msgid:'m4'}];
toggleDeleteMode();
['m1','m2','m3'].forEach(function(id){toggleMessageForDelete(id);});
confirmDeleteSelected();
document.getElementById('btn-delete-confirm-ok').onclick();
setTimeout(function(){
  console.log('remaining='+timelineMsgs.map(function(m){return m.msgid;}).join(','));
  console.log('selectionCleared='+(countDeleteSelection()===0));
},10);
"""
    )
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == ["remaining=m3,m4", "selectionCleared=true"]


# ---------------------------------------------------------------------------
# Backend API contract
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _authed(app, db_session, tenant_id, role="owner"):
    from app.auth import get_current_user
    from app.db.session import get_db

    def _db_gen():
        with db_session() as session:
            yield session

    user = MagicMock()
    user.id = "admin-1"
    user.role = role
    app.dependency_overrides[get_current_user] = lambda: (user, tenant_id)
    app.dependency_overrides[get_db] = _db_gen


def test_deletion_status_reflects_role_and_tenant_hold(client, db) -> None:
    from app.main import app

    _insert_message(db, msgtype="text", content_text="x", sender="staff_a", msgtime=1)
    _authed(app, db, _TENANT_A, role="owner")
    try:
        status = client.get("/api/admin/messages/deletion-status")
    finally:
        app.dependency_overrides.clear()
    assert status.status_code == 200
    assert status.json() == {"can_delete": True, "deletion_locked": False}

    _authed(app, db, _TENANT_A, role="compliance")
    try:
        status = client.get("/api/admin/messages/deletion-status")
    finally:
        app.dependency_overrides.clear()
    assert status.json() == {"can_delete": False, "deletion_locked": False}

    with db() as session:
        tenant = session.get(Tenant, _TENANT_A)
        tenant.deletion_locked = True
        session.commit()
    _authed(app, db, _TENANT_A, role="owner")
    try:
        status = client.get("/api/admin/messages/deletion-status")
    finally:
        app.dependency_overrides.clear()
    assert status.json() == {"can_delete": True, "deletion_locked": True}
    with db() as session:
        tenant = session.get(Tenant, _TENANT_A)
        tenant.deletion_locked = False
        session.commit()


def test_delete_api_returns_deleted_message_ids(client, db) -> None:
    from app.main import app

    _insert_message(db, msgtype="text", content_text="one", sender="staff_a", msgid="rnd363-1", msgtime=1)
    _insert_message(db, msgtype="text", content_text="two", sender="staff_a", msgid="rnd363-2", msgtime=2)
    _authed(app, db, _TENANT_A, role="owner")
    try:
        response = client.post(
            "/api/admin/messages/delete",
            json={"message_ids": ["rnd363-1", "rnd363-2", "missing"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == 2
    assert body["already_deleted"] == 0
    assert body["not_found"] == 1
    assert body["deleted_message_ids"] == ["rnd363-1", "rnd363-2"]
