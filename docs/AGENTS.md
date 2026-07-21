# AGENTS — 365 WeCom Archive

Agent roster, responsibilities, and handoff protocol.
Full working rules are in [DEV_AGENT_RULES.md](../DEV_AGENT_RULES.md).

---

## Agent Roster

| Agent | Role | Primary Tools |
|-------|------|---------------|
| ChatGPT | PM / Architect / Reviewer | Web browsing, code review, document generation |
| Claude Code | Primary implementer | File read/write, Bash, git, test runner |
| Codex | Secondary implementer / search | Code generation, grep/search, parallel subtasks |
| Cline | IDE-embedded agent | Targeted in-file edits, autocomplete |
| Haisu | Human owner | Final approval, commit/push to main, rule override |

---

## ChatGPT

**When invoked:** At issue creation and before any implementation starts.

**Responsibilities:**
- Read the issue and clarify requirements with Haisu if needed.
- Write the Devplan: approach, affected files, risks, acceptance criteria.
- Write QA checklist items in the issue description.
- Review the final change before Haisu approves the commit.


**Handoff to Claude Code / Codex:**
- Post the approved Devplan as an issue comment.

**Does not:**
- Write or commit implementation code.
- Decide on secrets or infrastructure without Haisu approval.
- Change Linear status unless Haisu explicitly asks

---

## Claude Code

**When invoked:** After Devplan is approved, to implement the plan.

**Responsibilities:**
- Read `DEV_AGENT_RULES.md` and the approved Devplan before starting.
- Work directly on `main` by default; do not create task branches.
- Implement exactly what the Devplan specifies — no scope creep.
- Run tests and linting before requesting commit approval.
- Write the QA Summary in the issue (or commit message when Haisu approves a commit).
- Flag blockers or out-of-scope discoveries immediately rather than working around them.

**Handoff to QA (self):**
- Complete the QA Summary block in the issue.
- Mark the work ready for review only when all QA checks pass.

**Handoff to ChatGPT:**
- Add a comment on the issue: "Ready for ChatGPT review."

**Does not:**
- Push to `main` without Haisu approval.
- Change unrelated files.
- Commit secrets.
- Modify CI/CD pipelines without explicit approval.

---

## Codex

**When invoked:** For parallel subtasks, code search, or when Claude Code is unavailable.

**Responsibilities:**
- Same scope discipline as Claude Code.
- Preferred for: large-scale search-and-replace, boilerplate generation, test scaffolding.
- Must produce a QA Summary before commit.

**Coordination with Claude Code:**
- If both are active on the same issue, Claude Code is the primary. Codex outputs are reviewed by Claude Code before commit.
- Never push competing changes for the same issue without coordinating with Claude Code.

---

## Cline

**When invoked:** For short, targeted edits within an already-open Devplan scope.

**Responsibilities:**
- In-file edits guided by Claude Code or the Devplan.
- Does not create new files, new routes, or new modules unless listed in the Devplan.
- Defers any ambiguity to Claude Code.

**Does not:**
- Independently commit or push.
- Write Devplans.
- Modify configuration files.

---

## Haisu

**When invoked:** At Devplan approval and commit approval.

**Responsibilities:**
- Approves or rejects the Devplan before implementation starts.
- Performs final human review of every change.
- The sole authority to approve commits and pushes to `main`.
- Can override any rule with explicit written justification.

**Does not:**
- Write implementation code in normal flow (only emergency hotfixes).
- Approve commits without a complete QA Summary.

---

## Standard Handoff Sequence

```
Haisu opens issue
    │
ChatGPT writes Devplan
    │
Haisu approves Devplan
    │
Claude Code (or Codex) implements directly on main
    │
Claude Code writes QA Summary
    │
ChatGPT reviews the change
    │
ChatGPT approves
    │
Haisu approves commit/push to main
```

---

## Escalation Path

| Situation | Who to notify |
|-----------|--------------|
| Scope is unclear after reading Devplan | Claude Code → ChatGPT comment |
| Implementation reveals new risk not in plan | Claude Code → Haisu comment, pause work |
| Secret accidentally staged | Any agent → Haisu immediately, do not push |
| Merge conflict on `main` | Claude Code resolves, ChatGPT reviews diff |
| Test failure that cannot be fixed in scope | Claude Code → new issue, block current change |

---

_Last updated: 2026-06-26 — RND-73_
