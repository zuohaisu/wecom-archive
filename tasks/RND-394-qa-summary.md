# RND-394 QA Summary

## QA Summary

Files changed:

- `backend/app/services/trial_subscriptions.py`: add the trusted, once-only 15-day trial assignment boundary.
- `backend/app/services/subscription_activation.py`: retain a valid trial's start and end date when a verified payment converts it to the annual subscription.
- `backend/app/audit.py`, `backend/app/web/templates/audit_log.html`, `backend/app/assets/i18n.js`: catalogue and render the trial-start audit event in all supported locales.
- `backend/tests/test_rnd394_trial_subscriptions.py`: cover trusted eligibility, exact duration, replay, historical reuse prevention, and disabled-owner rejection.
- `backend/tests/test_rnd384_subscription_activation.py`: cover payment during a valid trial extending from the trial end.

Acceptance criteria:

- [x] Only a third-party-authorized organization with an active Owner can receive a trial.
- [x] A trusted trial starts at the supplied server-trusted completion time and ends exactly 15 days later.
- [x] Repeated completion notifications replay the active trial without duplicate subscription history or audit rows.
- [x] A trial cannot be granted again after it has expired or after any other current subscription exists.
- [x] A verified payment during an active trial keeps the original trial start and extends the annual term from the trial end.
- [x] Trial start is emitted as an immutable, localized subscription audit event without credentials or personal claim data.

Commands run:

- `.venv/bin/python -m pytest -q backend/tests/test_rnd394_trial_subscriptions.py backend/tests/test_rnd384_subscription_activation.py backend/tests/test_audit_page.py backend/tests/test_rnd335_security_activity.py`: 36 passed, 2 skipped.
- `make lint-diff`: passed.
- `PYTHONPYCACHEPREFIX=/private/tmp/wecom-archive-pycache make typecheck`: passed.
- `PYTHONPYCACHEPREFIX=/private/tmp/wecom-archive-pycache make build`: passed.
- `PYTHONPYCACHEPREFIX=/private/tmp/wecom-archive-pycache .venv/bin/python -m pytest -q backend/tests/test_makefile_lint_diff.py`: 2 passed.
- `git diff --check`: passed.

Manual verification:

- Reviewed the entitlement and storage-capacity boundary: its existing `trial` status support grants the same plan entitlements only within the recorded time window; no schema migration or client-supplied price/quota input was added.
- Reviewed the payment activation branch: it still requires a trusted successful payment before entering the renewal path.

Risks or gaps:

- This ticket deliberately does not expose a route or UI control. RND-388/RND-396 must invoke this service only after their trusted configuration-ready event; RND-395 owns the user-facing trial and renewal presentation.
- The full suite's initial sandbox run could not create its required temporary git worktree. The infrastructure test passes when granted that local `.git` permission; the deployment CI remains the authoritative full-suite gate.

No secrets introduced: confirmed.

Only intentional files changed: confirmed.
