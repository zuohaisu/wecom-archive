[Goal check] This work advances RND-415 implementation verification by recording acceptance evidence and the remaining environment-specific test blocker.

# RND-415 Developer QA Summary

## Files changed

- `backend/app/{db/models.py,services/platform_accounts.py,schemas/platform_accounts.py,routers/platform_accounts.py}` — pending platform operators, hashed one-time invitations, password history/policy, session revocation, guarded APIs and page routes.
- `backend/alembic/versions/0067_rnd415_platform_operator_accounts.py` — platform-account schema migration with a SQLite roundtrip and pending-invitation downgrade guard.
- `backend/app/web/{templates,static}` — platform-themed account settings, controlled operator invite wizard, public activation page, and navigation entry.
- `backend/app/{audit.py,auth.py,routers/platform_auth.py,main.py}` — audit catalogue/display text, password-null-safe authentication, last-login tracking, router composition, and activation-token URL log redaction.
- `backend/tests/test_rnd415_platform_accounts.py` plus route/model/fixture contract updates — focused backend, template, migration, and regression coverage.

## Acceptance criteria

- [x] `/platform/settings` has Password and Operators tabs, uses `data-plane="platform"` and the existing orange theme without new theme variables.
- [x] Password rotation verifies the current password, requires 12+ characters with upper/lower/digit, rejects the active plus four preceding passwords, and defaults to revoking other sessions while retaining the current browser session.
- [x] Operator invitation creates a `pending` super-admin without a password, sends only a 24-hour hashed one-time activation link, and the public acceptance flow consumes it exactly once.
- [x] Operator list exposes only name, email, fixed v1 role, status, and last login; no token, password, or password hash is returned.
- [x] Invitation, acceptance, and password-change audit events are catalogued, localized, and retain no password/hash/token; `platform/accept-invite` query strings are redacted from application access logs.
- [x] The platform side nav links to the real Account & Security page; the invite wizard uses the RND-414 shared two-step controlled-operation modal.
- [x] Model/migration tests cover the history and invitation tables; focused tests and architecture guard pass.

## Commands run

- `git diff --check` — PASS.
- `node --check backend/app/assets/i18n.js` and all changed platform JS assets — PASS.
- `python -m pytest backend/tests/test_rnd415_platform_accounts.py -q` — PASS, 9 passed.
- `python -m pytest backend/tests/test_audit_page.py backend/tests/test_rnd415_platform_accounts.py backend/tests/test_http_contract.py backend/tests/test_architecture_boundary.py -q` — PASS, 107 passed.
- `make BACKEND_PY=<existing repo venv> verify` — lint-diff, typecheck, and build PASS; full pytest reaches 3352 passed / 219 skipped but is BLOCKED by two unrelated RND-406 assertions. The available repository venv is Python 3.9.6, whose `datetime.fromisoformat()` cannot parse the existing `...Z` response timestamps used by those tests. The RND-415 focused tests pass in that same environment.
- `alembic upgrade head --sql` — BLOCKED before RND-415 at existing migration `0004_tenant_wecom_config_corp_id_uniqueness.py`, which performs a live result fetch unsupported by Alembic offline SQL mode. The RND-415 SQLite migration roundtrip passes.

## Manual verification

- TestClient confirms unauthenticated settings pages redirect to `/platform/login`, authenticated pages render the platform design assets and nav, and activation HTML does not reflect a supplied bearer token.
- Invitation delivery, token hashing, activation/replay prevention, session revocation, password-history rejection, audit detail redaction, and last-login list presentation are exercised end to end by the focused suite.

## Risks / gaps

- A real PostgreSQL upgrade/downgrade and a real SMTP delivery must be run in a controlled non-production environment before release. Invites fail closed with `503 platform_invite_base_url_unavailable` if neither `INVITE_BASE_URL` nor `ADMIN_DOMAIN` supplies an HTTP(S) base URL.
- The full-suite blocker is external to this ticket and must be rerun under the repository's supported Python version (>=3.12 according to `uv.lock`) or fixed in its owning RND-406/tooling scope; do not mask it here.

No secrets introduced: confirmed.

Only intentional RND-415 files changed: confirmed.

No commit, push, pull request, deployment, or Linear state change performed.
