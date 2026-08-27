# DEV_AGENT_RULES — merged into AGENTS.md

**This file is a tombstone.** As of 2026-08-28 (v6) all binding working rules live
in the repository-root [`AGENTS.md`](AGENTS.md), together with the agent roster
that used to live in `docs/AGENTS.md`.

Historical ticket prompts under `tasks/` instruct agents to "read
`DEV_AGENT_RULES.md`". Read [`AGENTS.md`](AGENTS.md) instead — it is the same
rule set, de-duplicated, with roles named by function (dev / qa / ops / research)
and GitHub Issues in place of Linear.

Section anchors moved as follows:

| Old | New |
|---|---|
| `#core-philosophy` | [`#core-invariants`](AGENTS.md#core-invariants) |
| `#standard-workflow`, `#git-workflow` | [`#workflow`](AGENTS.md#workflow), [`#git-workflow`](AGENTS.md#git-workflow) |
| `#agent-responsibilities`, `#default-agent-selection` | [`#roles`](AGENTS.md#roles) |
| `#main-direct-commit-exception` | [`#main-direct-commit-exception`](AGENTS.md#main-direct-commit-exception) |
| `#git-operation-boundaries` | [`#git-operation-boundaries`](AGENTS.md#git-operation-boundaries) |
| `#commit-rules` | [`#commit-rules`](AGENTS.md#commit-rules) |
| `#qa-rules` | [`#validation-and-qa`](AGENTS.md#validation-and-qa) |
| `#architecture-boundaries` | [`#architecture-boundaries`](AGENTS.md#architecture-boundaries) |
| `#secrets-sensitive-data-and-untrusted-input` | [`#secrets-sensitive-data-and-untrusted-input`](AGENTS.md#secrets-sensitive-data-and-untrusted-input) |
| `#out-of-scope-without-explicit-approval` | [`#out-of-scope-without-explicit-approval`](AGENTS.md#out-of-scope-without-explicit-approval) |
| `#decision-priority` | [`#decision-priority`](AGENTS.md#decision-priority) |
