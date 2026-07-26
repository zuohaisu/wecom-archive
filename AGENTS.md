# AGENTS

This file is an entry point for agent runtimes that auto-load a repo-root
`AGENTS.md`. The actual rules live elsewhere; start here and follow the
links below rather than duplicating content into this file.

- **Working rules (binding):** [DEV_AGENT_RULES.md](DEV_AGENT_RULES.md) —
  workflow, git rules, secrets, commit rules, and
  [Architecture Boundaries](DEV_AGENT_RULES.md#architecture-boundaries)
  (dependency direction, composition-root purity — read this before adding
  a route or a new module under `backend/app/`).
- **Agent roster / handoff protocol:** [docs/AGENTS.md](docs/AGENTS.md) —
  who does what, when to hand off, escalation path.
- **Automated architecture guardrail:**
  [backend/tests/test_architecture_boundary.py](backend/tests/test_architecture_boundary.py)
  — enforces the rules above under `make test` / CI. Treat a failure here
  as a hard stop, not something to work around.
