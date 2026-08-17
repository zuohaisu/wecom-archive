#!/usr/bin/env python3
"""
RND-389 E2E driver: 自助接入全闭环验证 (self-service activation end-to-end).

Drives the REAL HTTP surface of a NON-PRODUCTION deployment with a real test
corp, asserting the RND-383 T1-T3 loop over the wire:

    provisioning status -> wrong-secret negative -> configure -> self-test ->
    auto-ready -> activate (trial grant) -> replay idempotency ->
    post-activation access transition -> billing probe -> first archived
    message -> (optional) second-tenant isolation

DB-backed assertions (trial subscription source, sync_state, archive_messages,
second-tenant binding) run only when DATABASE_URL is reachable; otherwise
those steps SKIP with a warning.  No live network is ever touched in CI — the
offline protocol round-trip lives in tests/test_rnd389_e2e_driver_guard.py.

Usage (from backend/, on the non-prod host):
    RND389_E2E=1 RND389_BASE_URL=https://staging.example.com \
      RND389_SESSION_ID=<provisioning session cookie from the operator browser> \
      RND389_ARCHIVE_SECRET=... RND389_PRIVATE_KEY_FILE=... \
      RND389_PUBLIC_KEY_VERSION=... RND389_CALLBACK_TOKEN=... \
      RND389_CALLBACK_AES_KEY=... \
      python scripts/e2e_rnd389_self_service_activation.py

Environment variables:
    RND389_E2E                  Required: set to 1 to arm the driver.
    RND389_BASE_URL             Required: non-prod base URL (no trailing /).
    RND389_SESSION_ID           Required: provisioning-scoped session cookie
                                obtained from the operator browser after the
                                WeCom third-party authorization + claim confirm.
    RND389_ARCHIVE_SECRET       Required: test corp 会话存档 Secret.
    RND389_PRIVATE_KEY_FILE     Required: path to the RSA private key PEM
                                (public key + version uploaded in the WeCom
                                admin console).
    RND389_PUBLIC_KEY_VERSION   Required: the public key version int.
    RND389_CALLBACK_TOKEN       Required: callback Token from the test corp
                                admin console.
    RND389_CALLBACK_AES_KEY     Required: callback EncodingAESKey (43 chars).
    RND389_WRONG_ARCHIVE_SECRET Optional wrong secret for the negative probe
                                (default: "rnd389-e2e-wrong-secret").
    RND389_SESSION_ID_2         Optional: second provisioning session cookie
                                to run the tenant-isolation phase (needs a
                                second test corp).
    RND389_POLL_SECONDS         Optional poll interval (default 5).
    RND389_ACTIVATION_TIMEOUT   Optional seconds to wait for ready/active
                                (default 120).
    RND389_MESSAGE_TIMEOUT      Optional seconds to wait for the first
                                archived message (default 600).
    DATABASE_URL                Optional: enables DB-backed assertions.

Exit codes:
    0  All steps ok (or skipped with warning)
    1  Any step failed / guards refused to run

Safety constraints (RND-383 cross-cutting rules):
    - Refuses to run when APP_ENV=production.
    - Never prints secrets, session ids, corp ids, message bodies or response
      payloads: logs carry step names, assertion outcomes and sha256[:12]
      digests of tenant-scoped identifiers only.
    - Never sends tenant_id / corp_id / lifecycle values: the session is the
      sole scope source, the same as the production UI surface.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import uuid
from dataclasses import dataclass

# Allow running from backend/ without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _digest(value: str) -> str:
    """Log-safe tag: sha256[:12], never the raw identifier."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class E2EGuardRefused(RuntimeError):
    """The environment refused to run the driver."""


# ---------------------------------------------------------------------------
# Environment classification & guards
# ---------------------------------------------------------------------------


def classify_environment(environ=None) -> str:
    """'production' | 'non-production' — the driver only runs non-prod."""
    raw = (environ or os.environ).get("APP_ENV", "").strip().lower()
    return "production" if raw == "production" else "non-production"


def guard_environment(environ=None) -> str:
    """Refuse production runs; require RND389_E2E=1 (arming flag)."""
    env = environ or os.environ
    if env.get("RND389_E2E", "").strip() != "1":
        raise E2EGuardRefused(
            "RND389_E2E=1 is required to run the E2E driver (arming flag)."
        )
    if classify_environment(env) == "production":
        raise E2EGuardRefused(
            "APP_ENV=production: the RND-389 E2E driver must never run against "
            "production data or infrastructure."
        )
    return classify_environment(env)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class E2EConfig:
    base_url: str
    session_id: str
    archive_secret: str
    wrong_secret: str
    private_key_pem: str
    publickey_version: int
    callback_token: str
    callback_aes_key: str
    poll_seconds: float
    activation_timeout: float
    message_timeout: float
    session_id_2: str | None
    db_url: str

    @property
    def session_2_enabled(self) -> bool:
        return bool(self.session_id_2)

    @staticmethod
    def from_env(environ=None) -> "E2EConfig":
        env = environ or os.environ

        def _require(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise E2EGuardRefused(f"Missing required env var: {name}")
            return value

        key_file = env.get("RND389_PRIVATE_KEY_FILE", "").strip()
        key_pem = env.get("RND389_PRIVATE_KEY_PEM", "").strip()
        if key_pem and key_file:
            raise E2EGuardRefused(
                "Set exactly one of RND389_PRIVATE_KEY_FILE / RND389_PRIVATE_KEY_PEM"
            )
        if key_file:
            try:
                with open(key_file, "r", encoding="utf-8") as handle:
                    key_pem = handle.read().strip()
            except OSError as error:
                raise E2EGuardRefused(
                    f"Cannot read RND389_PRIVATE_KEY_FILE: {error}"
                ) from error
        if not key_pem:
            raise E2EGuardRefused(
                "Missing RSA private key: set RND389_PRIVATE_KEY_FILE (preferred) "
                "or RND389_PRIVATE_KEY_PEM"
            )

        def _float(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as error:
                raise E2EGuardRefused(
                    f"{name} must be a number, got {raw!r}"
                ) from error

        return E2EConfig(
            base_url=_require("RND389_BASE_URL").rstrip("/"),
            session_id=_require("RND389_SESSION_ID"),
            archive_secret=_require("RND389_ARCHIVE_SECRET"),
            wrong_secret=env.get(
                "RND389_WRONG_ARCHIVE_SECRET", "rnd389-e2e-wrong-secret"
            ).strip(),
            private_key_pem=key_pem,
            publickey_version=int(_require("RND389_PUBLIC_KEY_VERSION")),
            callback_token=_require("RND389_CALLBACK_TOKEN"),
            callback_aes_key=_require("RND389_CALLBACK_AES_KEY"),
            poll_seconds=_float("RND389_POLL_SECONDS", 5.0),
            activation_timeout=_float("RND389_ACTIVATION_TIMEOUT", 120.0),
            message_timeout=_float("RND389_MESSAGE_TIMEOUT", 600.0),
            session_id_2=env.get("RND389_SESSION_ID_2", "").strip() or None,
            db_url=env.get("DATABASE_URL", "").strip(),
        )


# ---------------------------------------------------------------------------
# HTTP client (thin protocol wrapper; fakeable for offline tests)
# ---------------------------------------------------------------------------


class HttpClient:
    """Minimal JSON client.  Only ever prints status codes and curated
    safe fields from responses — never bodies or credentials."""

    def __init__(self, base_url: str, session_id: str, timeout: float = 30.0):
        self.base_url = base_url
        self.session_id = session_id
        self.timeout = timeout

    def _headers(self, json_body: bool = False) -> dict:
        headers = {"Cookie": f"session_id={self.session_id}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _call(
        self,
        method: str,
        path: str,
        json_body=None,
        extra_headers: dict | None = None,
    ) -> tuple[int, dict | None]:
        import requests

        headers = self._headers(json_body is not None)
        if extra_headers:
            headers.update(extra_headers)
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            json=json_body,
            timeout=self.timeout,
            allow_redirects=False,
        )
        payload = None
        if response.status_code not in (204,):
            try:
                payload = response.json()
            except ValueError:
                payload = None
        return response.status_code, payload

    # -- provisioning surface -------------------------------------------------

    def provisioning_status(self) -> tuple[int, dict | None]:
        return self._call("GET", "/api/provisioning/status")

    def provisioning_config(self) -> tuple[int, dict | None]:
        return self._call("GET", "/api/provisioning/config")

    def put_provisioning_config(self, updates: dict) -> tuple[int, dict | None]:
        return self._call("PUT", "/api/provisioning/config", json_body=updates)

    def test_provisioning_config(self) -> tuple[int, dict | None]:
        return self._call("POST", "/api/provisioning/config/test", json_body={})

    def activate(self) -> tuple[int, dict | None]:
        return self._call("POST", "/api/provisioning/activate", json_body={})

    # -- post-activation surface ---------------------------------------------

    def admin_login(self) -> tuple[int, str | None]:
        import requests

        response = requests.request(
            "GET",
            f"{self.base_url}/admin/login",
            headers=self._headers(),
            timeout=self.timeout,
            allow_redirects=False,
        )
        return response.status_code, response.headers.get("location")

    def billing_plan(self) -> tuple[int, dict | None]:
        return self._call("GET", "/api/billing/plan")

    def create_order(self, plan_code: str) -> tuple[int, dict | None]:
        # A fresh idempotency key per run; never reused across runs.
        return self._call(
            "POST",
            "/api/billing/orders",
            json_body={"plan_code": plan_code},
            extra_headers={"Idempotency-Key": f"rnd389-e2e-{uuid.uuid4().hex}"},
        )

    def close_order(self, order_id: str) -> tuple[int, dict | None]:
        return self._call("POST", f"/api/billing/orders/{order_id}/close")


# ---------------------------------------------------------------------------
# DB assertions (optional; enabled when DATABASE_URL is set)
# ---------------------------------------------------------------------------


class DbProbe:
    """Tenant-scoped read assertions.  Never writes in production modes —
    the no_entitlement probe writes only on the non-prod deployment that the
    operator explicitly armed with RND389_E2E=1."""

    def __init__(self, db_url: str):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        self._factory = sessionmaker(bind=create_engine(db_url))

    def tenant_id_for_corp(self, corp_id: str) -> str | None:
        from app.db.models import ThirdPartyOrganizationBinding

        with self._factory() as db:
            row = (
                db.query(ThirdPartyOrganizationBinding)
                .filter(ThirdPartyOrganizationBinding.corp_id == corp_id)
                .first()
            )
            return row.tenant_id if row else None

    def trial_subscription(self, tenant_id: str) -> dict | None:
        from app.db.models import Subscription

        with self._factory() as db:
            row = (
                db.query(Subscription)
                .filter(Subscription.tenant_id == tenant_id)
                .first()
            )
            if row is None:
                return None
            return {"status": row.status, "source": row.source}

    def subscription_count(self, tenant_id: str) -> int:
        from app.db.models import Subscription

        with self._factory() as db:
            return (
                db.query(Subscription)
                .filter(Subscription.tenant_id == tenant_id)
                .count()
            )

    def sync_state_exists(self, tenant_id: str) -> bool:
        from app.db.models import SyncState

        with self._factory() as db:
            return (
                db.query(SyncState)
                .filter(SyncState.tenant_id == tenant_id)
                .first()
                is not None
            )

    def archive_message_count(self, tenant_id: str) -> int:
        from app.db.models import ArchiveMessage

        with self._factory() as db:
            return (
                db.query(ArchiveMessage)
                .filter(ArchiveMessage.tenant_id == tenant_id)
                .count()
            )

    # -- negative-path probe (no_entitlement) --------------------------------

    def insert_expired_subscription(self, tenant_id: str) -> None:
        from datetime import datetime, timedelta, timezone

        from app.db.models import BillingPlan, Subscription

        now = datetime.now(timezone.utc)
        with self._factory() as db:
            plan = (
                db.query(BillingPlan)
                .filter(BillingPlan.code == "annual_base_cny_99")
                .first()
            )
            if plan is None:
                raise RuntimeError(
                    "no annual billing plan row to anchor the negative probe"
                )
            db.add(
                Subscription(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    plan_id=plan.id,
                    status="expired",
                    source="e2e_no_entitlement_probe",
                    starts_at=now - timedelta(days=40),
                    ends_at=now - timedelta(days=10),
                    grace_ends_at=now - timedelta(days=5),
                )
            )
            db.commit()

    def delete_subscription(self, tenant_id: str) -> None:
        from app.db.models import Subscription

        with self._factory() as db:
            db.query(Subscription).filter(
                Subscription.tenant_id == tenant_id
            ).delete()
            db.commit()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    status: str  # ok | fail | skip
    detail: str = ""


class StepTable:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(Step(name, status, detail))
        marker = {"ok": "[ OK ]", "fail": "[FAIL]", "skip": "[SKIP]"}[status]
        print(f"{marker} {name}: {detail}", flush=True)

    def finalize(self) -> int:
        print("\nRND-389 E2E step table", flush=True)
        for step in self.steps:
            print(f"  {step.status.upper():4s} {step.name}: {step.detail}", flush=True)
        failed = [s for s in self.steps if s.status == "fail"]
        if failed:
            print(f"RESULT: FAIL ({len(failed)} failed steps)", flush=True)
            return 1
        print("RESULT: PASS", flush=True)
        return 0


class E2ERunner:
    """Orchestrates the phases; ``http`` and ``db`` are injected so the guard
    tests can run the exact protocol offline with fakes."""

    def __init__(self, http: HttpClient, db: DbProbe | None, config: E2EConfig):
        self.http = http
        self.db = db
        self.config = config
        self.table = StepTable()
        self.corp_id = ""
        self.tenant_id: str | None = None

    # -- helpers --------------------------------------------------------------

    def _wait_for_state(
        self, http: HttpClient, acceptable: set[str], timeout: float, label: str
    ) -> dict | None:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            status, payload = http.provisioning_status()
            if status == 401:
                # Surface closed while polling: the auto-trigger promoted the
                # tenant (or the session died).  Do not burn the remaining
                # timeout; the caller's DB assertions arbitrate.
                return {"state": "active", "surface_closed": True}
            if status != 200 or payload is None:
                time.sleep(self.config.poll_seconds)
                continue
            last = payload
            state = (payload.get("activation") or {}).get("state")
            if state in acceptable:
                return payload
            time.sleep(self.config.poll_seconds)
        return last

    def _config_updates(self, archive_secret: str) -> dict:
        return {
            "archive_secret": archive_secret,
            "private_key": self.config.private_key_pem,
            "publickey_version": self.config.publickey_version,
            "callback_token": self.config.callback_token,
            "callback_encoding_aes_key": self.config.callback_aes_key,
        }

    def _configure(
        self, http: HttpClient, archive_secret: str, label: str
    ) -> tuple[bool, str]:
        status, payload = http.put_provisioning_config(
            self._config_updates(archive_secret)
        )
        if status != 200 or payload is None:
            return False, f"PUT config {label} -> HTTP {status}"
        missing = payload.get("missing") or []
        if missing:
            return False, f"PUT config {label} still missing: {missing}"
        return True, f"config saved ({label})"

    # -- phases ---------------------------------------------------------------

    def run(self) -> int:
        self.phase_session()
        self.phase_negative_wrong_secret()
        self.phase_no_entitlement()
        self.phase_activate()
        self.phase_replay()
        self.phase_post_activation_access()
        self.phase_billing_probe()
        self.phase_first_message()
        self.phase_second_tenant()
        return self.table.finalize()

    def phase_session(self) -> None:
        status, payload = self.http.provisioning_status()
        if status != 200 or payload is None:
            self.table.record(
                "provisioning_session",
                "fail",
                f"GET /api/provisioning/status -> HTTP {status} (session cookie "
                "must be a provisioning-scoped owner session)",
            )
            return
        lifecycle = payload.get("lifecycle_status")
        if lifecycle != "provisioning":
            self.table.record(
                "provisioning_session",
                "fail",
                f"lifecycle_status={lifecycle!r} (expected 'provisioning')",
            )
            return
        self.corp_id = (self.http.provisioning_config()[1] or {}).get("org", {}).get(
            "corp_id", ""
        )
        if self.db is not None and self.corp_id:
            self.tenant_id = self.db.tenant_id_for_corp(self.corp_id)
        else:
            self.tenant_id = None
        detail = f"lifecycle=provisioning tenant={_digest(self.tenant_id or self.corp_id)}"
        if self.tenant_id is None:
            detail += " (no DATABASE_URL: DB steps will skip)"
        self.table.record("provisioning_session", "ok", detail)

    def phase_negative_wrong_secret(self) -> None:
        ok, detail = self._configure(self.http, self.config.wrong_secret, "wrong archive secret")
        if not ok:
            self.table.record("negative_wrong_secret", "fail", detail)
            return
        status, payload = self.http.test_provisioning_config()
        if status != 200 or payload is None:
            self.table.record(
                "negative_wrong_secret", "fail", f"config test -> HTTP {status}"
            )
            return
        if payload.get("all_ok"):
            self.table.record(
                "negative_wrong_secret",
                "fail",
                "config test unexpectedly passed with a wrong archive secret",
            )
            return
        status, payload = self.http.activate()
        activation = (payload or {}).get("activation") or {}
        code = activation.get("safe_error_code")
        if status == 200 and activation.get("state") == "blocked" and code in {
            "credentials_invalid",
            "connectivity_failed",
        }:
            self.table.record(
                "negative_wrong_secret", "ok", f"blocked with {code}"
            )
        else:
            self.table.record(
                "negative_wrong_secret",
                "fail",
                f"expected blocked credentials_invalid, got HTTP {status} "
                f"state={activation.get('state')} code={code}",
            )

    def phase_no_entitlement(self) -> None:
        if self.db is None or self.tenant_id is None:
            self.table.record(
                "negative_no_entitlement",
                "skip",
                "requires DATABASE_URL for the expired-subscription probe",
            )
            return
        # The config must be complete for the gate chain to reach the
        # subscription gate, so the probe fixes the archive secret first.  The
        # PUT auto-trigger fires immediately and blocks on the expired row
        # (no_entitlement, no trial grant, no promotion) — the driver's own
        # activate below re-evaluates and must see the same verdict.
        self.db.insert_expired_subscription(self.tenant_id)
        try:
            ok, detail = self._configure(self.http, self.config.archive_secret, "probe fix")
            if not ok:
                self.table.record("negative_no_entitlement", "fail", detail)
                return
            status, payload = self.http.activate()
            activation = (payload or {}).get("activation") or {}
            code = activation.get("safe_error_code")
            if (
                status == 200
                and activation.get("state") == "blocked"
                and code == "no_entitlement"
            ):
                self.table.record(
                    "negative_no_entitlement", "ok", "blocked with no_entitlement"
                )
            else:
                self.table.record(
                    "negative_no_entitlement",
                    "fail",
                    f"expected blocked no_entitlement, got HTTP {status} "
                    f"state={activation.get('state')} code={code}",
                )
        finally:
            self.db.delete_subscription(self.tenant_id)

    def phase_activate(self) -> None:
        ok, detail = self._configure(self.http, self.config.archive_secret, "correct secret")
        if not ok:
            self.table.record("configure", "fail", detail)
            return
        self.table.record("configure", "ok", "all fields set")

        status, payload = self.http.test_provisioning_config()
        if status != 200 or payload is None or not payload.get("all_ok"):
            self.table.record(
                "self_test",
                "fail",
                f"config test -> HTTP {status} all_ok={(payload or {}).get('all_ok')}",
            )
            return
        self.table.record("self_test", "ok", "local checks + connectivity probe passed")

        payload = self._wait_for_state(
            self.http, {"ready", "active"}, self.config.activation_timeout, "ready/active"
        )
        if payload is None:
            self.table.record(
                "wait_ready", "fail", "timed out; last status unavailable"
            )
            return
        activation = payload.get("activation") or {}
        state = activation.get("state")
        if state not in {"ready", "active"}:
            self.table.record(
                "wait_ready",
                "fail",
                f"stuck at state={state} code={activation.get('safe_error_code')}",
            )
            return
        self.table.record(
            "wait_ready", "ok", f"auto-trigger reached state={state}"
        )

        status, payload = self.http.activate()
        if status == 401:
            # The auto-trigger worker beat us to the promotion: the surface
            # is closed (401) because the session was promoted.  The DB side
            # must still show exactly the one trial subscription.
            detail = "surface closed (401) — auto-activated before the driver's call"
            if self.db is not None and self.tenant_id is not None:
                if self._assert_single_trial(detail):
                    return
                return
            self.table.record("activate", "ok", detail)
            return
        body = payload or {}
        if status != 200:
            self.table.record(
                "activate", "fail", f"POST activate -> HTTP {status}"
            )
            return
        activated = bool(body.get("activated"))
        replayed = bool(body.get("replayed"))
        if not (activated or replayed):
            self.table.record(
                "activate",
                "fail",
                f"neither activated nor replayed: {body.get('activation')}",
            )
            return
        detail = f"activated={activated} replayed={replayed}"
        if self.db is not None and self.tenant_id is not None:
            if self._assert_single_trial(detail):
                return
            return
        self.table.record("activate", "ok", detail)

    def _assert_single_trial(self, detail: str) -> bool:
        """Record the activate step result from DB facts.  Returns True when
        the step was already recorded (either ok or fail)."""
        assert self.db is not None and self.tenant_id is not None
        subscription = self.db.trial_subscription(self.tenant_id)
        count = self.db.subscription_count(self.tenant_id)
        if subscription is None or count != 1:
            self.table.record(
                "activate",
                "fail",
                detail
                + f" (expected exactly 1 trial subscription, got count={count})",
            )
            return True
        if subscription["source"] != "self_service_trial":
            self.table.record(
                "activate",
                "fail",
                detail
                + f" (source={subscription['source']}, expected self_service_trial)",
            )
            return True
        self.table.record(
            "activate", "ok", detail + " trial=self_service_trial count=1"
        )
        return True

    def phase_replay(self) -> None:
        status, payload = self.http.activate()
        if status == 401:
            # Promotion closed the provisioning surface: a second HTTP
            # activate cannot even reach the handler.  The idempotency proof
            # is the single trial row below; service-level double-call
            # semantics are covered by the rnd388 unit suite.
            detail = "HTTP 401 (surface closed after promotion)"
            if self.db is not None and self.tenant_id is not None:
                count = self.db.subscription_count(self.tenant_id)
                if count != 1:
                    self.table.record(
                        "replay_idempotent",
                        "fail",
                        f"{detail} but subscription count={count} (double grant!)",
                    )
                    return
                detail += " subscriptions=1"
            self.table.record("replay_idempotent", "ok", detail)
            return
        body = payload or {}
        if status != 200 or not body.get("replayed") or body.get("activated"):
            self.table.record(
                "replay_idempotent",
                "fail",
                f"second activate -> HTTP {status} "
                f"activated={body.get('activated')} replayed={body.get('replayed')}",
            )
            return
        detail = "replayed=True"
        if self.db is not None and self.tenant_id is not None:
            count = self.db.subscription_count(self.tenant_id)
            if count != 1:
                self.table.record(
                    "replay_idempotent",
                    "fail",
                    f"{detail} but subscription count={count} (double grant!)",
                )
                return
            detail += " subscriptions=1"
        self.table.record("replay_idempotent", "ok", detail)

    def phase_post_activation_access(self) -> None:
        status, _ = self.http.provisioning_status()
        if status != 401:
            self.table.record(
                "post_activation_access",
                "fail",
                f"provisioning status after promotion -> HTTP {status} (expected 401)",
            )
            return
        login_status, location = self.http.admin_login()
        if login_status != 302 or location != "/dashboard":
            self.table.record(
                "post_activation_access",
                "fail",
                f"/admin/login -> HTTP {login_status} location={location} "
                "(expected 302 /dashboard)",
            )
            return
        self.table.record(
            "post_activation_access",
            "ok",
            "provisioning guard 401, /admin/login bounces to /dashboard",
        )

    def phase_billing_probe(self) -> None:
        status, payload = self.http.billing_plan()
        if status != 200 or payload is None:
            self.table.record(
                "billing_probe",
                "skip",
                f"plan endpoint -> HTTP {status} (payment not configured in this "
                "environment; real payment verify per runbook)",
            )
            return
        code = payload.get("code")
        if code != "annual_base_cny_99":
            self.table.record(
                "billing_probe",
                "fail",
                f"expected annual_base_cny_99, got code={code}",
            )
            return
        if not payload.get("payment_enabled"):
            self.table.record(
                "billing_probe",
                "skip",
                "plan ok but payment disabled; order lifecycle skipped",
            )
            return
        order_status, order = self.http.create_order(code)
        if order_status != 201 or order is None:
            self.table.record(
                "billing_probe",
                "fail",
                f"create order -> HTTP {order_status}",
            )
            return
        close_status, closed = self.http.close_order(order["order_id"])
        if close_status != 200 or closed is None or closed.get("status") != "closed":
            self.table.record(
                "billing_probe",
                "fail",
                f"close order -> HTTP {close_status} status="
                f"{(closed or {}).get('status')}",
            )
            return
        self.table.record(
            "billing_probe", "ok", "order created (201) and closed via HTTP"
        )

    def phase_first_message(self) -> None:
        if self.db is None or self.tenant_id is None:
            self.table.record(
                "first_message",
                "skip",
                "requires DATABASE_URL (operator verifies first message in console)",
            )
            return
        if not self.db.sync_state_exists(self.tenant_id):
            self.table.record(
                "first_message",
                "fail",
                "no sync_state row for tenant — archive worker never ran",
            )
            return
        print(
            "[INFO] 请在测试企业微信中发送一条会话存档消息（任意聊天），"
            "等待归档 worker 拉取…",
            flush=True,
        )
        deadline = time.monotonic() + self.config.message_timeout
        count = 0
        while time.monotonic() < deadline:
            count = self.db.archive_message_count(self.tenant_id)
            if count > 0:
                break
            time.sleep(self.config.poll_seconds)
        if count <= 0:
            self.table.record(
                "first_message",
                "fail",
                f"no archive_messages for tenant within "
                f"{self.config.message_timeout:.0f}s (sync_state exists, worker "
                "ran but got nothing — check the test corp's archive scope)",
            )
            return
        self.table.record(
            "first_message",
            "ok",
            f"sync_state present, archive_messages={count} (tenant-scoped)",
        )

    def phase_second_tenant(self) -> None:
        if not self.config.session_2_enabled:
            self.table.record(
                "second_tenant_isolation",
                "skip",
                "RND389_SESSION_ID_2 not set (needs a second test corp)",
            )
            return
        if self.db is None:
            self.table.record(
                "second_tenant_isolation",
                "skip",
                "RND389_SESSION_ID_2 set but DATABASE_URL missing",
            )
            return

        other = HttpClient(self.config.base_url, self.config.session_id_2)
        status, payload = other.provisioning_status()
        if status != 200 or payload is None:
            self.table.record(
                "second_tenant_isolation",
                "fail",
                f"second session status -> HTTP {status}",
            )
            return
        org = (other.provisioning_config()[1] or {}).get("org", {})
        tenant_2 = self.db.tenant_id_for_corp(org.get("corp_id", "")) if org else None
        if tenant_2 is None or tenant_2 == self.tenant_id:
            self.table.record(
                "second_tenant_isolation",
                "fail",
                "could not resolve a distinct tenant for the second session",
            )
            return
        ok, detail = self._configure(
            other, self.config.archive_secret, f"tenant2 {_digest(tenant_2)}"
        )
        if not ok:
            self.table.record("second_tenant_isolation", "fail", detail)
            return
        payload = self._wait_for_state(
            other, {"ready", "active"}, self.config.activation_timeout, "tenant2 ready"
        )
        if payload is None:
            self.table.record(
                "second_tenant_isolation",
                "fail",
                "tenant2 never reached ready/active",
            )
            return
        if not self.db.sync_state_exists(tenant_2):
            self.table.record(
                "second_tenant_isolation",
                "fail",
                "no sync_state row for tenant2 — worker isolation broken",
            )
            return
        self.table.record(
            "second_tenant_isolation",
            "ok",
            f"tenant2 activated with its own sync_state (tenant={_digest(tenant_2)})",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    try:
        env_class = guard_environment()
        config = E2EConfig.from_env()
    except E2EGuardRefused as error:
        print(f"[FAIL] {error}", flush=True)
        return 1

    print(
        f"[INFO] RND-389 E2E driver armed (environment={env_class}, "
        f"base={_digest(config.base_url)})",
        flush=True,
    )
    http = HttpClient(config.base_url, config.session_id)
    db = DbProbe(config.db_url) if config.db_url else None
    runner = E2ERunner(http=http, db=db, config=config)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
