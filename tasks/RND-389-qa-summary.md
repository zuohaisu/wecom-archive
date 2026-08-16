# RND-389 QA Summary — 非生产 E2E 验证驱动与守卫 (T4)

> The run itself is gated on RND-351 / RND-392 / RND-352 (non-prod app, env, test corp).
> This commit ships the driver, the offline guard suite, the runbook and the QA-report
> template; the live-run report will be filled from `RND-389-qa-report-template.md` when
> the run happens. Production entry stays closed — the driver refuses `APP_ENV=production`.

## Files changed

- `backend/scripts/e2e_rnd389_self_service_activation.py` (new, ~760 lines) — the E2E driver:
  - Guards: `classify_environment`/`guard_environment` — refuses to run unless
    `RND389_E2E=1` is set and `APP_ENV` is not `production`; production refusal is the
    backstop that keeps the production entry closed.
  - `E2EConfig.from_env` — all secrets from `RND389_*` env (archive secret, private key
    file XOR PEM, public-key version, callback token + AES key, second-tenant session,
    poll/timeouts, optional `DATABASE_URL`); `_require`/`_float` helpers fail loudly.
  - `HttpClient` — protocol wrapper over the same JSON surface the UI drives
    (status/config/put-config/config-test/activate/admin-login/billing plan/orders);
    per-request `Cookie: session_id=…`, `allow_redirects=False`, `Idempotency-Key`
    header on order creation. Never logs request bodies.
  - `DbProbe` — optional DB-side assertions (tenant resolution via corp_id, trial row
    `source=self_service_trial` count, sync-state + message-count probes, expired-sub
    insert/delete for the no-entitlement probe).
  - `E2ERunner` phases (step table, `[ OK ]/[FAIL]/[SKIP]`, exit 0/1):
    provisioning_session → negative_wrong_secret → negative_no_entitlement (DB-probe,
    self-cleaning) → configure → self_test → wait_ready → activate → replay_idempotent →
    post_activation_access → billing_probe → first_message (operator sends one 会话存档
    message; driver polls for a tenant-scoped row) → second_tenant_isolation (distinct
    client + tenant, skips without a second corp session).
  - Race handling: the PUT/test auto-trigger can promote the tenant before the driver's
    own activate call — a 401 from the closed provisioning surface is treated as
    "auto-activated", with the DB asserting exactly one trial row; `_wait_for_state`
    short-circuits on 401 instead of burning the timeout.
- `backend/tests/test_rnd389_e2e_driver_guard.py` (new, 11 tests) — no live network:
  - Guards: default classification, arming-flag requirement, `APP_ENV=production`
    refusal even when armed, staging+armed passes.
  - `E2EConfig.from_env`: missing-secret refusal, private-key-from-file, both-key-sources
    rejection.
  - Offline protocol round-trip via `FakeHttp` — a state-machine twin mirroring the
    server's behavior in the same sqlite engine the `DbProbe` reads (so HTTP assertions
    and DB assertions agree): happy path (exit 0, step ordering, exactly one
    `self_service_trial` row), no-secret-leak rendering (raw secrets/session/corp/tenant
    absent from every step detail), failed-step surfacing (exit 1), and no-DB mode
    (DB steps skip, HTTP-only steps still run).
- `tasks/RND-389-e2e-runbook.md` (new) — operator steps: preconditions (deploy T1–T3,
  `alembic upgrade head`, test-corp admin-console config incl. 可信 IP + callback URL
  verification, browser session-cookie handoff), env table, run command, phase guide,
  teardown (freeze tenants, revoke corp authorization, unset secrets), rollback notes.
- `tasks/RND-389-qa-report-template.md` (new) — QA-report skeleton with acceptance
  criteria; filled as the summary when the run happens and the verdict lands in Linear.

## Acceptance criteria (this commit)

- [x] Driver ships with production refusal + `RND389_E2E=1` arming — production entry stays closed.
- [x] Offline guard suite proves the protocol orchestration (including both negative
  paths and replay idempotency) deterministically in CI — no live network ever touched.
- [x] Driver output carries tenant `sha256[:12]` digests only; raw secrets/session/corp/tenant
  never appear (asserted offline).
- [ ] Live non-prod run — gated on RND-351/392/352; report template prepared, verdict
  lands in Linear when the run happens.

## Commands run

- `pytest tests/test_rnd389_e2e_driver_guard.py -q` — **11 passed in 0.72s**.
- Regression gate (provisioning surface + contract): rnd348 + rnd388 + rnd386 + rnd387×2
  + rnd394 + http contract — **130 passed in 13.52s**.
- `make verify` (ruff lint-diff → typecheck → build → full pytest) — **OK: 3133 passed,
  93 skipped, 0 failed** (up from 3122 at T3; +11 new RND-389 guard tests). Two gate fixes:
  the offline fake now mirrors the real server's post-promotion surface closure (401 on
  provisioning routes once the session is admin-scoped, proven during T3), and the driver
  treats that 401 as the replay-idempotency proof; one unused-import lint fix.

## Risks or gaps

- Real wire coverage (live connectivity probe, WeChat pay notify, real message ingest)
  only lands with the gated live run; the offline suite pins the protocol but not the wire.
- The first-message phase depends on the operator sending a message and on the T2 worker
  chain picking it up within `RND389_MESSAGE_TIMEOUT`; flaky infrastructure would show up
  as a step FAIL, not a false PASS.
- The claim flow stays browser-assisted (session-cookie handoff) — no browser automation
  in this ticket.

## No secrets introduced: confirmed
## Only intentional files changed: confirmed
