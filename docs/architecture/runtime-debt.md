# Runtime Architecture Debt Register

> **Status:** authoritative register for intentionally retained runtime seams
> (GitHub #92, updated by GH-94). A `TRANSITIONAL` classification is not
> permission to remove a path. Every retirement gate must be met with evidence
> first.

## Rules for this register

- **CURRENT** means an intentional active boundary, not debt.
- **TRANSITIONAL** means the path exists now but the target state retires it.
- **RETIRED** records a removed runtime seam and its provenance; it is not a
  claim that the new code has already received post-deploy production evidence.
- **UNKNOWN** means repository evidence cannot establish the required live fact.
- A green test suite proves repository behavior; it does not prove production
  traffic has moved off a compatibility path.

## Registered seams

| Name | Classification | Current evidence | Current risk | Target state | Retirement gate | Separate GitHub Issue required? | Related issue |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Legacy single-corp archive worker mode, environment archive credentials, and default-bootstrap credential format | `RETIRED` | GH-93 removes the `WECOM_CORP_ID` / `WECOM_ARCHIVE_SECRET` branches from the archive orchestrator and sync/decrypt shells. The only supported normal modes are explicit tenant and all-active-tenants, both resolved from encrypted `TenantWecomConfig`. #77 closure and #108 post-merge production verification prove no active archive/media/reconciliation workload depends on the shared pair; it may remain configuration-present for legitimate non-archive use. `bootstrap_default_tenant.py` remains a historical bootstrap artifact. | A deployment regression could interrupt tenant-scoped archive work; it is not remediated by restoring shared environment configuration. | Active tenant configuration is the only archive credential authority. Historical bootstrap/recovery artifacts never become a normal runtime selector. | #77 Gates 1–4 and #93 implementation authorization passed. GH-93 requires focused tests, CI, reviewed rollback, and post-deploy Ops verification. | Delivered by GH-93 | [#77](https://github.com/zuohaisu/wecom-archive-365/issues/77), [#108](https://github.com/zuohaisu/wecom-archive-365/issues/108), [#93](https://github.com/zuohaisu/wecom-archive-365/issues/93) |
| Environment-scoped archive callback and global private-key fallback | `RETIRED` | GH-93 removes environment callback candidates and the unregistered `WECOM_PRIVATE_KEY_PATH` fallback. Archive callbacks and decrypt private keys resolve only from the selected tenant's stored configuration or tenant-scoped `KeyVersion`. Historical migrations and explicit recovery tools retain provenance but are not active runtime callers. | Missing/unreadable tenant callback or key material fails closed instead of selecting a global credential. | Each active tenant has complete stored callback and key material; no archive callback or decrypt path derives authority from environment configuration. | Same #77/#108 evidence chain, plus GH-93 callback/key fail-closed tests, CI, rollback review, and post-deploy Ops verification. | Delivered by GH-93 | [#77](https://github.com/zuohaisu/wecom-archive-365/issues/77), [#108](https://github.com/zuohaisu/wecom-archive-365/issues/108), [#93](https://github.com/zuohaisu/wecom-archive-365/issues/93) |
| `Tenant.is_active` compatibility projection | `RETIRED` | GH-94 removes the mapped ORM field and all runtime tenant reads/writes. The legacy PATCH input delegates to lifecycle operations; retained outputs are derived. Migration 0075 checks the canonical lifecycle mapping and drops the column; its downgrade reconstructs the derived value. Ops' 2026-10-07 read-only evidence reports PostgreSQL 16.15 at Alembic 0073, one `active/true` tenant, and no other pair. The migration has not been executed in production. See [the retirement record](tenant-is-active-compatibility-assessment.md). | Production still needs a human-approved cutover. The old runtime must be stopped/drained before dropping the column; rollback requires schema downgrade before restarting the prior application. | No persisted or runtime tenant boolean authority; lifecycle status and centralized service policy decide access. | GH-94 implementation and migration are in one PR. After review, merge, and green required CI, Ops must receive explicit human authorization, re-check the deployed revision and invariant, stop/drain runtimes, run migration 0075, deploy, and smoke-test. Production migration/deployment is outside this Dev change. | Delivered by GH-94 | [#94](https://github.com/zuohaisu/wecom-archive-365/issues/94) |

## Deliberately not registered as retirement debt

- The all-active-tenants archive loop is a current runtime mode, not a legacy
  mode. It intentionally isolates individual credential/child-worker failures.
- `TenantStorageDaily` is an operational rollup, while the live downloaded-media
  sum is the capacity-write authority. This is an intentional authority /
  projection split, not duplicate quota policy.
- Platform Operations is intentionally a controlled projection/orchestration
  layer. Its manual ledger is separate from provider facts by design; removing
  that separation would be a billing semantic change, not cleanup.
- Versioned systemd files are deployment assets. The repository cannot prove
  that an omitted, manually installed, or operator-managed unit is absent from
  production, so installation state is `UNKNOWN — requires production/external evidence`.
