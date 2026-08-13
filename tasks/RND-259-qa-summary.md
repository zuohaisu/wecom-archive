# RND-259 QA Summary

## Files changed

- `backend/alembic/versions/0049_rnd259_tenant_branding.py`, `backend/app/db/models.py`: tenant-isolated Logo/Favicon bytes and one managed-domain lifecycle row.
- `backend/app/services/branding.py`, `backend/app/routers/branding.py`, `backend/app/schemas/branding.py`: entitlement-gated branding API, strict image validation/re-encoding, DNS TXT ownership verification, host gate, fallback assets, and approved public-base helper.
- `backend/app/routers/platform.py`: PlatformAdmin-only non-secret TLS-controller work list, lifecycle report endpoint, and aggregate lifecycle metrics.
- `backend/app/main.py`, templates, `branding.js`, `sidenav.py`: Settings / 品牌 UI; current tenant Logo/Favicon on login, console, and all favicon/manifest links.
- `backend/app/audit.py`, `i18n.js`: redacted branding lifecycle audit actions and localized activity labels.
- `backend/tests/test_rnd259_paid_branding.py` plus route/i18n/RBAC snapshot updates: automated coverage for the new contract.
- `docs/custom-branding-operations.md`, `docs/DATA_MODEL.md`: paid-cloud positioning, safety/operations lifecycle, open-source boundary, and data model.

## Acceptance checks

- [x] `custom_branding` and `custom_domain` use the existing server-authoritative entitlement service; direct unauthorised API calls return fixed `*_upgrade_required` 403 responses.
- [x] Logo and optional Favicon support safe decoded formats only, declared-vs-real MIME validation, byte/dimension/pixel limits, metadata-stripping re-encoding, tenant scoping, no-store asset delivery, and default fallback.
- [x] One normalized custom hostname per tenant is globally unique; IP, wildcard, localhost/internal/reserved and platform names are rejected.
- [x] Raw DNS TXT value is returned once only; only SHA-256 remains in storage/audit. DNS, certificate, entitlement, tenant-active and exact-Host gates fail closed.
- [x] Certificate `failed`/`expired`, disabled, unbound, unverified, unknown, duplicate-Host and subscription-expired requests return to platform/default or `421`; configuration persists for later recovery.
- [x] WeCom/OAuth/callback settings remain platform-domain based; no custom request Host or forwarded Host is used to create an absolute URL.
- [x] Audits and PlatformAdmin metrics/work list contain no verification token, private key, certificate material or raw controller error.

## Commands run

- `make verify`: **PASS** — 2994 passed, 90 skipped.
- `make lint-diff`: **PASS** after rebasing the migration to revision `0049` / parent `0048`.
- `python -m compileall -q backend/alembic/versions`: **PASS**.
- `cd backend && ../.venv/bin/python -m pytest tests/test_rnd259_paid_branding.py -q`: **PASS** — 4 passed.
- `node --check backend/app/web/static/branding.js`: **PASS**.
- `node --check backend/app/assets/i18n.js`: **PASS**.
- `git diff --check`: **PASS**.

## Manual / source verification

- Confirmed every server-rendered template now uses `/api/branding/favicon` and the host-scoped manifest rather than a fixed platform favicon.
- Confirmed all remaining 康冠时代 references in login/reset pages are required legal/attribution copy; the provisioning-only billing shell remains platform branding.

## Risks / production gate

- The application control plane is complete and fails closed, but the repository has no approved multi-tenant edge/ACME provider implementation. A formally approved managed TLS controller must consume the PlatformAdmin work list, provision routing/certificates, report lifecycle status, and be failure/renewal exercised before production enablement. Until then domains remain pending and cannot route.
- No secrets introduced: confirmed.
- Only intentional RND-259 files changed: confirmed.
- No commit, push, PR, or Linear modification performed.
