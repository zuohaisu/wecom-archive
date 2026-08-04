[Goal check] This work advances RND-321 independent QA closure by recording the migration-verification incident and the evidence required to safely rerun AC-8.

# RND-321 QA execution incident — 2026-08-05

## Status — updated after isolated AC-8 rerun

- Overall verdict: `PASS` for the local RND-321 code/migration acceptance scope.
- RND-321 business-code status: no new defect found; no Developer rework requested.
- AC-8: passed in a fresh disposable PostgreSQL database after the safety preflight below.
- Human WeCom QR scan: post-deployment human touchpoint; not part of this migration gate.

## AC-8 completion evidence

The rerun created the unique local database `wecom_archive_rnd321_ac8_c8aa017e3ca9_test` and passed every required preflight before any migration or seed write:

1. Direct `SELECT current_database()` returned exactly that unique database.
2. Alembic was launched with `DATABASE_URL` set to the same URL. A connection observer on Alembic's actual SQLAlchemy engine queried `current_database()` and observed only that same database.
3. The database began with no public tables. Any mismatch would have raised before `upgrade`, `downgrade`, or insert.

The harness then ran `0033 -> seed legacy access_requested admin_users row -> 0034 -> 0033`.

- The migration revisions observed were `0033`, then `0034`, then `0033`.
- A complete `SELECT *` snapshot of the seeded `admin_users` row was equal immediately after the 0034 upgrade and equal again after downgrade to 0033.
- `admin_login_identities` contained zero rows for the legacy account after upgrade, proving it was not auto-backfilled.
- The harness disposed its connections and deleted only the database it created.

AC-8 is now closed. The remainder of this report preserves the earlier incident record for audit and the separately identified tooling-safety follow-up.

## Incident and containment

1. QA first inspected the shared local `wecom_archive_test` database. It had one existing `tenants` row, so QA did not write to it.
2. QA created a uniquely named disposable local PostgreSQL database. The wrapper set Alembic `Config.sqlalchemy.url`, but did **not** set the process environment variable `DATABASE_URL`.
3. Alembic consequently connected to the local fallback database `wecom_archive`, not the disposable database, and upgraded it from revision `0005` to `0034`. That database already contained application data.
4. QA deleted only the database it had created. It did not downgrade, delete, or otherwise modify `wecom_archive` after discovering the mismatch.
5. Per Haisu's direction, local `wecom_archive` remains at `0034`. No QA agent may perform further writes or migrations against it without a new explicit instruction.

No source code, commit, push, deployment, secret, or external notification was created by this incident report. The migration of local `wecom_archive` is an out-of-worktree database side effect and must be handled separately by a human-approved recovery/retention decision.

## Classification

### Primary: QA execution incident

The immediate cause was QA command/environment usage: the disposable database URL was placed in Alembic `Config`, while this repository's migration environment takes its effective URL from `DATABASE_URL`. QA should have asserted the effective connection before running any migration.

### Secondary: repository tooling/safety gap

The repository does ignore an explicit Alembic config URL in its online migration path. Minimal, read-only reproduction evidence:

| Check | Evidence |
| --- | --- |
| Config URL is not consulted | `backend/alembic/env.py:22-31` defines `_database_url()` using only `DATABASE_URL`/`DB_*`; static AST check: `uses_config_main_option=False`. |
| Missing env falls back to development name | `backend/alembic/env.py:28` uses `DB_NAME` default `wecom_archive`; static AST check: `fallback_default_wecom_archive=True`. |
| Online migration overwrites config | `backend/alembic/env.py:45-52` assigns `cfg["sqlalchemy.url"] = _database_url()` before opening the engine; static AST check: `online_path_overwrites_sqlalchemy_url=True`. |

This proves the config-URL fallback behavior. It does **not** change the primary classification: the QA invocation omitted the repository-required `DATABASE_URL`. If Haisu chooses, this documented safety gap should become a separate tooling/safety ticket; it is not RND-321 Developer rework.

## Evidence already green

- `env -u DATABASE_URL ./.venv/bin/python -m pytest backend/tests/test_rnd286_user_admin.py backend/tests/test_users_page.py backend/tests/test_rnd321_qr_login.py -q` — `85 passed`.
- `env -u DATABASE_URL ./.venv/bin/python -m pytest backend/tests/test_architecture_boundary.py -q` — `25 passed`.
- `env -u DATABASE_URL BACKEND_PY=/Users/hzuo/Documents/code/wecom-archive-365/.venv/bin/python make verify` — exit 0, `2771 passed, 23 skipped`, with the approved Ruff 0.15.21 environment.
- Static review confirms migration 0034 does not update existing `admin_users`; its runtime remediation is owner-gated legacy identity release, not migration mutation.

The workspace's default Ruff 0.16.1 `make lint-diff` remains red on its broader rule set; that remains the separately scoped RND-342 upgrade matter, not a new RND-321 defect.

## Required preflight for the next AC-8 run

Use a fresh, uniquely named, disposable local PostgreSQL database. Before running `upgrade`, `downgrade`, or any seed insert, the harness must fail closed unless all three assertions pass:

1. A direct query on the test URL returns `current_database()` equal to this run's unique test database name.
2. The exact Alembic process is launched with `DATABASE_URL` set to that same URL, and a connection made through Alembic reports the same `current_database()`.
3. The name does not match, the URL is absent, or Alembic would select a fallback/default database: exit before any migration or write.

Only after those checks pass may the harness execute:

```text
0033 -> seed legacy access_requested admin_users row -> 0034 -> 0033
```

The final assertion must compare the pre-upgrade legacy `admin_users` row with its post-downgrade row and confirm equality, then delete only the database created by that harness. Do not use `wecom_archive` or the non-pristine shared `wecom_archive_test` database.
