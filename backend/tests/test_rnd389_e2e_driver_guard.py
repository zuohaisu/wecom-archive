"""RND-389 E2E driver guards: production refusal, env classification, and the
offline protocol round-trip.

No live network is ever touched in CI.  The FakeHttp below implements the
same JSON protocol the runner drives (status/config/test/activate/plan/orders/
login), backed by a sqlite mirror of the rows the real server would write, so
the whole happy-path orchestration — including the negative probes — is
exercised deterministically.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import make_worker_engine  # noqa: E402

from scripts.e2e_rnd389_self_service_activation import (  # noqa: E402
    DbProbe,
    E2EConfig,
    E2EGuardRefused,
    E2ERunner,
    classify_environment,
    guard_environment,
)

from app.db.base import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    ArchiveMessage,
    BillingPlan,
    Subscription,
    SyncState,
    ThirdPartyOrganizationBinding,
)

CORRECT_SECRET = "rnd389-e2e-correct-secret"
WRONG_SECRET = "rnd389-e2e-wrong-secret"
SESION_ID = "session-rnd389-e2e"
CORP_ID = "ww-rnd389-e2e-corp"
TENANT_ID = "tenant-rnd389-e2e"
ANNUAL_CODE = "annual_base_cny_99"


@pytest.fixture()
def db_factory():
    # The worker fake schema covers the ORM columns for subscriptions,
    # sync_states and archive_messages; the binding table is plain columns
    # with no postgres-only features, so metadata create_all handles it.
    engine = make_worker_engine()
    Base.metadata.create_all(
        engine, tables=[ThirdPartyOrganizationBinding.__table__]
    )
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            BillingPlan(
                id="annual-plan-rnd389",
                code=ANNUAL_CODE,
                display_name="Annual",
                is_active=True,
                amount_cents=9900,
                currency="CNY",
                billing_period_months=12,
                storage_quota_bytes=1024,
            )
        )
        db.add(
            ThirdPartyOrganizationBinding(
                id="binding-rnd389",
                tenant_id=TENANT_ID,
                corp_id=CORP_ID,
                agent_id="1000002",
                permanent_code_encrypted="encrypted",
                authorization_mode="admin",
            )
        )
        now = datetime.now(timezone.utc)
        db.add(
            SyncState(
                tenant_id=TENANT_ID,
                corp_id=CORP_ID,
                last_seq=0,
                status="idle",
                started_at=now,
            )
        )
        # One archived message pre-seeded: the first-message wait resolves
        # immediately offline.
        db.add(
            ArchiveMessage(
                tenant_id=TENANT_ID,
                msgid="msg-rnd389-1",
                seq=1,
                publickey_ver=1,
                encrypt_random_key="key",
                encrypt_chat_msg="payload",
                msgtime=int(now.timestamp()),
                msgtype="text",
            )
        )
        db.commit()
    return factory


def _config() -> E2EConfig:
    return E2EConfig(
        base_url="https://rnd389-staging.example",
        session_id=SESION_ID,
        archive_secret=CORRECT_SECRET,
        wrong_secret=WRONG_SECRET,
        private_key_pem="-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
        publickey_version=1,
        callback_token="rnd389-token",
        callback_aes_key="rnd389callbackaeskey0123456789abcdef0123456789",
        poll_seconds=0.0,
        activation_timeout=5.0,
        message_timeout=5.0,
        session_id_2=None,
        db_url="",
    )


class FakeHttp:
    """State-machine twin of the deployment: the server-side rows the real
    gates would write are mirrored in the same sqlite engine the DbProbe
    reads, so the runner's HTTP assertions and DB assertions agree."""

    def __init__(self, factory, *, secret: str, session_id: str):
        self.factory = factory
        self.stored_secret: str | None = None
        self.session_id = session_id
        self.lifecycle = "provisioning"
        self.last_code: str | None = None
        self.calls: list[str] = []

    def _activation_payload(self, state: str, code: str | None = None) -> dict:
        return {"state": state, "gate_results": {}, "safe_error_code": code, "revision": 1}

    def _has_subscription(self) -> bool:
        with self.factory() as db:
            return db.query(Subscription).count() > 0

    def provisioning_status(self):
        self.calls.append("status")
        if self.lifecycle == "active":
            # After promotion the session is admin-scoped, so the
            # provisioning guard answers 401 (mirrors get_provisioning_user).
            return 401, None
        if self.stored_secret is None:
            return 200, {
                "lifecycle_status": "provisioning",
                "archive_enabled": False,
                "activation": self._activation_payload("not_started"),
                "allowed_actions": ["view_status", "purchase_plan", "view_settings"],
            }
        if self.last_code is not None:
            return 200, {
                "lifecycle_status": "provisioning",
                "archive_enabled": False,
                "activation": self._activation_payload("blocked", self.last_code),
                "allowed_actions": ["view_status", "purchase_plan", "view_settings"],
            }
        return 200, {
            "lifecycle_status": "provisioning",
            "archive_enabled": False,
            "activation": self._activation_payload("ready"),
            "allowed_actions": [
                "view_status",
                "purchase_plan",
                "view_settings",
                "activate",
            ],
        }

    def provisioning_config(self):
        self.calls.append("config")
        return 200, {"org": {"corp_id": CORP_ID}}

    def put_provisioning_config(self, updates):
        self.calls.append("put")
        secret = (updates or {}).get("archive_secret")
        if not secret:
            return 400, {"errors": [{"key": "archive_secret", "code": "required"}]}
        self.stored_secret = secret
        self.last_code = None
        return 200, {"missing": []}

    def test_provisioning_config(self):
        self.calls.append("test")
        ok = self.stored_secret == CORRECT_SECRET
        return 200, {"all_ok": ok, "fields": {}}

    def activate(self):
        self.calls.append("activate")
        if self.lifecycle == "active":
            # Same guard as provisioning_status: the promoted session is
            # admin-scoped, so a second HTTP activate is unreachable (401).
            return 401, None
        if self.stored_secret != CORRECT_SECRET:
            self.last_code = "credentials_invalid"
            return 200, {
                "activated": False,
                "replayed": False,
                "activation": self._activation_payload("blocked", "credentials_invalid"),
                "worker_dispatch": None,
            }
        if self._has_subscription():
            self.last_code = "no_entitlement"
            return 200, {
                "activated": False,
                "replayed": False,
                "activation": self._activation_payload("blocked", "no_entitlement"),
                "worker_dispatch": None,
            }
        # Trial grant + promotion, mirroring the server's activate_tenant.
        import uuid

        with self.factory() as db:
            now = datetime.now(timezone.utc)
            db.add(
                Subscription(
                    id=uuid.uuid4().hex,
                    tenant_id=TENANT_ID,
                    plan_id="annual-plan-rnd389",
                    status="trial",
                    source="self_service_trial",
                    starts_at=now,
                    ends_at=now + timedelta(days=15),
                    grace_ends_at=now + timedelta(days=16),
                )
            )
            db.commit()
        self.lifecycle = "active"
        return 200, {
            "activated": True,
            "replayed": False,
            "activation": self._activation_payload("active"),
            "worker_dispatch": "activation",
        }

    def admin_login(self):
        self.calls.append("login")
        if self.lifecycle == "active":
            return 302, "/dashboard"
        return 200, None

    def billing_plan(self):
        self.calls.append("plan")
        return 200, {"code": ANNUAL_CODE, "payment_enabled": True}

    def create_order(self, plan_code):
        self.calls.append("create_order")
        return 201, {"order_id": "ord-rnd389-1", "status": "pending"}

    def close_order(self, order_id):
        self.calls.append("close_order")
        return 200, {"order_id": order_id, "status": "closed"}


def _run_offline(factory, http) -> E2ERunner:
    db = DbProbe("sqlite://")
    db._factory = factory  # share the seeded engine
    runner = E2ERunner(http=http, db=db, config=_config())
    exit_code = runner.run()
    return exit_code, runner


# ---------------------------------------------------------------------------
# Guards & environment classification
# ---------------------------------------------------------------------------


def test_classify_environment_defaults_to_non_production():
    assert classify_environment({}) == "non-production"
    assert classify_environment({"APP_ENV": "development"}) == "non-production"
    assert classify_environment({"APP_ENV": "Production"}) == "production"


def test_requires_arming_flag():
    with pytest.raises(E2EGuardRefused, match="RND389_E2E=1"):
        guard_environment({})


def test_refuses_production_even_when_armed():
    with pytest.raises(E2EGuardRefused, match="production"):
        guard_environment({"RND389_E2E": "1", "APP_ENV": "production"})


def test_non_production_armed_passes_guard():
    assert guard_environment({"RND389_E2E": "1", "APP_ENV": "staging"}) == "non-production"


def test_config_from_env_requires_secrets():
    env = {
        "RND389_E2E": "1",
        "RND389_BASE_URL": "https://staging.example",
        "RND389_SESSION_ID": SESION_ID,
        # archive secret missing
        "RND389_PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
        "RND389_PUBLIC_KEY_VERSION": "1",
        "RND389_CALLBACK_TOKEN": "t",
        "RND389_CALLBACK_AES_KEY": "a" * 43,
    }
    with pytest.raises(E2EGuardRefused, match="RND389_ARCHIVE_SECRET"):
        E2EConfig.from_env(env)


def test_config_reads_private_key_from_file(tmp_path):
    key_file = tmp_path / "private.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nFROMFILE\n-----END PRIVATE KEY-----")
    env = {
        "RND389_E2E": "1",
        "RND389_BASE_URL": "https://staging.example",
        "RND389_SESSION_ID": SESION_ID,
        "RND389_ARCHIVE_SECRET": CORRECT_SECRET,
        "RND389_PRIVATE_KEY_FILE": str(key_file),
        "RND389_PUBLIC_KEY_VERSION": "1",
        "RND389_CALLBACK_TOKEN": "t",
        "RND389_CALLBACK_AES_KEY": "a" * 43,
    }
    config = E2EConfig.from_env(env)
    assert config.private_key_pem == "-----BEGIN PRIVATE KEY-----\nFROMFILE\n-----END PRIVATE KEY-----"


def test_config_rejects_both_key_sources(tmp_path):
    key_file = tmp_path / "private.pem"
    key_file.write_text("x")
    env = {
        "RND389_E2E": "1",
        "RND389_BASE_URL": "https://staging.example",
        "RND389_SESSION_ID": SESION_ID,
        "RND389_ARCHIVE_SECRET": CORRECT_SECRET,
        "RND389_PRIVATE_KEY_FILE": str(key_file),
        "RND389_PRIVATE_KEY_PEM": "y",
        "RND389_PUBLIC_KEY_VERSION": "1",
        "RND389_CALLBACK_TOKEN": "t",
        "RND389_CALLBACK_AES_KEY": "a" * 43,
    }
    with pytest.raises(E2EGuardRefused, match="exactly one"):
        E2EConfig.from_env(env)


# ---------------------------------------------------------------------------
# Offline protocol round-trip
# ---------------------------------------------------------------------------


def test_offline_happy_path_round_trip(db_factory):
    http = FakeHttp(db_factory, secret=CORRECT_SECRET, session_id=SESION_ID)
    exit_code, runner = _run_offline(db_factory, http)

    assert exit_code == 0, [s for s in runner.table.steps if s.status == "fail"]
    by_name = {step.name: step.status for step in runner.table.steps}
    for step in ("provisioning_session", "negative_wrong_secret", "negative_no_entitlement",
                 "configure", "self_test", "wait_ready", "activate", "replay_idempotent",
                 "post_activation_access", "billing_probe", "first_message"):
        assert by_name[step] == "ok", f"{step} -> {by_name.get(step)}"
    assert by_name["second_tenant_isolation"] == "skip"

    # Protocol ordering: session probe first, org snapshot second, the
    # wrong-secret PUT is the first write, wrong-secret probe before the fix,
    # replay after the first activation, billing after access transition.
    assert http.calls[:3] == ["status", "config", "put"]
    assert "activate" in http.calls
    wrong_activate = http.calls.index("activate")
    assert http.calls.index("put") < wrong_activate  # wrong-secret PUT first
    fix_put = http.calls.index("put", http.calls.index("test"))
    assert fix_put < http.calls.index("activate", wrong_activate + 1)
    replay_at = http.calls.index("activate", http.calls.index("activate") + 1)
    assert replay_at < http.calls.index("login")
    assert http.calls.index("login") < http.calls.index("plan")
    assert http.calls.index("create_order") < http.calls.index("close_order")

    # DB side agrees: exactly one trial subscription after the run.
    with db_factory() as db:
        rows = db.query(Subscription).all()
        assert len(rows) == 1
        assert rows[0].source == "self_service_trial"


def test_offline_steps_never_leak_secrets(db_factory):
    http = FakeHttp(db_factory, secret=CORRECT_SECRET, session_id=SESION_ID)
    exit_code, runner = _run_offline(db_factory, http)
    assert exit_code == 0
    rendered = " ".join(f"{s.name} {s.detail}" for s in runner.table.steps)
    assert CORRECT_SECRET not in rendered
    assert WRONG_SECRET not in rendered
    assert SESION_ID not in rendered
    assert CORP_ID not in rendered
    assert TENANT_ID not in rendered


def test_offline_failure_surfaces_failed_step(db_factory):
    class BrokenHttp(FakeHttp):
        def test_provisioning_config(self):
            self.calls.append("test")
            return 200, {"all_ok": False, "fields": {}}

    http = BrokenHttp(db_factory, secret=CORRECT_SECRET, session_id=SESION_ID)
    exit_code, runner = _run_offline(db_factory, http)
    assert exit_code == 1
    assert any(step.name == "self_test" and step.status == "fail" for step in runner.table.steps)


def test_offline_no_db_skips_db_steps(db_factory):
    http = FakeHttp(db_factory, secret=CORRECT_SECRET, session_id=SESION_ID)
    runner = E2ERunner(http=http, db=None, config=_config())
    exit_code = runner.run()
    assert exit_code == 0
    by_name = {step.name: step.status for step in runner.table.steps}
    assert by_name["negative_no_entitlement"] == "skip"
    assert by_name["first_message"] == "skip"
    assert by_name["activate"] == "ok"  # HTTP-only assertions still run
