# Contributing

Thanks for considering a contribution to Crowntime WeCom Archive.

## Before you start

Every external contributor must sign the [Contributor License Agreement](CLA.md)
before a pull request can be merged. The signing mechanism is not yet
automated — open an issue asking for signing instructions before you invest
time in a large change. Read the [security policy](SECURITY.md) first if what
you found is a vulnerability rather than a bug; those must not go in a public
issue.

## Setting up

Python 3.11+ and PostgreSQL 14+ are the supported baseline. PostgreSQL is
required to run the application, but not to run most of the test suite.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cp .env.example backend/.env      # never commit this file
```

See the README's [Local console](README.md#local-console) section for the
database and startup steps.

### The WeCom SDK

Archive synchronization, message decryption, and media download all go
through Tencent's proprietary WeCom C SDK, which this repository does not
and cannot redistribute. You do **not** need it to contribute: the console,
the API, and nearly the entire test suite run against a fake SDK and sample
data. You only need the real SDK to work on the live sync path itself, and
you must obtain it from Tencent under its own terms.

## Making a change

Run the acceptance chain before opening a pull request:

```bash
make verify        # lint on your diff, type/import check, build, tests
```

`make test` alone runs the suite. Tests that need a live PostgreSQL skip
themselves when `DATABASE_URL` is unset, so a run reporting skips is the
expected local result.

Pull requests run the same gates in CI via
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). A red CI is a red
pull request; please do not ask for a review until it is green.

## What makes a change easy to accept

- **One concern per pull request.** A bug fix bundled with a refactor is
  hard to review and harder to revert.
- **A test that fails before your change and passes after it.** For a bug
  fix this is the single most useful thing you can include.
- **Comments that explain why, not what.** This codebase leans heavily on
  explaining the reasoning behind non-obvious decisions; please match that.
- **No new lint findings in your diff.** `make lint-diff` checks exactly
  this. The repository carries a pre-existing whole-repo lint baseline that
  you are not expected to fix.

## Things to know

- **Decrypted message envelopes are never persisted.** The
  `archive_messages.decrypted_payload` column is always NULL by design —
  the worker extracts the fields it needs and stores those. This is a data
  minimization contract, not an oversight; please do not "fix" it.
- **The console is server-rendered with no template engine.** Follow the
  existing patterns in `backend/app/web/` rather than introducing one.
- **User-facing strings are localized** across three locales in
  `backend/app/assets/i18n.js`. A new string needs all three.
- **Database changes need an Alembic migration**, and CI runs
  `alembic check` to catch a model that has drifted from its migrations.

## Reporting bugs

Include the version or commit, what you expected, what happened, and the
minimal steps to reproduce it. Never paste real credentials, private keys,
conversation content, or personal data into an issue — this is an archive
product, and issue trackers are public.
