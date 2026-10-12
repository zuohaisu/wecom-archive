"""GH-226: default message IDs must not depend on recycled dict addresses."""

from importlib import import_module

import pytest
from sqlalchemy.orm import Session


@pytest.fixture(params=[
    "tests.fakes",
    "tests.test_listing_service",
    "tests.test_conversation_membership_service",
])
def message_helper(request):
    module = import_module(request.param)
    if request.param == "tests.fakes":
        db = Session(module.make_worker_engine())
        insert = module.insert_archive_message
    else:
        db = module._make_session()
        insert = module._insert_message
    try:
        yield module, db, insert
    finally:
        engine = db.get_bind()
        db.close()
        engine.dispose()


def test_default_msgids_survive_reused_object_addresses(message_helper, monkeypatch):
    module, db, insert = message_helper
    # Deterministically reproduce the old allocator-dependent collision.
    monkeypatch.setattr(module, "id", lambda _: 42, raising=False)
    messages = [insert(db) for _ in range(20)]
    assert {message.seq for message in messages} == {1}
    assert len({message.id for message in messages}) == 20
    assert len({message.msgid for message in messages}) == 20


def test_explicit_msgids_are_preserved_across_tenants(message_helper):
    _, db, insert = message_helper
    # Consumers intentionally reuse an upstream ID for tenant-isolation tests.
    first = insert(db, msgid="shared-msgid", tenant_id="tenant-a")
    second = insert(db, msgid="shared-msgid", tenant_id="tenant-b")
    assert first.msgid == second.msgid == "shared-msgid"
    assert first.id != second.id
