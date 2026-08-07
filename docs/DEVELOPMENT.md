# Development guide

Internal engineering guide for Crowntime WeCom Archive. This is a proprietary,
hosted-only product — see [LICENSE](../LICENSE) and
[ADR-0003](adr/0003-product-strategy-hosted-only.md). There is no external
contribution process; this document is for the people who work on it.

## Setting up

Python 3.11+ and PostgreSQL 14+ are the supported baseline. PostgreSQL is
required to run the application, but not to run most of the test suite.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cp .env.example backend/.env      # never commit this file
```

See the README's [Local console](../README.md#local-console) section for the
database and startup steps.

### The WeCom SDK

Archive synchronization, message decryption, and media download all go through
Tencent's proprietary WeCom C SDK, which is not in this repository and cannot
be redistributed. You do **not** need it for most work: the console, the API,
and nearly the entire test suite run against a fake SDK and sample data. You
only need the real SDK to work on the live sync path itself.

## Making a change

Run the acceptance chain before opening a pull request:

```bash
make verify        # lint on your diff, type/import check, build, tests
```

`make test` alone runs the suite. Tests that need a live PostgreSQL skip
themselves when `DATABASE_URL` is unset, so a run reporting skips is the
expected local result.

Pull requests run the same gates in CI via
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). A red CI is a red
pull request; do not ask for a review until it is green. Merging to `main`
triggers the production deployment pipeline
([`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)).

## What makes a change easy to review

- **One concern per pull request.** A bug fix bundled with a refactor is hard
  to review and harder to revert.
- **A test that fails before your change and passes after it.** For a bug fix
  this is the single most useful thing you can include.
- **Comments that explain why, not what.** This codebase leans heavily on
  explaining the reasoning behind non-obvious decisions; match that.
- **No new lint findings in your diff.** `make lint-diff` checks exactly this.
  The repository carries a pre-existing whole-repo lint baseline that you are
  not expected to fix.

## Things to know

- **Decrypted message envelopes are never persisted.** The
  `archive_messages.decrypted_payload` column is always NULL by design — the
  worker extracts the fields it needs and stores those. This is a data
  minimization contract, not an oversight; do not "fix" it.
- **The console is server-rendered with no template engine.** Follow the
  existing patterns in `backend/app/web/` rather than introducing one.
- **User-facing strings are localized** across three locales in
  `backend/app/assets/i18n.js`. A new string needs all three.
- **Database changes need an Alembic migration**, and CI runs `alembic check`
  to catch a model that has drifted from its migrations.
- **Archive private keys are the highest-severity asset in this system.** As a
  hosted service we hold every customer's decryption key. Never let key
  material reach logs, backups, error payloads, or plaintext database columns.
  See [key-hosting-and-tenant-isolation.md](key-hosting-and-tenant-isolation.md).

## Reporting bugs

Include the version or commit, what you expected, what happened, and the
minimal steps to reproduce it. Never paste real credentials, private keys,
conversation content, or personal data into an issue — this is an archive
product handling other companies' conversations.

Security issues follow [SECURITY.md](../SECURITY.md) instead.
