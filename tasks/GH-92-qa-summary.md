## QA Summary

Files changed:
- `docs/architecture/current-state.md`: authoritative current architecture, domain-authority, documentation-hierarchy, contradiction, and UNKNOWN map.
- `docs/architecture/runtime-debt.md`: transitional runtime debt register and retirement gates.
- `docs/ARCHITECTURE.md`: replaces the superseded internal/single-tenant overview with the current architecture index.
- `docs/DATA_MODEL.md`, `docs/DEVELOPMENT.md`, `.env.example`: correct current tenancy/credential terminology and GitHub-Issue workflow guidance.
- `docs/operations/`, `docs/DEPLOYMENT.md`, `docs/ops/`: correct current callback/deployment wording and mark historical operational references.
- `docs/adr/`, `docs/research/`, `docs/CONVERSATION_REVIEW_CONSOLE_PRD.md`: mark superseded planning and historical references as non-authoritative.
- `docs/ai/`, `overview.md`, `TASK_TRACKING.md`, `PROJECT_DOCUMENTATION_REPORT.md`, `docs/ticket-autopilot-workflow.md`: retire competing historical onboarding/workflow descriptions.
- `backend/scripts/bootstrap_default_tenant.py`, `backend/app/key_provider.py`: documentation-only comments classify legacy compatibility behavior; runtime semantics are unchanged.
- `tasks/GH-92-qa-summary.md`: self-QA evidence, validation result, and remaining environment/independent-QA gaps.

Acceptance criteria:
- [x] Current Architecture Map and domain authority map: pass.
- [x] CURRENT / SUPERSEDED / TRANSITIONAL / UNKNOWN contradiction inventory: pass.
- [x] Authoritative documentation hierarchy names GitHub Issues as the active issue-management system: pass.
- [x] Legacy single-corp archive mode retained and registered with an evidence-gated retirement sequence: pass.
- [x] WeCom provider authorization, provider instructions, archive callback, tenant configuration, and archive runtime are separated: pass.
- [x] Billing, storage capacity, service lifecycle, and Platform Operations authority boundaries are documented without changing runtime semantics: pass.
- [x] Follow-up GitHub Issues created: #93 (legacy archive retirement) and #94 (`Tenant.is_active` projection retirement).
- [ ] `make verify`: blocked by missing local disposable PostgreSQL databases; see Commands run.

Commands run:
- `git diff --check`: pass.
- Markdown reference checker over `README.md` and `docs/**/*.md`: pass; no missing local links.
- Active-workflow Linear-reference scan over architecture/onboarding/development sources: pass; matches only explicit historical/provenance explanations.
- Added-diff credential-pattern review: pass; no candidate secret/private-key/connection-string additions found.
- `make verify` with a Python 3.9 virtual environment: fail — unsupported interpreter cannot parse the suite's UTC `Z` timestamps in two billing E2E tests. Recreated the ignored local virtual environment with Python 3.11, the repository baseline.
- `make verify` with Python 3.11: lint-diff, typecheck, and build passed. Test stage: 3436 passed, 158 skipped, 14 failed, 13 errors. Every failing/erroring case requires disposable local PostgreSQL databases under `/tmp`; they fail with `FATAL: database <test database> does not exist`. No #92 runtime/test behavior was changed. This is an environment/service blocker, not confirmed against an untouched baseline.

Manual verification:
- Reviewed current map/debt register against models, lifecycle/service-access, credential resolution, archive worker modes, callbacks, payment/refund, storage, Platform Operations, deployment scripts, manifests, and active GitHub #73/#76–#82/#92: pass.
- Confirmed no production configuration, systemd behavior, archive semantics, billing semantics, tenant activation semantics, or test assertions changed: pass.

Risks or gaps:
- `UNKNOWN — requires production/external evidence`: installed/enabled production units and selectors; actual legacy archive fallback usage; live payment/provider and self-service gate state. #77 is the required first read-only archive diagnosis.
- Independent QA was not assigned in this Dev conversation; CI/independent review remain required before merge.

No secrets introduced: confirmed
Only intentional files changed: confirmed (verified with git status --short)
No test weakened, skipped, or deleted: confirmed
Staged paths listed explicitly, no `git add .`: confirmed
