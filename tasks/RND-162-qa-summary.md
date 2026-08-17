# RND-162 QA Summary

## Files changed

- `backend/alembic/versions/0065_rnd162_product_analytics_events.py`, `backend/app/db/models.py`: privacy-minimal, indexed product-event storage.
- `backend/app/schemas/product_analytics.py`, `backend/app/services/product_analytics.py`, `backend/app/routers/product_analytics.py`: closed v1 event allowlist, best-effort collection, retention cleanup, bounded aggregate queries, tenant detail and PlatformAdmin-only APIs.
- `backend/app/routers/auth.py`, `backend/app/routers/ai_support.py`: safe backend events for password/WeCom login success, password login failure, language changes and submitted feedback.
- `backend/app/web/static/product-analytics.js` and tenant UI assets/templates: asynchronous first-party behavior collection plus a Product Analytics area in `/platform/operations`.
- `docs/product-analytics-events-v1.md`, `docs/API.md`, `docs/DATA_MODEL.md`, `.env.example`, cleanup script: contract, API, model, toggle and retention documentation.
- `backend/tests/test_rnd162_product_analytics.py`, `backend/tests/test_http_contract.py`: behavior, privacy, permission, failure-isolation, retention, UI and route-contract coverage.

## Acceptance criteria

- [x] PlatformAdmin can view aggregate active/inactive tenants, login trend/failure rate, function adoption and per-tenant product-use details.
- [x] Overview, tenant list and tenant detail accept bounded time, tenant and canonical event filters; tenant list also filters active/inactive status.
- [x] Tenant sessions can submit only their own allowlisted browser events; they receive `401` on PlatformAdmin analysis APIs.
- [x] `PRODUCT_ANALYTICS_ENABLED=false` prevents persistence; collection and backend recording failures are best-effort and do not alter the primary action response.
- [x] Event-id primary-key idempotency, 180-day raw-event retention, indexed query paths and cleanup script are implemented.
- [x] Product events are separate from `AuditLog`; analytics APIs never return raw event attributes, archive content, search terms, contact identifiers, media information or credentials.
- [x] Contract documents versioning, fields, privacy, triggers, retention and prohibited data.

## Commands run

- `cd backend && alembic heads`: passed — `0065 (head)`, rebased from the upstream `0064` baseline.
- `git diff --check`: passed.
- `PATH=<temporary python wrapper>:$PATH make BACKEND_PY=/Users/zuohaisu/Documents/code/wecom-archive-365/.venv/bin/python verify`: passed — 3253 passed, 219 skipped.

The worktree has no local `.venv`; verification used the existing repository virtual environment through an absolute path and a temporary untracked `python` wrapper required by the Makefile self-test. No environment or repository configuration was changed.

## Manual verification

- Reviewed the Platform Operations template and static script: the Product Analytics region renders aggregate metrics, trends, active/inactive tenant list, tenant detail and filters without DOM interpolation of server data.
- Confirmed `product-analytics.js` only sends the reviewed event/attribute allowlist and never accepts tenant/admin identity or free-form data.

## Risks or gaps

- The retention script is intentionally scheduler-neutral; deployment operations must schedule `backend/scripts/purge_product_analytics_events_once.py` daily as documented. No deployment configuration was changed.
- Migration application was validated for a single Alembic head locally; no production database was accessed.

No secrets introduced: confirmed.

Only intentional files changed: confirmed.
