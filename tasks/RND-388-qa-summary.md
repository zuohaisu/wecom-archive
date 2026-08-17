# RND-388 QA Summary — 自动连通性检查、Ready 状态与无人工激活 (T3)

## Files changed

- `backend/alembic/versions/0056_rnd388_tenant_activation_checks.py` (new) — `tenant_activation_checks` table: id PK, tenant_id FK unique, `state` CHECK in (not_started|blocked|ready), `gate_results` JSONB, `safe_error_code` nullable, `revision`, timestamps. `lifecycle_status` CHECK stays frozen at provisioning|active|frozen|suspended (RND-398); the activation progress lives in a separate persisted state machine.
- `backend/app/db/models.py` — `TenantActivationCheck` model mirroring 0056.
- `backend/app/services/tenant_activation.py` (new) — the state machine + single promotion routine:
  - `GATE_IDS = ("binding", "config", "connectivity", "subscription", "runtime")` — evaluated in order, gates after the first failure stay absent from `gate_results` (UI renders skipped).
  - Safe error codes: missing_binding, config_incomplete, config_not_decryptable, credentials_invalid, connectivity_failed, no_entitlement, runtime_unavailable, activation_conflict.
  - `_gate_binding` — binding row exists, permanent_code decrypts; `_gate_config` — all T1 fields set + every stored secret decrypts + private key parses as RSA PEM; `_gate_connectivity` — real `get_wecom_token` call with per-tenant `activation-connectivity:{tenant_id}` cache key, mapped via `connectivity_failure_reason` to credentials_invalid/connectivity_failed; `_gate_subscription` — no Subscription row → **defers** (the post-gate RND-394 trial grant decides), present-but-not-entitled → no_entitlement; `_gate_runtime` — `WECOM_SDK_LIB_PATH` env set + callback creds resolvable per-tenant or env.
  - Trial wiring: on a fully-passing evaluation with no Subscription row at all, `grant_self_service_trial` (15-day, TRIAL_SOURCE="self_service_trial"); declined (TrialNotEligible/TrialAlreadyUsed) → re-check entitlement; a used/expired subscription never gets a second chance.
  - `evaluate_activation` — row-locked (`with_for_update`) evaluation; replay short-circuit (active tenant → snapshot only, zero writes, zero network); upserts the checks row (revision+1); caller owns the commit.
  - `activation_status` — read-only persisted snapshot; never locks, re-evaluates or touches the network (the polling page must not probe connectivity every 5s).
  - `promote_tenant` — the single promotion routine: lifecycle provisioning→active, is_active=True, lifecycle_revision+1, onboarding_completed_at, frozen_at=None, bulk-promotes provisioning sessions→admin, audits `PLATFORM_TENANT_ACTIVATED` with actor + gate_results detail.
  - `activate_tenant` — the only promoter; `require_gates=False` is the platform console override; holds the row lock for the whole attempt so a concurrent second caller replays with no double trial/audit/promotion; commits before returning; never accepts a client-supplied tenant id or lifecycle value.
  - `spawn_activation_worker(db_bind, tenant_id, actor)` — detached best-effort trigger thread (fresh session owns its own transaction; the row lock serializes).
- `backend/app/routers/provisioning.py` —
  - `POST /api/provisioning/activate` — manual retry entry for ready/blocked tenants; idempotent; on first activation dispatches `dispatch_archive_worker(trigger_source="activation", tenant_id=…)` strictly after the promotion commit; response carries `activated/replayed/activation/worker_dispatch` (all safe fields).
  - `GET /api/provisioning/status` — now returns lifecycle + `activation` snapshot + `allowed_actions` (gains "activate" only when ready).
  - `PUT /api/provisioning/config` and `POST /api/provisioning/config/test` now fire `spawn_activation_worker(actor="self_service")` best-effort after success (auto-activation triggers, event-driven not button-only).
- `backend/app/routers/billing.py` — WeChat-pay notify: after `apply_trusted_payment` commits, best-effort `spawn_activation_worker(db.get_bind(), order.tenant_id, actor="payment_notify")` in try/except with `logger.exception` → 204 (covers pay-first-then-configure order; the row lock serializes vs a concurrent self-service evaluation); removed the dangling duplicate `except` blocks that sat after the 204 return (SyntaxError); added the missing module `logger`.
- `backend/app/routers/platform.py` — activation branch of `update_tenant_status` now delegates to `activate_tenant(db, tenant_id, actor="platform", require_gates=False)` — the platform console stays authoritative, gates not required there; identical session promotion + audit (session promotion/audit logic removed in favor of the shared service).
- `backend/app/web/templates/provisioning.html` — waiting-room card → dynamic onboarding status page: design-system shell, breadcrumb 开通步骤/开通进度, page header, `#activation-root` (aria-live/aria-busy), activation-specific CSS (hero card by state, gate chips ok/failed/skipped), lang-menu/logout inline script.
- `backend/app/web/static/provisioning.js` — full rewrite: shared helpers (t/applyI18n/makeElement); the entire T1 wizard wrapped in a DOM-guard (`if (!form || …) return;`) so the same bundle serves both pages without crashing; new `initActivation()` module: 5s polling (stops at active), gate-chip render, safe-error i18n mapping, per-state CTAs (no_entitlement→/admin/billing purchase, config codes→/admin/provisioning/settings), activate handler (401→/admin/login, activated→/dashboard, else render+resume poll), pagehide cleanup.
- `backend/app/assets/i18n.js` — identical `activation.*` key sets in all 3 locales (zh-CN/zh-TW/en): pageTitle/pageDescription/loading, 4 state heroes, 5 gate labels + ok/failed/skipped, 8 safe-error texts, 4 CTAs, activating/activated/failed, technicalDetails/revision, goDashboard, authFailed.
- `backend/tests/fakes.py` — `tenant_activation_checks` in `_SCHEMA_SQL` (sqlite mirror).
- `backend/tests/test_http_contract.py` — route count 151 → **152** (the one new route: POST /api/provisioning/activate), path list + snapshot row updated.
- `backend/tests/test_rnd348_self_service_provisioning.py` — status-body assertion extended with the `activation` key (deliberate in-scope contract evolution, same delivery branch).
- `backend/tests/test_rnd388_self_service_activation.py` (new, 16 tests) — fixture with all 12 required tables; seeded provisioning tenant + binding + owner + provisioning-scoped session + annual-plan entitlement; helpers for complete config (real RSA PEM via `set_credentials` + `set_callback_credentials`), runtime env, token ok/fail patches, worker dispatch/spawn capture.

## Acceptance criteria

- [x] 无人工激活 — config save, config test and payment notify all fire detached best-effort evaluations; the activate button is a manual retry entry, not the only path.
- [x] Ready 状态独立持久化 — `tenant_activation_checks` (not_started|blocked|ready) is separate from the frozen `lifecycle_status` CHECK; status endpoint is read-only (no re-evaluation, no network on poll).
- [x] 门禁顺序与跳过渲染 — gates run in order; gates after the first failure are absent from gate_results and the UI renders them as skipped.
- [x] 试用与订阅门禁 — no Subscription row → gate defers → post-gate 15-day trial grant on full pass; present-but-not-entitled → no_entitlement without a second grant; blocked tenants stay subscription-less.
- [x] 幂等 — active-tenant replay is a short-circuit (no writes/network); concurrent callers serialized by the tenant row lock; exactly one trial, one audit, one worker dispatch (asserted via monkeypatched dispatcher).
- [x] 平台权威 — console activation delegates with `require_gates=False`; deactivation unchanged; `test_rnd310_tenant_activation.py` green.
- [x] 客户端不可信 — tenant id / corp_id / lifecycle never accepted on the provisioning surface; session is the sole scope source; after promotion the session is admin-scoped and the provisioning guard answers 401 → /admin/login 302 → /dashboard (UI redirect path verified).
- [x] 日志与响应无凭据 — logs carry sha256[:12] tenant digests only; GET returns masks/status, never plaintext; no secrets in the diff.

## Commands run

- New suite: `pytest backend/tests/test_rnd388_self_service_activation.py -q` — **16 passed in 6.74s** (two test-side fixes during the gate: the post-promotion login assertion needed the session cookie passed per-request — the client jar is empty in this fixture since the claim flow never went through HTTP; and the runtime-blocked test correctly expects `Subscription.count() == 0`, since the trial grants only on a fully-passing evaluation).
- Regression suites: rnd348 + rnd386 + rnd387 (callback+worker) + rnd394 + rnd310 + http contract + rnd280 RBAC + field encryption — **129 passed in 10.35s**.
- Route-count discipline: 151 at T2 commit → **152** at current tree (one new route, contract test pins it).
- `make verify` (ruff lint-diff → typecheck → build → full pytest) — **OK: 3122 passed, 93 skipped, 0 failed** (up from 3106 at T2; +16 new RND-388 tests). One lint fix during the gate: unused `ActivationSnapshot` import in the provisioning router.

## Manual verification

- The two design decisions re-verified in code: (1) trial grant is inside the all-gates-pass path only — a tenant blocked at runtime stays subscription-less and the later successful evaluation grants exactly once; (2) the post-promotion access transition is 401 (provisioning guard filters `session_scope == "provisioning"`, the promoted session answers 401, NOT 403), and /admin/login then bounces the still-valid admin session to /dashboard — the JS 403 branch is defensive dead code.
- `activate_tenant` replay path manually exercised via isolated script: active tenant → replayed=True, snapshot state "active", zero writes.

## Risks or gaps

- Real WeCom wire verification (live connectivity probe, payment notify with real WeChat) remains in the non-prod E2E (T4, gated on RND-351/392/352).
- The notify-path trigger depends on `PaymentOrder.tenant_id` lookup after `apply_trusted_payment`; if the lookup fails the notify still returns 204 and logs, and a later config-save/retry re-evaluates.
- Poll interval (5s) and per-state CTA targets are unit-level verified via JS code review; no browser automation in CI.

## No secrets introduced: confirmed
## Only intentional files changed: confirmed
