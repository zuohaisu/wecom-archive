# RND-420 QA Summary

## Files changed

- `tasks/RND-420-research-report.md`: records the 2026-08-28 provider-console evidence (E21), clarifies that product-sales/rebate and self-developed SaaS application paths are separate, and updates the guarantee-deposit conclusion.
- `tasks/RND-420-qa-summary.md`: records this validation.

## Acceptance criteria

- [x] The report states that the CNY 30,000 guarantee deposit applies only to the product-sales/rebate path.
- [x] The report states that this project's self-developed SaaS application path does not trigger that deposit.
- [x] E21's nature, source boundary, and conclusion are documented without committing the screenshot or credentials.
- [x] Documentation validation completed.

## Commands run

- `git diff --check`: passed.
- `git diff --cached --check`: passed.
- `env -u DATABASE_URL RND201_TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:1/postgres' PATH="$PWD/.venv/bin:$PATH" make verify`: passed — 3404 passed, 217 skipped, 86 warnings.

## Manual verification

- Reviewed the final report diff: the E21 entry references only Haisu-retained, redacted evidence and a local path; no image, credential, or account data is added to Git.

## Risks / gaps

- The full live-PostgreSQL migration suite was not run locally. An initially reachable shared local PostgreSQL instance caused concurrent scratch-database collisions; the passing run deliberately used the repository's normal offline mode, which skips only tests gated on unavailable PostgreSQL. Required CI will run the PostgreSQL gate against its isolated service.

No secrets introduced: confirmed.

Only intentional files changed: confirmed (verified with `git status --short`).

No test weakened, skipped, or deleted: confirmed.

Staged paths listed explicitly; no `git add .`: confirmed.
