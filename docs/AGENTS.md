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
| Haisu | Human owner | Final approval, merge, rule override |

---

## ChatGPT

**When invoked:** At issue creation and before any implementation starts.

**Responsibilities:**
- Read the issue and clarify requirements with Haisu if needed.
- Write the Devplan: approach, affected files, risks, acceptance criteria.
- Write QA checklist items in the issue or PR description.
- Review the final PR before Haisu merges.

**Handoff to Claude Code / Codex:**
- Post the approved Devplan in the issue or PR comment.

**Does not:**
- Write or commit implementation code.
- Decide on secrets or infrastructure without Haisu approval.
- Change Linear status unless Haisu explicitly asks

---

## Claude Code

**When invoked:** After Devplan is approved, to implement the plan.

**Responsibilities:**
- Read `DEV_AGENT_RULES.md` and the approved Devplan before starting.
- Create a branch following the naming convention.
- Implement exactly what the Devplan specifies — no scope creep.
- Run tests and linting before opening a PR.
- Write the QA Summary in the PR description.
- Flag blockers or out-of-scope discoveries immediately rather than working around them.

**Handoff to QA (self):**
- Complete the QA Summary block in the PR.
- Move PR from Draft to Ready for Review only when all QA checks pass.

**Handoff to ChatGPT:**
- Add a comment on the PR: "Ready for ChatGPT review."

**Does not:**
- Merge PRs.
- Change unrelated files.
- Commit secrets.
- Modify CI/CD pipelines without explicit approval.

---

## Codex

**When invoked:** For parallel subtasks, code search, or when Claude Code is unavailable.

**Responsibilities:**
- Same scope discipline as Claude Code.
- Preferred for: large-scale search-and-replace, boilerplate generation, test scaffolding.
- Must produce a QA Summary before any PR is opened.

**Coordination with Claude Code:**
- If both are active on the same issue, Claude Code is the primary. Codex outputs are reviewed by Claude Code before commit.
- Never open competing PRs for the same issue.

---

## Cline

**When invoked:** For short, targeted edits within an already-open Devplan scope.

**Responsibilities:**
- In-file edits guided by Claude Code or the Devplan.
- Does not create new files, new routes, or new modules unless listed in the Devplan.
- Defers any ambiguity to Claude Code.

**Does not:**
- Independently open PRs.
- Write Devplans.
- Modify configuration files.

---

## Haisu

**When invoked:** At Devplan approval and PR merge.

**Responsibilities:**
- Approves or rejects the Devplan before implementation starts.
- Performs final human review of every PR.
- The sole authority to merge into `main`.
- Can override any rule with explicit written justification.

**Does not:**
- Write implementation code in normal flow (only emergency hotfixes).
- Approve PRs without a complete QA Summary.

---

## Standard Handoff Sequence

```
Haisu opens issue
    │
ChatGPT writes Devplan
    │
Haisu approves Devplan
    │
Claude Code (or Codex) implements on feature branch
    │
Claude Code writes QA Summary → PR opened as Draft
    │
Claude Code moves PR to Ready → ChatGPT reviews
    │
ChatGPT approves
    │
Haisu merges → branch deleted
```

---

## Escalation Path

| Situation | Who to notify |
|-----------|--------------|
| Scope is unclear after reading Devplan | Claude Code → ChatGPT comment |
| Implementation reveals new risk not in plan | Claude Code → Haisu comment, pause work |
| Secret accidentally staged | Any agent → Haisu immediately, do not push |
| Merge conflict on `main` | Claude Code resolves, ChatGPT reviews diff |
| Test failure that cannot be fixed in scope | Claude Code → new issue, block current PR |

---

_Last updated: 2026-06-26 — RND-73_
