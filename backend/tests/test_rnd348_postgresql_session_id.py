"""Real-PostgreSQL regression for RND-348 provisioning session IDs."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.models import AdminSession, WecomOrganizationClaim
from app.services.organization_provisioning import provision_organization


_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
try:
    _IS_POSTGRESQL = bool(
        _DATABASE_URL and make_url(_DATABASE_URL).get_backend_name() == "postgresql"
    )
except Exception:
    _IS_POSTGRESQL = False


@pytest.mark.skipif(not _IS_POSTGRESQL, reason="PostgreSQL DATABASE_URL not set")
def test_provisioning_session_id_fits_real_postgresql_varchar_36() -> None:
    """The full provisioning commit must succeed against VARCHAR(36)."""
    engine = create_engine(_DATABASE_URL)
    if not inspect(engine).has_table("wecom_organization_claims"):
        pytest.skip("Database is not migrated through RND-348")

    connection = engine.connect()
    outer_transaction = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    raw_claim_ref = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    claim = WecomOrganizationClaim(
        id=str(uuid.uuid4()),
        public_ref_hash=hashlib.sha256(raw_claim_ref.encode()).hexdigest(),
        corp_id=f"ww-rnd348-{uuid.uuid4()}",
        corp_name="RND-348 PostgreSQL regression",
        authorized_subject=f"rnd348-{uuid.uuid4()}",
        agent_id="1000009",
        permanent_code_encrypted="postgresql-regression-ciphertext",
        state="pending",
        expires_at=now + timedelta(minutes=5),
    )
    try:
        db.add(claim)
        db.commit()

        result = provision_organization(db, raw_claim_ref)

        assert len(result.session_id) == 36
        assert str(uuid.UUID(result.session_id)) == result.session_id
        assert db.get(AdminSession, result.session_id) is not None
    finally:
        db.close()
        outer_transaction.rollback()
        connection.close()
        engine.dispose()
