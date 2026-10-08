# ADR-0007: Runtime Edition and Self-Hosted Policy Boundaries

**Status**: Accepted for GH-168 implementation; external activation remains gated
**Date**: 2026-10-07
**Author**: Crowntime WeCom Archive dev (Haisu-approved product direction)
**Related**: GH-168 (#168), [Discussion #178](https://github.com/zuohaisu/wecom-archive/discussions/178)

---

## 1. Context

The latest product decision is a single repository: first-party archive and cloud
operations code are all AGPL-3.0 and are not hidden or excluded by edition. The
runtime still needs an explicit self-hosted/cloud distinction because self-hosted
installations must not depend on SaaS billing, trial activation, or cloud
organization provisioning, while the hosted service must retain its existing
commercial behavior.

At baseline `53ec37f`, the application registers all routers unconditionally,
subscription lifecycle gates control tenant access and media quota, and there is
no `APP_EDITION` consumer. The read-only ops investigation
(`tasks/GH-168-ops-investigation.md`, available in the main checkout) records that
production web and its scheduled application workers read the clone's
`backend/.env`; the non-production web reads a separate env file. The report did
not SSH to either host, so the live value of `APP_EDITION` is unknown. This ADR
records code behavior and a merge gate, not a claim about current production
state.

ADR-0003 remains as a historical decision record; its status header now marks
the hosted-only policy superseded by Discussion #178 and this ADR. ADR-0005
continues to specify the commercial lifecycle for `cloud`.

## 2. Decision

### 2.1 Edition is runtime configuration, not a source or license boundary

- `APP_EDITION` is read from the process environment, not the database-backed
  Settings registry. Its value is shared by web and worker processes through
  their existing environment injection.
- An unset variable resolves to `selfhost`; accepted values are exactly
  `selfhost` and `cloud`. Empty or unknown values fail closed at application
  startup and before edition-dependent worker operations; they must never
  silently select a mode.
- The composition root may accept an explicit edition in tests, but production
  resolves it from the environment. The selected mode is attached to the app
  instance and is not inferred from a tenant, subscription, request, or client.
- All first-party source remains in this repository under AGPL-3.0. Do not add a
  proprietary cloud package, source allowlist exclusion, or code-obscuring gate.

### 2.2 Mode-specific runtime behavior

- `cloud` preserves the current route registration, onboarding, provider/payment
  flows, subscription lifecycle, storage entitlements, platform operations, and
  scheduled billing/payment behavior. Changes must be regression-tested in this
  mode, not skipped when the test runner's default is selfhost.
- `selfhost` keeps archive review, authentication, tenant-scoped configuration,
  S2 conversation-archive setup, media, exports, audit, and community workers.
  Cloud billing/payment and platform-operation endpoints, hosted public and
  platform AI-support surfaces, WeCom third-party organization auto-provisioning,
  and cloud-only billing/recovery workers are unavailable in this mode. The
  selfhost setup path uses the local first-run administrator and tenant
  configuration; it does not require purchase, a trial, or third-party
  automatic organization creation. Monthly commercial export-count quotas do
  not apply, while existing owner approval, tenant scope, auditing, and
  technical export safety limits remain in force.
- Shared code and static assets remain part of the same application. A static
  file being downloadable is not an authorization boundary; every operation
  that can read or mutate tenant data remains protected by the existing session,
  role, tenant-scope, and service checks.
- No new module dependency direction is introduced. Edition policy lives in
  the existing process-settings module; the composition root wires routers, and
  routers/services consume policy through existing downward dependencies.
  `backend/tests/test_architecture_boundary.py` remains authoritative. Do not
  weaken the architecture guard.

### 2.3 Tenant lifecycle and access

- In `cloud`, ADR-0005's subscription-driven `active`/`frozen` behavior remains
  authoritative, and an explicit `suspended` state still blocks all tenant
  capabilities.
- In `selfhost`, subscription expiry is not an access gate. A persisted
  `frozen` projection may be treated as commercially frozen only in cloud; the
  selfhost policy permits ordinary tenant capabilities without mutating that
  persisted lifecycle state. This is not an automatic state transition or
  subscription grant.
- `suspended`, missing identity, unknown lifecycle state, missing tenant scope,
  invalid credentials, and audit/configuration failures remain fail-closed in
  both editions. No mode may auto-clear a suspension or trust browser-provided
  identity, lifecycle, tenant, or entitlement values.

### 2.4 Self-hosted storage capacity

- Cloud continues to use the authoritative subscription plan quota.
- Selfhost does not inherit a cloud plan or commercial quota. Its optional
  administrator-configured `SELFHOST_STORAGE_LIMIT_BYTES` is a technical
  per-tenant write limit; unset or `0` means no application-level quota.
- A positive configured limit is enforced using server-measured usage while
  holding the existing tenant serialization lock. Invalid configuration or
  unavailable usage fails closed for new media writes. A missing subscription
  is not treated as a selfhost quota failure.
- The limit does not remove storage-backend error handling: failed/partial writes
  must not be recorded as downloaded, and the existing atomic publication and
  cleanup behavior remains in force. This is not a cloud plan entitlement.

### 2.5 Workers and deployment boundary

- Core archive, media, and export jobs continue to use the shared process
  environment and remain available in selfhost. Cloud billing lifecycle,
  payment recovery/reconciliation, and billing-notification entry points must
  refuse to run in selfhost, even if invoked manually.
- Do not edit production or non-production env files, systemd, CD, or live
  infrastructure in GH-168. No SSH or production-state verification is part of
  this implementation.
- Before the final v1.0.0 PR is merged, the required gate is: implementation
  branch complete → ops reviews edition consumption by web/workers → separately
  approved preseed of `APP_EDITION=cloud` in the production env → human merge →
  existing CD applies the code → cloud routes/workers are verified. If that
  preseed is not approved and verified before merge, stop the merge rather than
  allow a production restart to default to selfhost.
- Removing `APP_EDITION=cloud` and restarting selects selfhost; it is not a
  cloud rollback. Configuration repair retains `cloud`. A code-version rollback
  must first restore the old code through the approved deployment rollback path.

## 3. Consequences

- The application has one auditable runtime policy and one source tree; edition
  differences are behavioral and do not claim to protect cloud source.
- Selfhost avoids SaaS purchase/trial/expiry requirements while preserving manual
  suspension, tenant isolation, secret encryption, audit, and conservative
  write-failure behavior.
- Existing cloud tests must run against `cloud`; dedicated contract and behavior
  tests must also construct `selfhost`. No broad skip or expected-route
  weakening is acceptable.
- Production's current APP_EDITION value cannot be established from the local
  investigation. The pre-merge ops check is therefore a required external gate.

## 4. Alternatives Considered

- **Keep a proprietary `app/cloud` or export exclusion**: rejected because source
  confidentiality is not a product requirement and the repository is single,
  fully AGPL-3.0.
- **Make selfhost work by granting a fake permanent trial, editing subscription
  rows, or auto-resuming suspended tenants**: rejected because it falsifies
  commercial/audit state and weakens a security control.
- **Use a Settings DB value for APP_EDITION**: rejected because the mode must be
  consistently available before DB-dependent routes and to one-shot workers;
  the existing process environment is the common injection point.
- **Change systemd/CD or edit production env in the implementation**: rejected;
  those are ops actions requiring separate approval, and the existing env-file
  injection is sufficient.

## 5. References

- [Discussion #178 — current open-source decisions](https://github.com/zuohaisu/wecom-archive/discussions/178)
- [ADR-0003 — historical hosted-only strategy](0003-product-strategy-hosted-only.md)
- [ADR-0005 — cloud billing lifecycle and service gates](0005-saas-billing-lifecycle-refunds-and-service-gates.md)
- [Architecture boundary guard](../../backend/tests/test_architecture_boundary.py)
- [GH-168 ops investigation](../../tasks/GH-168-ops-investigation.md) (gitignored operational evidence; read from the existing main checkout)

---

_Last updated: 2026-10-07_
