# `Tenant.is_active` Retirement Record

## Decision and current state

GH-94 authorizes complete retirement of the persisted `Tenant.is_active` field
in one implementation change set. This branch removes the ORM field and all
runtime reads/writes, retains only lifecycle-derived compatibility outputs and
a safe legacy PATCH adapter, and adds the forward/drop plus downgrade/rebuild
migration. The migration has **not** been run in production; no deployment,
repair, or production write is part of this change.

`Tenant.lifecycle_status` is the sole tenant service-state authority. The
central service-access policy continues to deny `provisioning`, `frozen`, and
`suspended` for protected service capabilities; worker credential resolution
requires `active` lifecycle plus an active `TenantWecomConfig`.

## Production read-only evidence

The Ops evidence recorded in GitHub #94 (2026-10-07) reports:

- ECS: `ali-xy-qw`; deployed application commit: `53ec37f` (identified as
  `origin/main` at inspection time).
- PostgreSQL 16.15; Alembic revision `0073`.
- One tenant total; `(active, true)`: 1; all other
  `(lifecycle_status, is_active)` pairs: 0; no NULL or unknown lifecycle state.
- Scheduled archive work enters through `scripts/run_archive_worker_once.py`;
  no production systemd job was found directly invoking sync/decrypt child
  scripts.

There is no observed production mismatch to repair. Synthetic mismatches below
are migration safety-test inputs only, not production data. Before a production
cutover, Ops must re-confirm the live revision and invariant; this implementation
does not authorize that cutover.

## Reference classification

### Migrated runtime reads and writes

| Area | GH-94 result |
| --- | --- |
| ORM (`backend/app/db/models.py`) | `Tenant.is_active` removed. `Tenant.lifecycle_status` remains constrained to `provisioning`, `active`, `frozen`, or `suspended`. |
| Worker discovery (`tenant_credentials.active_tenant_ids` / `active_tenant_configs`) | All-tenant selection uses `lifecycle_status == 'active'`; config selection additionally requires `TenantWecomConfig.is_active`. |
| Explicit worker target (`resolve_tenant_archive_credentials`) | Requires an existing tenant with `lifecycle_status == 'active'` and an active config. Missing/non-active tenant or inactive/missing config fails closed. Thus no boolean mismatch can make all-tenant and explicit-target eligibility disagree. |
| Trial eligibility | Uses lifecycle states (`provisioning` or `active`) only. |
| Custom-host resolution and managed branding lists | Require `lifecycle_status == 'active'`; entitlement, verification, and certificate checks remain in force. |
| Password-reset discovery | Uses the active lifecycle state. Password login remains intentionally distinct: frozen owners retain billing recovery, while suspended tenants are denied. |
| Billing, activation, provisioning | Lifecycle transition services no longer write a tenant boolean. Provisioning and bootstrap scripts write lifecycle state directly. |
| Platform Operations and AI diagnostics | Any retained tenant `is_active` output is derived as `lifecycle_status == 'active'`. |

### Compatibility boundaries and non-tenant fields

- `PATCH /api/platform/tenants/{tenant_id}` retains its historical boolean
  input as an adapter, not an authority. `false` on an active tenant delegates
  to `suspend_tenant_service`; `true` on provisioning delegates to gated
  `activate_tenant`; `true` on frozen or suspended returns HTTP 409; active
  `true` is a no-op. It never writes tenant service state directly.
- Retained tenant-list/status and Platform Operations/AI boolean outputs are
  lifecycle-derived. `TenantProvisionOut.is_active` describes the separate
  `TenantWecomConfig.is_active` field, not tenant service state.
- The tenant configuration service continues rejecting the obsolete
  `is_active` client key. It is a denylist entry, not a model read/write.
- `TenantWecomConfig.is_active`, `BillingPlan.is_active`, key/config flags,
  contact identity state, and archive seat/output flags describe other entities
  and are intentionally unchanged.

### Historical-only and migration references

- Historical migrations `0001`, `0002`, `0040`, and `0051` remain unchanged.
  Their references preserve the original schema and migration history; they are
  not live runtime decisions. Historical migration tests retain legacy-schema
  fixtures only where they test those old revisions.
- `docs/research/wecom_employee_login_tenant_saas_foundation.md` and archived
  planner/roadmap documents under `deliverables/archive/` retain historical
  schema or planning examples. They are not current model/runtime contracts.
- Forward migration `0075` checks every stored pair against the canonical map
  (`active → true`; `provisioning`, `frozen`, `suspended → false`) and aborts on
  any mismatch before dropping the column. It never rewrites lifecycle state.
- Downgrade recreates the old column and derives it strictly from
  `lifecycle_status`; it leaves lifecycle state unchanged. Start the previous
  application only after schema downgrade and projection verification.
- Tests for historical migration revisions retain old-schema fixtures where
  needed. Current runtime fixtures no longer define or insert the tenant
  boolean. Dedicated GH-94 tests use synthetic old-schema rows to exercise
  forward mismatch abort and downgrade behavior.

## Canonical mapping and cutover safety

| `lifecycle_status` | Derived compatibility value |
| --- | --- |
| `active` | `true` |
| `provisioning` | `false` |
| `frozen` | `false` |
| `suspended` | `false` |

No code or migration may change lifecycle state to follow a boolean. The
forward migration fails closed if a stored pair is outside this mapping; it
does not repair, auto-activate, or silently change worker eligibility.

Rollback order after the column is dropped:

`stop/drain new runtime → schema downgrade → verify derived projection → start previous runtime`

Never run the previous application against a schema without its expected
column. Production migration/deployment requires the separate explicit human
approval described in GH-94, following PR review, required CI, and a fresh
read-only production preflight.
