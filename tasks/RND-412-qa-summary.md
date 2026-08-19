# RND-412 QA Summary — CI-wide optimization pass (beyond ffmpeg/timeout)

Scope note: the ffmpeg-install-hang hotfix (static-binary install + job-level
`timeout-minutes: 20`) is owned by a separate concurrent session on this same
branch and is intentionally untouched here, per explicit user instruction.
This summary covers a broader CI audit and the one additional change made as
a result.

## Files changed

- `.github/workflows/test.yml`: added `cache: "pip"` +
  `cache-dependency-path: backend/requirements.txt, backend/requirements-dev.txt`
  to the existing `Set up Python 3.11` (`actions/setup-python@v5`) step. No
  other lines touched.

## Audit findings (informational — most are explicitly deferred, not bugs)

1. **No caching anywhere in any workflow** (pip, apt, node) before this
   change — confirmed via repo-wide `grep -rn "actions/cache"` (zero hits)
   and inspection of both `setup-python`/`setup-node` steps. This is the one
   finding acted on this round.
2. **Job parallelization (RND-412's P0-c)** is real and valuable but
   deliberately **not implemented this round**: splitting into
   `lint`/`offline-tests`/`postgres-tests` jobs requires relocating the
   "Install ffmpeg" step into whichever job runs `pytest tests/`, which
   sits inside the file region the other concurrent session is actively
   editing (observed one live concurrent edit mid-session: a `curl
   --max-time` value changed from 90→120 while this session had the file
   open). Deferred to a follow-up once that hotfix lands, to avoid
   clobbering in-flight work.
3. **`pytest-xdist` (RND-412's P1) audited and confirmed unsafe to enable
   yet** — this matches the ticket's own stated precondition ("需要先审计...
   不能直接开"). Concrete hazards found:
   - 7 PostgreSQL test files (`test_revoke_association_migration.py`,
     `test_media_storage_backend_migration.py`,
     `test_message_revocations_integrity_migration.py`,
     `test_message_revocations_tenant_integrity.py`,
     `test_check_message_revocations_integrity.py`,
     `test_verify_alembic_head.py`, `test_readiness_health_endpoint.py`,
     `test_revoke_concurrency.py`) run `DROP DATABASE`/`CREATE DATABASE`
     against one shared admin connection — `test_revoke_concurrency.py`'s
     own comments already flag this pattern as fragile under concurrent
     connections.
   - `test_tenant_foundation.py` writes to the shared, non-scratch
     `wecom_test` database using a fixed constant ID.
   - `test_rnd310_tenant_activation.py:16-17` uses a module-scoped,
     non-`tmp_path` SQLite engine shared mutably across that file's tests.
   - The duplicated `_postgres_reachable()` skip-decision (7 files) opens a
     live network connection at collection time; xdist requires identical
     collection across workers, which this pattern puts at risk under
     concurrency.
   - `pytest-xdist` isn't declared as a dependency yet either way.
   No code changes made for this item — audit-only, as the ticket requires.
4. **Minor, out-of-scope observations, recorded but not acted on**:
   - `backend/test_local.db` (12KB, tracked in git) has zero references
     anywhere under `backend/tests/` — looks like a stray leftover, unrelated
     to CI speed; left alone pending a separate decision.
   - `test.yml` and the root `Makefile` (`make verify`/`make test`) are two
     independently-maintained "how to validate this repo" paths that have
     already drifted (CI doesn't use a venv or the Makefile's
     escalating-timeout pytest wrapper; `ruff check` runs only via the
     Makefile, never in CI). Converging them is a behavior change outside a
     CI-speed ticket's scope, and the ticket explicitly requires `make
     verify` semantics stay unchanged — noted only, not touched.

## Acceptance criteria (ticket-level)

- [ ] PR-triggered CI critical path shows a measurable, reproducible drop
      vs. baseline, with real before/after Actions run links — **not yet
      verifiable locally**; requires a real PR run. Not claimed as done.
- [x] No existing test coverage or safety gates removed or skipped — this
      change only adds a cache key to an install step; no gate logic touched.
- [x] No P0-a (ffmpeg-out-of-blocking-path) was adopted, so no substitute
      mechanism is required — N/A, ffmpeg stays in the blocking job per the
      other session's P0-b approach.
- [x] No required-check/branch-protection changes needed — no job structure
      was changed this round (repo also currently has no enforced branch
      protection per `DEV_AGENT_RULES.md`'s enforcement note).
- [x] Deploy flow untouched — `deploy.yml`/`deploy-nonprod.yml` not modified.
- [x] `make verify` semantics unchanged — the Makefile was not touched.

## Commands run

- `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/test.yml'))"` — passed, YAML parses; also spot-checked the parsed `with:` block for the new step to confirm `cache`/`cache-dependency-path` resolved to the expected values.
- `git diff .github/workflows/test.yml` — confirmed the diff is scoped to the `Set up Python 3.11` step only; no overlap with the concurrently-edited ffmpeg/bats/timeout region.

No `actionlint` binary was available in this environment to lint further;
not installed as part of this change (would be an unrelated dependency add).

## Manual verification

- Cross-checked the `test_ffmpeg_converts_amr_fixture_to_mp3` skip logic
  referenced in the other session's in-progress diff against
  `backend/tests/test_voice_transcode.py` directly — confirmed accurate.
- Could not trigger a real GitHub Actions run from this environment to
  observe an actual cache hit; this is called out below as unverified rather
  than claimed.

## Risks or gaps

- Pip cache effectiveness (actual install-time reduction) is unverified
  locally — needs confirmation from a real Actions run once this branch is
  pushed. First run after this change will populate the cache cold (no
  speedup); the speedup shows up starting the second run with unchanged
  `requirements*.txt`.
- P0-c and P1 remain open follow-up work, deliberately deferred per the
  reasoning above — not implemented, not claimed as done.
- This branch still has two sets of uncommitted changes interleaved in the
  same file from two different sessions (this one's pip-cache addition, and
  the other session's ffmpeg/timeout hotfix). Recommend the other session's
  work lands as (or alongside) this commit, or coordinate ordering, before
  either is committed — flagging this explicitly rather than committing
  unilaterally.

No secrets introduced: confirmed.

Only intentional files changed: confirmed (`git status` shows only
`.github/workflows/test.yml` modified; this QA file and its dev-prompt
counterpart are new, expected `tasks/` artifacts).
