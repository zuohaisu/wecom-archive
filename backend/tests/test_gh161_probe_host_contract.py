"""GH-161 — production Host validation vs. the CD internal readiness probe.

Since ``APP_ENV=production`` landed in production ``backend/.env`` (GH-148
cutover), ``BrandingHostMiddleware`` accepts only configured platform
hosts: the loopback/localhost allowlist exists only outside production.
The CD probe (``scripts/deploy_server.sh``) connects to the loopback
``INTERNAL_HEALTH`` URL, so without an explicit Host header it presents
``Host: 127.0.0.1:<port>`` and is rejected with 421 **before the health
handler runs** — which auto-rolled back every CD deploy.

The deploy-side fix presents the configured ``ADMIN_DOMAIN`` hostname as
an explicit Host header. These tests pin the application-side contract
that fix depends on:

* production + default loopback Host  → 421 (never reaches the handler);
* production + configured Host        → the real readiness handler runs;
* a genuine readiness failure stays 503 with a valid Host (a valid Host
  must never fake health — the deploy gate still fails on it).

The rejected approaches (reverting APP_ENV, allowing arbitrary localhost
Hosts on business routes, treating 421 as healthy) stay rejected.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PROBE_HOST_LOOPBACK = "127.0.0.1:8035"  # what a header-less curl sends
CONFIGURED_HOST = "admin.example.com"  # the configured ADMIN_DOMAIN


@pytest.fixture()
def production_client(monkeypatch):
    """TestClient against the real app with production Host policy active.

    Settings are constructed per call (no cache — see app/settings.py), so
    the env vars are read at request time by BrandingHostMiddleware.
    """
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_DOMAIN", CONFIGURED_HOST)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    # app.db.session caches a module-level engine singleton keyed on first
    # access — reset it so this test's DATABASE_URL is the one used.
    import app.db.session as session_module

    session_module._engine = None
    from app.main import app

    return TestClient(app)


def test_production_rejects_default_loopback_probe_host_with_421(production_client):
    """The exact probe CD used to send — curl to the loopback listener with
    no Host header. In production this must be rejected with 421 before the
    health handler runs (this — not a 503 — is what failed every CD deploy)."""
    response = production_client.get("/health/ready", headers={"Host": PROBE_HOST_LOOPBACK})
    assert response.status_code == 421


def test_production_probe_with_configured_host_reaches_health_ready(production_client, monkeypatch):
    """The deploy fix's premise: with the configured ADMIN_DOMAIN hostname as
    Host, the request passes BrandingHostMiddleware and the real readiness
    handler executes and reports 200 when ready."""
    # sqlite URLs cannot take the pooled-engine kwargs in app.db.session, so
    # (as in the readiness-endpoint tests' failure paths) the engine is not
    # what is under test here — patch it to reach the readiness check itself.
    monkeypatch.setattr("app.main.get_engine", lambda: object())
    monkeypatch.setattr("app.main.full_readiness_check", lambda _engine: (True, "ok"))

    response = production_client.get("/health/ready", headers={"Host": CONFIGURED_HOST})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # A port on the Host header is stripped by the same parser the
    # middleware uses (hostname_from_host_header) — the comparison is on
    # hostname, mirroring platform_default_hosts().
    with_port = production_client.get("/health/ready", headers={"Host": f"{CONFIGURED_HOST}:8443"})
    assert with_port.status_code == 200


def test_production_genuine_readiness_failure_still_returns_503_with_configured_host(
    production_client, monkeypatch
):
    """A valid Host never fakes health: when the readiness check itself
    fails, the handler still returns 503 — which (via curl -f) must keep
    failing the deploy / triggering rollback. Only the Host rejection (421)
    is a probe/config error."""
    monkeypatch.setattr("app.main.get_engine", lambda: object())
    monkeypatch.setattr("app.main.full_readiness_check", lambda _engine: (False, "schema behind"))

    response = production_client.get("/health/ready", headers={"Host": CONFIGURED_HOST})
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def _development_client(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    import app.db.session as session_module

    session_module._engine = None
    from app.main import app

    return TestClient(app)


def test_development_loopback_host_still_reaches_health_live(monkeypatch):
    """Why this only surfaced at the GH-148 cutover: outside production the
    middleware adds loopback/test hosts, so the old header-less probe passed
    against the very same loopback URL."""
    client = _development_client(monkeypatch)
    response = client.get("/health/live", headers={"Host": PROBE_HOST_LOOPBACK})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
