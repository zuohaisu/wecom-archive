"""Shared fixtures for all tests in backend/tests."""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_db


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Create an isolated SQLite database session for each test."""
    from cryptography.fernet import Fernet

    # Use in-memory SQLite with foreign keys enabled
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )

    # Import models AFTER engine creation to avoid circular imports
    from app.db.models import Base, PlatformAdmin, Tenant, TenantWecomConfig

    # Create only the tables needed for this module
    Base.metadata.create_all(
        bind=engine,
        tables=[
            PlatformAdmin.__table__,
            Tenant.__table__,
            TenantWecomConfig.__table__,
        ],
    )

    # Set field encryption key for crypto operations
    import os

    if "FIELD_ENCRYPTION_KEY" not in os.environ:
        os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session) -> Generator[TestClient, None, None]:
    """Create a TestClient with db_session override."""
    from app.main import app

    print(f"DEBUG client fixture - db_session: {db_session}")
    
    def override_db() -> Generator[Session, None, None]:
        print(f"DEBUG override_db called, yielding: {db_session}")
        yield db_session
        print(f"DEBUG override_db finished")

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        print("DEBUG client fixture - yielding test_client")
        yield test_client
    print("DEBUG client fixture - clearing overrides")
    app.dependency_overrides.clear()


@pytest.fixture
def platform_admin_user(db_session: Session):
    """Create or return platform admin for test auth - matches test_rnd311 pattern."""
    from app.auth import hash_password
    from app.db.models import PlatformAdmin

    email = "platform@example.test"
    password = "test-password"

    existing = db_session.query(PlatformAdmin).filter(PlatformAdmin.email == email).first()
    print(f"DEBUG platform_admin_user fixture - checking existing PA...")
    if not existing:
        pa = PlatformAdmin(
            id="rnd314-platform-admin",
            email=email,
            password_hash=hash_password(password),
            role="superadmin",
            status="active",
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)
        result = pa
    else:
        result = existing
    print(f"DEBUG platform_admin_user fixture - returning: {result.id}, {result.email}")
    # Debug: log password hash
    from app.auth import verify_password
    pwd_ok = verify_password(password, result.password_hash)
    print(f"DEBUG platform_admin_user fixture - password verification: {pwd_ok}")
    return result
