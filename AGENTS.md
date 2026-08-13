# AGENTS

This file is an entry point for agent runtimes that auto-load a repo-root
`AGENTS.md`. The actual rules live elsewhere; start here and follow the
links below rather than duplicating content into this file.

- **Working rules (binding):** [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) —
  PR-first workflow, worktree/branch rules, git rules, secrets, commit rules, and
  [Architecture Boundaries](DEV_AGENT_RULES.md#architecture-boundaries)
  (dependency direction, composition-root purity — read this before adding
  a route or a new module under `backend/app/`).
- **Delivery invariant:** all changes reach `main` through a pull request whose
  required CI has passed. Never develop on or push directly to `main`; the merge
  of deployable paths to `main` is what triggers CD.
- **Traceability invariant:** every Linear issue has exactly one final commit.
  A non-`main` worktree/branch/PR may contain several related issue commits
  (normally from the same Epic), but one commit must never mix multiple issues.
- **Agent roster / handoff protocol:** [docs/AGENTS.md](docs/AGENTS.md) —
  who does what, when to hand off, escalation path.
- **Automated architecture guardrail:**
  [backend/tests/test_architecture_boundary.py](backend/tests/test_architecture_boundary.py)
  — enforces the rules above under `make test` / CI. Treat a failure here
  as a hard stop, not something to work around.
