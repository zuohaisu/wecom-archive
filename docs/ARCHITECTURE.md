# Architecture — wecom-archive

> This is the current repository-backed architecture summary. It describes
> source and policy, not live production configuration.

## Product and license boundary

All first-party source—including cloud and self-hosted behavior—lives in this
repository under AGPL-3.0. `APP_EDITION` selects runtime policy; it is not a
source-code or license boundary. The self-hosted edition avoids SaaS purchase,
trial, subscription-expiry, and commercial quota dependencies while retaining
tenant isolation, authentication, authorization, audit, encryption, and
fail-closed behavior. The cloud edition retains its commercial lifecycle and
billing behavior.

The runtime defaults to `selfhost` when `APP_EDITION` is unset; invalid values
fail closed. The production value is an Ops fact and must not be inferred from
this source tree. See [ADR-0007](adr/0007-runtime-edition-policies.md) and
[Discussion #178](https://github.com/zuohaisu/wecom-archive/discussions/178)
for the approved product and edition decisions.

## Component boundaries

The intended dependency direction is:

```text
composition root (app/main.py) → routers → services/domain → database
```

The composition root wires routers and process policy. Business routes belong
in `backend/app/routers/`; domain workflows belong in services; persistence
belongs in `backend/app/db/`. Services must not import routers, and routers
must not import the composition root. The executable authority for these rules
is [`backend/tests/test_architecture_boundary.py`](../backend/tests/test_architecture_boundary.py).

## Tenant and service boundaries

A tenant-scoped authenticated session determines tenant identity; request data
must not select another tenant. Tenant lifecycle and edition policy are
separate: self-hosted `frozen` projections do not disable ordinary service,
while `suspended`, missing identity, unknown lifecycle state, invalid
credentials, and failed authorization remain fail-closed. Do not mutate a
persisted lifecycle state to simulate an edition policy.

Secrets and archive content are untrusted/sensitive data. Keep credentials
outside version control, use synthetic fixtures, and validate external values
at adapter boundaries. Preserve auditability and tenant scope for data reads,
writes, exports, and worker operations.

## Decision records

- [ADR-0007 — runtime edition and self-hosted policy](adr/0007-runtime-edition-policies.md) is the current edition decision.
- [ADR-0005 — cloud billing lifecycle and service gates](adr/0005-saas-billing-lifecycle-refunds-and-service-gates.md) remains the cloud commercial policy.
- [ADR-0003 — hosted-only strategy](adr/0003-product-strategy-hosted-only.md) is retained as a historical record and is superseded by the current AGPL/self-host decision.
- [CONTRIBUTING.md](../CONTRIBUTING.md) is the public entry point for contribution, validation, safety, and architecture guidance.
