# Historical Development Guide

> **Superseded:** This internal guide predates the AGPL-3.0 and self-hosted
> decisions. Its hosted-only and “no external contribution process” statements
> are historical, not current. See [CONTRIBUTING.md](../CONTRIBUTING.md) for
> today's contributor workflow and [ADR-0007](adr/0007-runtime-edition-policies.md)
> for the current runtime policy.

This page remains for historical engineering notes; do not use its old
product-boundary statements as current policy.

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

Every change runs in an assigned non-`main` delivery worktree and branch, based
on an up-to-date `origin/main`. Never develop on, commit to, or push directly to
`main`. Each GitHub Issue keeps its own implementation conversation and exactly
one final commit. A worktree/branch/pull request may group several related issue
commits, normally from the same Epic. Those issues must be implemented serially
so uncommitted changes from different tickets never coexist. Agents must verify
`git branch --show-current` before editing and stop if it reports `main`.

Run the acceptance chain before opening a pull request:

```bash
make verify        # lint on your diff, type/import check, build, tests
```

`make test` alone runs the suite. Tests that need a live PostgreSQL skip
themselves when `DATABASE_URL` is unset, so a run reporting skips is the
expected local result.

Pull requests run the same gates in CI via
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). A red CI is a red
pull request; do not merge until it is green and human review is complete.
When Merge Queue is enabled, the same CI also runs for the merge-group
candidate. A multi-ticket PR must list its issue-to-commit mapping and use a
merge strategy that preserves the individual ticket commits; do not squash it
into one commit.

Merging deployable paths to `main` triggers the CD-only production workflow
([`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)). CD deploys
the exact merged `main` SHA and retains migration, readiness, serialization,
and rollback gates, but it does not repeat the complete CI suite. Require a
pull request and green CI, keep the branch up to date, and never push directly
to `main`. Branch-protection settings are managed in GitHub, outside this
repository; verify them against [the binding repository rules](../AGENTS.md)
before changing merge policy or maintainers.
Documentation/task-only merges still pass PR CI but are excluded from CD by the
workflow's `paths-ignore` list.

## What makes a change easy to review

- **One coherent delivery per pull request.** A PR may contain several related
  same-Epic ticket commits, but unrelated fixes or refactors belong in another
  delivery branch and PR.
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
- **Archive private keys are the highest-severity asset in this system.** The
  application handles tenant decryption keys. Never let key material reach
  logs, backups, error payloads, or plaintext database columns.
  See [key-hosting-and-tenant-isolation.md](key-hosting-and-tenant-isolation.md).

## Reporting bugs

Include the version or commit, what you expected, what happened, and the
minimal steps to reproduce it. Never paste real credentials, private keys,
conversation content, or personal data into an issue — this is an archive
product handling other companies' conversations.

Security issues follow [SECURITY.md](../SECURITY.md) instead.
