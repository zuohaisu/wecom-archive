# Current Architecture Map

> **Status:** authoritative repository-backed architecture map (GitHub #92, 2026-08-28).
>
> This document describes what the checked-in code, configuration contract, and
> active GitHub Issues prove. It does **not** claim a live production fact that
> requires an operator check. `UNKNOWN` items remain unknown until read-only
> Ops evidence exists.

## Product and deployment boundary

Crowntime WeCom Archive is a **private, proprietary, hosted multi-tenant
SaaS**. It is not an open-source, self-hosted, or on-premises product. The
customer-side principal is an organization represented by a `Tenant`; its
initial customer administrator is an `Owner`. Platform personnel use the
separate `PlatformAdmin` identity.

Repository evidence:

- `README.md` and ADR-0003 define the hosted, proprietary product boundary.
- `backend/app/db/models.py`, tenant-scoped sessions, credential rows, workers,
  billing records, and platform-admin models implement multi-tenancy.
- `scripts/deploy_server.sh` and `docs/DEPLOYMENT.md` define the repository's
  controlled hosted deployment contract; `scripts/deploy_nonprod.sh` and
  `deploy/systemd/wecom-archive-365-nonprod.service` define an isolated
  non-production instance.
- GitHub #73–#82 define the currently gated customer-launch work. In particular,
  the third-party self-service entry remains closed by default in
  `.env.example`; repository code is not proof that it is enabled in production.

The versioned deployment contract assumes a FastAPI service, PostgreSQL,
WeCom's proprietary C SDK, worker processes, and local and/or Qiniu media
storage. The repository deliberately does **not** version the production web
service unit or reverse-proxy configuration. Whether an asset is installed,
enabled, or carrying traffic is therefore `UNKNOWN — requires production/external evidence`.

## Identity, tenancy, and service authority

| Concept | Authority | Boundary |
| --- | --- | --- |
| Tenant | `Tenant` row in `backend/app/db/models.py` | Company/organization isolation root. `lifecycle_status` is the tenant-service projection. `is_active` is a retained compatibility projection, not an independent lifecycle authority. |
| Tenant user and roles | `AdminUser` plus `AdminLoginIdentity` | Tenant-scoped user roles are `owner`, `admin`, `compliance`, `legal`, and `readonlyaudit`. Login identity binding, not a legacy `wecom_user_id` lookup, is the authorization authority. |
| Owner | `AdminUser.role == "owner"` in the tenant | The first Owner is created from a trusted third-party administrator authorization. Owners can use the restricted provisioning surface and the permitted billing surface; they are not PlatformAdmins. |
| Tenant session | `AdminSession.tenant_id` and `session_scope` | Tenant APIs derive tenant scope from the authenticated session, never a caller-supplied tenant parameter. `provisioning` sessions are restricted to the provisioning routes. |
| PlatformAdmin | `PlatformAdmin` and `PlatformAdminSession` | Tenant-less, separate credentials/session, cross-tenant operations only through platform authorization and audit. A tenant role cannot satisfy platform authentication. |
| Provisioning identity | trusted proof → claim → `ThirdPartyOrganizationBinding` → Owner + provisioning session | `organization_provisioning.py` atomically creates the tenant, binding, Owner, login identity, and restricted session. Browser input does not supply CorpID, agent ID, or lifecycle. |
| Activation gate | `TenantActivationCheck` and `tenant_activation.py` | Gate state (`not_started` / `blocked` / `ready`) is distinct from the tenant lifecycle. Promotion to `active` is centralized and audited. |
| Service lifecycle | `Tenant.lifecycle_status` plus `billing_lifecycle.py` and `service_access.py` | `provisioning`, `active`, `frozen`, and `suspended` control service access. Subscription time policy is projected here; manual suspension wins over payment. |

## WeCom boundaries

These are separate integrations. Do not merge them into a generic “WeCom
module” or reuse credentials across them.

| Boundary | Authoritative inputs/state | Entry points |
| --- | --- | --- |
| Third-party provider authorization | `WECOM_THIRD_PARTY_*`; newest encrypted `WecomSuiteTicketState`; trusted proofs/claims/bindings | `/api/auth/wecom/third-party/install` and `/api/auth/wecom/third-party/callback`; `wecom_org_authorization.py` |
| Provider instruction callback | independent instruction Token/AES key; `WecomSuiteTicketState` | `/api/wecom/third-party/instructions`; `wecom_provider_instructions.py` |
| Archive callback | active per-tenant callback credentials in `TenantWecomConfig`; transitional env candidate described below | `/api/wecom/archive/events`; `wecom_events.py` and `tenant_callback_resolution.py` |
| Tenant archive configuration | encrypted `TenantWecomConfig` fields and trusted `ThirdPartyOrganizationBinding` | provisioning config routes; `tenant_config_service.py` |
| Archive runtime credentials | encrypted tenant config resolved by `tenant_credentials.py` | explicit-tenant/all-tenants worker paths; legacy archive environment path is transitional |
| External-contact reconciliation identity | active `TenantWecomConfig` resolved by `tenant_credentials.py`; provider API settings remain separate | daily reconciliation and durable refresh workers run one tenant at a time; a tenant never derives identity from global `WECOM_CORP_ID` |

The provider instruction callback has an intentional asymmetric receiver
contract verified in `test_rnd350_suite_ticket_lifecycle.py`:

- GET URL verification decrypts an envelope whose receiver must be the
  provider CorpID (`WECOM_THIRD_PARTY_CORP_ID`).
- POST `suite_ticket` instructions require both the encrypted envelope receiver
  and plaintext `<SuiteId>` to match `WECOM_THIRD_PARTY_SUITE_ID`.

A `suite_ticket` is encrypted in the database, is fresh for less than 20
minutes, warns from 20 minutes, and expires at 30 minutes. Authorization reads
that database value; it must never use an environment ticket.

## Archive runtime map

### Runtime modes

`backend/scripts/run_archive_worker_once.py` is the single sync/decrypt
orchestrator and owns the cross-process archive lock.

| Selector state | Mode | Credential source | Failure behavior |
| --- | --- | --- | --- |
| `WECOM_TENANT_ID` set | Explicit tenant | encrypted, active `TenantWecomConfig` | Hard-fails that tenant chain when credentials are unavailable. Used for callback, manual, and activation dispatch. |
| no tenant selector; `WECOM_CORP_ID` set | Legacy single-corp | environment `WECOM_CORP_ID` / `WECOM_ARCHIVE_SECRET` plus config lookup | Compatibility path. It is `TRANSITIONAL`, not a removal candidate in #92. |
| neither selector set | All-active-tenants loop | each active tenant's encrypted config | Runs tenants independently; a bad tenant does not abort a successful tenant. |

The worker runs sync then decrypt. `sync_states` owns the tenant-and-CorpID
cursor; `archive_messages` owns persisted archive envelopes and normalized
message state; `media_files` owns media download state. Decryption does not
persist a full decrypted payload. `download_wecom_media_once.py` is the sole
media-worker entry point.

A validated archive callback resolves one active tenant and dispatches an
explicit-tenant worker without waiting for completion. The archive timer is
reconciliation, not a second authority. The repository-managed archive unit
unsets the legacy selector pair before entering this mode; the temporary #77
operator drop-in remains a production-transition safeguard until Ops verifies
the deployed unit. After successful sync/decrypt, the worker may wake the
generic media worker; media has its own lock and durable retry/capacity state.
Its normal timer/event path discovers every active tenant and initializes one
SDK session from that tenant's own encrypted archive credentials, never from
ambient `WECOM_CORP_ID` / `WECOM_ARCHIVE_SECRET`. External-contact daily and
refresh workers use the same tenant discovery and isolate a tenant failure
before continuing. The daily job processes a bounded, daily-rotated tenant
slice (`EXTERNAL_CONTACT_RECONCILE_TENANT_LIMIT`) without changing its 04:15
schedule. Timer and callback trigger names are operational attribution, not
business inputs.

The repository versions archive, media, payment, lifecycle, and other systemd
units. `deploy/systemd/MANAGED_UNITS` is a narrow auto-sync allowlist, not a
claim that every versioned unit is installed — `deploy/systemd/WORKLOAD_MANIFEST`
(GH-104) is the authoritative classification of which units are required,
deferred, manual, deprecated, or out of scope; see
`docs/operations/scheduled-workload-manifest.md`. See `docs/DEPLOYMENT.md` for the
repo-owned/operator-managed boundary.

## Commercial and storage authority map

| Domain | Source of truth | Explicit non-authorities |
| --- | --- | --- |
| Plan | `BillingPlan` and `PlanEntitlement`; read through `entitlements.py` | browser plan/price/quota values |
| PaymentOrder | `PaymentOrder`, verified `PaymentEvent`, and `payment_orders.py` | checkout display, browser redirect, HTTP 200, or unverified callback body |
| Subscription | current `Subscription`, append-only `SubscriptionHistory`, and idempotent `SubscriptionActivation` | raw provider status or tenant lifecycle alone |
| Entitlement | effective subscription summary in `entitlements.py` | frontend claims; order creation alone |
| Exact paid term | `SubscriptionTermGrant` | date subtraction or a later subscription snapshot |
| Refund | `RefundOrder`, append-only verified `RefundEvent`, and `refunds.py` | refund request/processing state; manual financial ledger |
| Tenant service state | `Tenant.lifecycle_status`, projected by `billing_lifecycle.py` and applied through `service_access.py` | payment/refund state, UI state, or manual ledger rows |
| Manual finance | `ManualFinancialTransaction` | provider receipt/refund facts; it never grants or reverses entitlement |
| Storage usage | live tenant-scoped sum of downloaded `MediaFile.file_size` | browser value or stale rollup |
| Storage capacity/write gate | `storage_capacity.py` under a tenant row lock | UI utilization calculation |
| Daily storage rollup | `TenantStorageDaily` maintained by the rollup service | the write-gate authority; it is an operational projection |

`Platform Operations` is a **projection and controlled-operations layer**, not
a second billing business-rule engine. `platform_operations.py` composes
aggregate tenant/commercial views while keeping provider facts and manual
ledger rows separate. Its high-risk commands authenticate a PlatformAdmin,
require confirmation/reason/idempotency audit, then call the existing
lifecycle/payment/refund authority paths. Its controlled manual subscription
assignment and manual ledger records remain explicit, audited operations; they
must not be reinterpreted as verified provider facts.

## Authoritative documentation hierarchy

1. `AGENTS.md` — binding contribution, safety, ticket, validation, and
   architecture-boundary rules.
2. This document and [runtime-debt.md](runtime-debt.md) — current repository
   architecture, authority boundaries, classifications, and removal gates.
3. Executable authority — current models, services, routers, worker scripts,
   migrations, tests, `.env.example`, deployment workflow/scripts, and
   versioned unit manifests. When prose conflicts with executable behavior,
   the executable source wins and the prose must be corrected.
4. `docs/DEPLOYMENT.md` and `docs/operations/` — deployment/runbook contracts;
   repository text cannot prove live production state.
5. ADRs — decisions with their stated scope. ADR-0002 through ADR-0006 remain
   decision context where their code is current. ADR-0001 is historical
   unimplemented conversation-model planning, not a current implementation
   contract.
6. Research, PRDs, old runbooks, and migration records — historical/reference
   material only unless a current document explicitly promotes a fact.

GitHub Issues are the active issue-management system. RND identifiers retained
in history are GitHub-issue migration identifiers, not a requirement to use
Linear. A historical Linear URL is evidence of provenance only and cannot
supply current instructions.

## Contradiction inventory

| Classification | Finding and resolution |
| --- | --- |
| `CURRENT` | Hosted/proprietary product boundary in README and ADR-0003; tenant-scoped models, credentials, service lifecycle, billing, PlatformAdmin, third-party authorization, and isolated non-production deployment in current code/config. |
| `CURRENT` | Tenant-scoped encrypted configuration is the intended archive credential authority. Explicit-tenant dispatch and all-active-tenants operation are implemented and tested. |
| `SUPERSEDED` | The previous `docs/ARCHITECTURE.md` described an internal/admin-only, single-default-tenant MVP with unrestricted employee login. It is replaced by this map and the architecture index. |
| `SUPERSEDED` | “Future SaaS”, plaintext tenant-secret, active Linear-workflow, and open-source/self-host-first wording in older explanatory material are not current implementation direction. Historical records retain provenance only. |
| `SUPERSEDED` | ADR-0001's proposed `conversations` / `conversation_members` / dual-write implementation was never adopted by the current code. It is explicitly marked historical rather than a live architecture plan. |
| `TRANSITIONAL` | Legacy single-corp archive credentials and callback/key fallbacks remain for compatibility. GitHub #77 is active evidence that removal could interrupt archive continuity. See the debt register. |
| `TRANSITIONAL` | `Tenant.is_active` remains a compatibility projection beside `lifecycle_status`; its eventual removal needs a separate lifecycle dependency audit. See GitHub #94. |
| `UNKNOWN` | Which versioned units, environment selectors, callbacks, payment providers, or self-service gates are active in production. Requires read-only Ops evidence; do not infer from source control. |
| `UNKNOWN` | Whether every production tenant has readable tenant-scoped archive credentials and no longer uses a legacy timer fallback. GitHub #77 owns the first read-only diagnosis. |

## Follow-up boundaries

- [GitHub #93](https://github.com/zuohaisu/wecom-archive-365/issues/93) is the
  evidence-gated removal path for legacy single-corp archive runtime. It is
  blocked by the production diagnosis/migration evidence in [GitHub #77](https://github.com/zuohaisu/wecom-archive-365/issues/77).
- [GitHub #94](https://github.com/zuohaisu/wecom-archive-365/issues/94) is the
  separate dependency audit for retiring `Tenant.is_active` as a compatibility
  projection.
- #92 changes documentation and classification only. It does not authorize a
  production change, a credential operation, or a runtime-path deletion.
