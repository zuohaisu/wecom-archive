# Ruff 0.16.1 migration plan

## Current baseline

The repository baseline is **Ruff 0.15.21**, pinned as
`ruff==0.15.21` in [`backend/requirements.txt`](../../backend/requirements.txt).
The root Makefile invokes Ruff through the repository virtual environment
(`.venv/bin/python -m ruff`), and CI installs that same requirements file.

This pin preserves the existing lint semantics while Ruff 0.16's changed
implicit defaults are assessed. Ruff 0.16+ is not an upgrade performed by
RND-342: the purpose of this pin is reproducible local and CI results, not a
permanent refusal to upgrade.

The first evaluation target is **Ruff 0.16.1**. It is a comparison and
migration target only; it must not become the version used by the current
quality gate until the phases below are accepted.

## Explicit rule strategy before upgrading

The current `pyproject.toml` deliberately has no `lint.select`, so Ruff
0.15.21's effective default rule families remain implicit. Before switching
the canonical version, record an explicit selection that reproduces that
baseline: `lint.select = ["E4", "E7", "E9", "F"]`, while retaining the
existing test-only F811 per-file ignore. Confirm that selection against the
0.15.21 diagnostic baseline before merging it.

For the initial 0.16.1 switch, no newly-defaulted rule family is approved for
enablement. The observed new families (`B`, `BLE`, `C4`, `EXE`, `FA`, `FLY`,
`FURB`, `I`, `ISC`, `PIE`, `PLR`, `PLW`, `PYI`, `RET`, `RUF`, `S`, `SIM`,
`TRY`, and `UP`) are deferred until each has a separately reviewed baseline
and acceptance decision. This is an explicit selection policy, not a plan to
add ignores. Individual rules may be enabled later in small, reviewed changes
when their diagnostics are understood. Depending on implicit defaults across
Ruff releases is not acceptable because it makes an unchanged checkout lint
differently after a tool upgrade.

This document does **not** apply that `lint.select` change now; the active
0.15.21 rule behavior remains unchanged by RND-342.

## Phased migration

1. In an isolated environment, run Ruff 0.16.1 against exactly the current
   lint targets and collect a machine-readable diagnostic baseline.
2. Group the results by rule code and code area (application, scripts, and
   tests).
3. Classify every group as a real defect, mechanical repair, disputed rule, or
   probable false positive.
4. Create independent tickets or small reviewable batches for accepted fixes;
   do not combine them with the tool-version change.
5. Run the candidate configuration and Ruff 0.16.1 in non-blocking CI or a
   comparable pre-merge check until the selected policy and cleanup batches
   are accepted.
6. After accepted cleanup and an explicit-rule baseline, change the canonical
   Ruff pin from 0.15.21 to the approved exact 0.16.1 version.
7. Update local bootstrap documentation and CI together, then confirm both
   execute the same binary version and run the complete quality gate.

## Rollback

If the upgrade does not meet its acceptance criteria:

1. Restore `ruff==0.15.21` in the canonical requirements source.
2. Restore the explicit rule configuration that was in effect immediately
   before the upgrade (or remove the new explicit selection if the upgrade
   introduced it).
3. Keep independent code fixes that have already been reviewed and proven safe;
   do not discard them merely to roll the tool version back.
4. Reinstall the canonical requirements and run the full quality gate to prove
   the restored local and CI configuration is healthy.

## Non-goals

This plan does not authorize:

- a large current-ticket rewrite of existing application or test code;
- adding ignores for every newly reported diagnostic; or
- upgrading the blocking CI version without a diagnostic baseline, independent
  cleanup acceptance, and a successful non-blocking evaluation.
