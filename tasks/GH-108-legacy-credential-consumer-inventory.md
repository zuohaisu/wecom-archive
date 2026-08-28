# GH-108 Legacy Credential Consumer Inventory

## Scope and method

Repository audit completed before implementation with case-insensitive searches for
`WECOM_CORP_ID`, `WECOM_ARCHIVE_SECRET`, `WECOM_TENANT_ID`, `_require_env`,
default-tenant/bootstrap paths, CorpID→tenant lookups, legacy credential
resolvers, direct SDK initialization, media credential construction,
external-contact construction, and `WECOM_THIRD_PARTY_*`.

This inventory classifies repository evidence only. Installation, enabled-unit,
and production-traffic facts are **not** inferable from git and require Ops
verification.

| Consumer | File(s) | Credential / identity dependency | Runtime? | Classification | Target |
| --- | --- | --- | --- | --- | --- |
| Archive timer entrypoint | `backend/scripts/run_archive_worker_once.py` | Explicit/all-active paths use encrypted `TenantWecomConfig`; legacy branch reads the selector pair | Yes | `CURRENT_TENANT_SCOPED` + `TRANSITIONAL_LEGACY_RUNTIME` | Keep legacy branch for #93; versioned timer unit now unsets the pair. |
| Archive sync/decrypt shells | `backend/scripts/sync_wecom_archive_once.py`, `backend/scripts/decrypt_wecom_messages_once.py` | `WECOM_TENANT_ID` resolves tenant credentials; no-selector legacy shell reads pair | Child processes | `CURRENT_TENANT_SCOPED` + `TRANSITIONAL_LEGACY_RUNTIME` | Preserve #93-owned compatibility branch. |
| Archive callback/manual dispatch | `backend/app/services/archive_worker_trigger.py`, `backend/app/routers/sync.py` | Authenticated/manual tenant is passed as `WECOM_TENANT_ID` | Yes | `CURRENT_TENANT_SCOPED` | GH-108 changed manual sync to pass its authenticated tenant; no global CorpID guard/selection remains. |
| Archive callback fallback | `backend/app/services/tenant_callback_resolution.py`, `backend/app/services/tenant_activation.py`, `backend/app/key_provider.py` | Environment callback/key candidates remain before/alongside stored tenant candidates | Yes | `TRANSITIONAL_LEGACY_RUNTIME` | #93 or separately approved callback/key retirement after production evidence. |
| Archive-complete media dispatch fallback | `backend/app/media_event_dispatch.py` | No-tenant fallback maps global CorpID; current explicit/all-active archive paths pass tenant id | Yes | `TRANSITIONAL_LEGACY_RUNTIME` | Preserve only while the #93 legacy archive branch exists. |
| Media timer/event worker | `backend/scripts/download_wecom_media_once.py`, `deploy/systemd/wecom-archive-media-{download,event}.service` | Optional explicit tenant or all active configs; SDK gets that tenant's CorpID/archive secret | Yes | `CURRENT_TENANT_SCOPED` | Migrated in GH-108. Ambient legacy pair is ignored; missing/unreadable config fails closed per tenant. |
| Daily external-contact reconciliation | `backend/app/services/external_contact_sync.py`, `deploy/systemd/wecom-external-contact-reconcile.{service,timer}` | Bounded daily-rotated active-config slice; CorpID comes from validated tenant config. `WECOM_EXTERNAL_CONTACT_SECRET` / `WECOM_OAUTH_SECRET` are separate API settings, never selectors. | Yes | `CURRENT_TENANT_SCOPED` (identity); `UNKNOWN` (API-secret scope) | Migrated in GH-108; sequential per-tenant runs roll back/isolate failures. `EXTERNAL_CONTACT_RECONCILE_TENANT_LIMIT` bounds work without changing the 04:15 schedule. Ops must confirm the API-secret authority separately. |
| Durable external-contact refresh | `backend/scripts/refresh_external_contacts_once.py`, `backend/app/services/external_contact_refresh_worker.py`, `deploy/systemd/wecom-external-contact-refresh.*` | Queue rows are selected by resolved tenant id; CorpID is passed from that same config; external-contact API-secret scope is not proven by repository data | Possibly installed; versioned path/timer is outside managed allowlist | `CURRENT_TENANT_SCOPED` (identity); `UNKNOWN` (API-secret scope) | Migrated in GH-108. Ops must inspect whether this optional unit is installed/enabled and verify the external API credential authority. |
| Group-chat metadata trigger | `backend/app/services/group_chat_metadata_refresh_trigger.py`, `backend/app/services/decrypt_worker.py` | Tenant and CorpID are passed from tenant-scoped decrypt; external-contact API setting is separate | Yes, archive child hook | `CURRENT_TENANT_SCOPED` | No global CorpID selection; retain global API setting pending its own provider-credential authority decision. |
| Reachability automation | `backend/scripts/run_reachability_automation_once.py`, `deploy/systemd/wecom-archive-reachability-check.*` | Explicit archive child tenant or all active configs; no CorpID lookup | Possibly installed; manual unit | `CURRENT_TENANT_SCOPED` | Migrated in GH-108 as an additional audit finding. Missing configs are visible fail-closed outcomes. |
| WeCom OAuth/admin login settings | `backend/app/settings.py`, `backend/app/routers/auth.py`, `backend/app/config/{schema,resolver,guard}.py`, `.env.example` | Global `WECOM_CORP_ID`, agent id, OAuth secret for the legacy/admin OAuth integration | Web app | `LEGITIMATE_GLOBAL_PROVIDER_CONFIG` | Out of GH-108: this is not an archive SDK selector pair. Do not delete with archive retirement. |
| Third-party provider authorization | `backend/app/settings.py`, `backend/app/services/wecom_org_authorization.py`, `backend/app/routers/wecom_provider_instructions.py`, `.env.example` | `WECOM_THIRD_PARTY_*`, encrypted suite ticket and per-tenant binding | Web app / callback | `LEGITIMATE_GLOBAL_PROVIDER_CONFIG` | Deliberately unchanged. These provider credentials are not legacy single-corp archive credentials. |
| Historical default bootstrap | `backend/scripts/bootstrap_default_tenant.py`, migrations `0002`, `0003`, `0024` | Global CorpID/OAuth values create historical default row | Bootstrap only | `HISTORICAL` | #93/debt-register follow-up; do not delete in GH-108. |
| Historical archive recovery tools | `backfill_missing_seqs_once.py`, `backfill_revoke_associations_once.py`, `reparse_structured_content_once.py`, `smoke_wecom_get_chat_data.py`, `smoke_wecom_sdk_init.py` | Direct legacy SDK CorpID/archive-secret initialization | Operator-invoked only | `HISTORICAL` | No versioned timer/managed-unit entry. Ops must report any manual runbook use before #93 removal. |
| Historical media/storage backfills | `migrate_local_media_to_qiniu.py`, `backfill_thumbnails_once.py`, `backfill_voice_transcode_once.py` | Global CorpID resolves a target tenant; no normal worker path | Operator-invoked only | `HISTORICAL` | Keep out of GH-108; distinct maintenance-tool migration if Ops finds active use. |
| Historical contact tools | `sync_contact_display_names_once.py`, `sync_group_chat_metadata_once.py` | Global CorpID plus OAuth/external-contact settings | Operator-invoked only | `HISTORICAL` | Keep out of GH-108; no timer/service evidence. |
| Non-production avatar backfill | `backend/scripts/sync_contact_avatars_once.py` | Global CorpID/OAuth but explicitly rejects production and needs an arming flag | Non-production only | `TEST_ONLY` | Deliberately unchanged. |
| Migrations, tests, fixtures | `backend/alembic/**`, `backend/tests/**`, `scripts/tests/**` | Synthetic fixture values and historical compatibility assertions | No | `TEST_ONLY` / `HISTORICAL` | Retain regression coverage; no production credential dependency. |
| Documentation, research, archived deliverables | `docs/**`, `deliverables/**`, `README.md` | References to old configuration/operations | No | `HISTORICAL` | Update only current runbooks affected by GH-108; retain history as history. |
| Manually installed versioned units not in `MANAGED_UNITS` | `deploy/systemd/wecom-archive-media-event.*`, `wecom-external-contact-refresh.*`, `wecom-archive-reachability-check.*`, manual backfill units | Repository cannot prove installed/enabled state | Unknown | `UNKNOWN` | **OPS EVIDENCE REQUIRED:** inventory installed timers/services and manual runbooks before #93 Gate 4 production pass. Migrated entrypoints no longer select via global CorpID; historical tools remain separate retirement candidates. |

## Deliberately protected global configuration

`WECOM_THIRD_PARTY_*` is provider-level configuration and remains untouched.
`WECOM_EXTERNAL_CONTACT_SECRET` and `WECOM_OAUTH_SECRET` are consumed as API
credentials only; GH-108 removes their use as tenant selectors. Repository
code does **not** prove whether either is provider-global or must become a
per-tenant credential. This remains `UNKNOWN — OPS EVIDENCE REQUIRED`; it is
not a reason to substitute one tenant's secret for another or to remove the
current configuration without Ops/product evidence.

## Repository-side conclusion

After this change, the normal archive timer, media worker, external-contact
reconciliation/refresh paths, reachability automation, and authenticated manual
archive trigger select tenant authority from `TenantWecomConfig` or trusted
session/callback scope. The remaining direct pair reads are historical tools or
#93-owned archive/callback compatibility paths.

**OPS EVIDENCE REQUIRED:** deploy, inspect effective units/runbooks, and
observe production behavior before claiming that no installed workload depends
on the legacy pair.
