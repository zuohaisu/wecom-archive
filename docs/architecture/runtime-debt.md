# Runtime Architecture Debt Register

> **Status:** authoritative register for intentionally retained runtime seams
> (GitHub #92, 2026-08-28). A `TRANSITIONAL` classification is not permission
> to remove a path. Every retirement gate must be met with evidence first.

## Rules for this register

- **CURRENT** means an intentional active boundary, not debt.
- **TRANSITIONAL** means the path exists now but the target state retires it.
- **UNKNOWN** means repository evidence cannot establish the required live fact.
- A green test suite proves repository behavior; it does not prove production
  traffic has moved off a compatibility path.

## Registered seams

| Name | Classification | Current evidence | Current risk | Target state | Retirement gate | Separate GitHub Issue required? | Related issue |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Legacy single-corp archive worker mode and default-bootstrap credential format | `TRANSITIONAL` | `run_archive_worker_once.py` selects the `WECOM_CORP_ID` path when `WECOM_TENANT_ID` is absent; the sync/decrypt compatibility shells retain that environment mode. GH-108 moved the production media and external-contact workers to tenant discovery, and the versioned archive unit now unsets the pair before timer execution. `bootstrap_default_tenant.py` can also create a historical default row with a plaintext OAuth value in `app_secret`, which is not the current tenant-scoped archive format. | Removing a fallback or assuming that the bootstrap row is tenant-ready can stop sync/decrypt for a tenant whose tenant-scoped credentials are unreadable or whose installed timer still selects the legacy path. #77 reports exactly that production-risk hypothesis. | Explicit tenant and all-active-tenants modes use encrypted, readable `TenantWecomConfig` archive credentials for every active tenant; no deployment selector or active tenant relies on the single-corp environment pair or default-bootstrap credential format. | Diagnose old tenant → repair/migrate tenant-scoped credentials → prove tenant-scoped sync/decrypt/cursor/media continuity → verify all legacy tenants and installed triggers migrated → approve separate removal implementation. Read-only Ops evidence is mandatory before any removal. | Yes | [#77](https://github.com/zuohaisu/wecom-archive-365/issues/77), [#93](https://github.com/zuohaisu/wecom-archive-365/issues/93) |
| Environment-scoped archive callback and private-key fallback | `TRANSITIONAL` | `tenant_callback_resolution.py` tries the environment callback pair first; `tenant_activation.py` accepts that pair as a runtime fallback; `key_provider.py` retains legacy `WECOM_PRIVATE_KEY_PATH` fallback. | A callback/key path can appear healthy while a tenant row is missing, incomplete, or unreadable. Removing a fallback before each tenant has stored credentials can reject callbacks or block decrypt. | Active tenant rows carry encrypted callback credentials and usable tenant/versioned private-key material; archive callback resolution no longer needs global archive secrets. | Same tenant-by-tenant continuity evidence as the legacy worker mode, plus callback GET/POST verification and decrypt-key availability for every migrated tenant. Confirm no operator-managed unit/configuration uses the fallback. | Yes; deliver only with the #93 retirement scope or a separately approved split | [#77](https://github.com/zuohaisu/wecom-archive-365/issues/77), [#93](https://github.com/zuohaisu/wecom-archive-365/issues/93) |
| `Tenant.is_active` compatibility projection | `TRANSITIONAL` | `Tenant` has both `lifecycle_status` and `is_active`; lifecycle/activation code writes both, and active-tenant worker selection still reads the boolean. | A future caller can read the boolean as an independent service authority, creating drift from `provisioning` / `active` / `frozen` / `suspended`. A blind schema removal can change worker eligibility or activation behavior. | `lifecycle_status` is the sole service-state representation and every caller consumes the centralized lifecycle/service-access policy. | Inventory every read/write; define migration behavior for legacy row combinations, provisioning, suspend/resume, platform operations, and all-tenants worker selection; add transition tests; obtain read-only production state inventory; then ship a separately approved migration/removal. | Yes | [#94](https://github.com/zuohaisu/wecom-archive-365/issues/94) |

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
