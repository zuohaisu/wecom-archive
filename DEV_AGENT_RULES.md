# DEV_AGENT_RULES v3 - 365 WeCom Archive

Binding working rules for every AI agent and human contributor on this project.
Deviation requires explicit approval from Haisu.

---

## Core Philosophy

This project uses AI agents as scoped contributors, not autonomous owners.

Core rules:

- One Linear Issue = One Implementation Conversation.
- Every implementation starts from an approved issue scope.
- Development is performed directly on the main branch by default.
- Feature branches are optional and should only be used for high-risk work or when Haisu explicitly requests them.
- Every agent must preserve project safety, traceability, and reviewability.
- The smallest correct change is preferred over broad refactors.
- The original implementing agent owns implementation fixes within the same issue.
- Haisu is Product Owner and final scope authority.

The default unit of work is the Linear issue. Do not mix multiple issues into one implementation conversation or one commit.

---

## Standard Workflow

```
Linear Issue
  |
  v
Planning (ChatGPT)
  |
  v
Implementation (Claude Code or Codex)
  |
  v
Codex QA
  |
  v
Commit
  |
  v
Push origin/main
```

Workflow rules:

- Start from a Linear issue before implementation.
- Keep the AI conversation tied to the issue being worked.
- Plan in ChatGPT when scope, architecture, or sequencing needs clarification.
- Implement on main by default.
- Use Claude Code as the primary implementation agent unless Haisu assigns Codex.
- Run Codex QA before committing.
- Use the original implementing agent for implementation fixes unless Haisu explicitly redirects the work.
- Do not expand scope during implementation without explicit approval.

---

## AI Conversation Strategy

ChatGPT is the only long-lived planning conversation across the whole project.

Rules:

- ChatGPT may maintain project-level planning context across issues.
- Claude Code, Codex, and Cline conversations should be issue-scoped and short-lived.
- Do not continue feature implementation across multiple unrelated agent conversations.
- If a new issue starts, start a new implementation conversation.
- If QA findings arrive for an issue, return to the original implementing agent conversation when practical.
- Do not use separate AI conversations to create hidden side scopes.

The goal is to preserve traceability: one issue, one implementation conversation, one focused implementation.

---

## Agent Responsibilities

### ChatGPT

Primary role: long-lived planning, architecture framing, product reasoning, and review support.

Responsibilities:

- Own project-level planning context.
- Help shape Linear issue scope before implementation.
- Clarify acceptance criteria and trade-offs.
- Identify risks, dependencies, and sequencing.
- Review plans and implementation summaries when useful.
- Avoid writing large implementation patches directly unless Haisu explicitly asks.

ChatGPT is the planning memory across the project, not the default code implementer.

### Claude Code

Primary role: main implementation agent.

Responsibilities:

- Implement approved Linear issue scope.
- Work directly on main by default.
- Do not create branches unless Haisu explicitly requests them.
- Keep changes focused and reviewable.
- Run relevant tests, linting, and formatting checks.
- Handle implementation fixes for its own work.
- Document QA results before commit.

Claude Code should not expand product scope or architecture direction without approval.

### Codex

Primary role: primary code reviewer, repository inspection, QA, and focused engineering support.

Responsibilities:

- Review implementation before commit.
- Implement approved issue scope when assigned.
- Inspect the repo before changing files.
- Keep edits limited to the requested files and behavior.
- Run requested verification commands.
- Handle implementation fixes for its own work.
- Report files changed, tests run, risks, and remaining gaps.

Codex must not commit, push, or modify unrelated files unless Haisu explicitly asks.

### Cline

Primary role: debugging, environment research, local inspection, and small targeted edits.

Responsibilities:

- Investigate errors, stack traces, environment issues, and repo behavior.
- Produce concise findings and recommended fixes.
- Make small scoped edits only when clearly approved.
- Avoid driving large feature implementation.
- Defer architecture, product scope, and multi-file feature work to ChatGPT plus Claude Code or Codex.

Cline is primarily a research/debug agent, not the owner of large feature delivery.

### Haisu

Primary role: Product Owner and final scope authority.

Responsibilities:

- Own product direction and issue priority.
- Approve scope changes.
- Decide which agent should handle each task.
- Review final implementation behavior and risks.
- Approve commits and pushes when needed.
- Override these rules when needed.

Haisu is the final product owner and scope authority.

---

## Default Agent Selection

| Task type | Default agent | Notes |
|---|---|---|
| Project planning | ChatGPT | Long-lived planning context across the project |
| Product or architecture framing | ChatGPT | Use before implementation when scope is unclear |
| Main feature implementation | Claude Code | Work directly on main by default |
| Focused code implementation | Codex | Best for scoped repo edits and verification |
| QA and code review | Codex | Primary reviewer before commit |
| Implementation fixes | Original implementing agent | Keep fixes in the same issue conversation |
| Bug investigation | Cline or Codex | Cline for research/debug; Codex when code changes are likely |
| Large feature implementation | Claude Code or Codex | Cline should not lead this work |
| Final scope decision | Haisu | Haisu is final authority |

---

## Git Workflow

Default workflow:

```
Linear Issue
  |
  v
Implement on main
  |
  v
Codex QA
  |
  v
Commit
  |
  v
Push origin/main
```

Rules:

- Development happens directly on main by default.
- Keep each implementation tied to one Linear issue.
- Run Codex QA before committing.
- Do not push `origin/main` unless Haisu explicitly asks.
- Do not create feature branches unless the work qualifies below or Haisu explicitly requests it.

Feature branches should only be used for:

- Destructive database migrations.
- Deployment changes.
- Authentication/security work.
- Major refactors.
- When explicitly requested by Haisu.

---

## Secrets and Sensitive Data

Never commit:

- API keys
- Tokens
- Passwords
- Connection strings
- Private keys
- Service account files
- `.env` files
- Real user data
- Production chat exports
- Production media
- WeCom Corp Secret or App Secret values

Rules:

- Use `.env` for local secrets.
- Keep `.env` ignored by git.
- Use `.env.example` only for placeholder values.
- Read secrets from environment variables.
- Do not hardcode secrets in code, docs, tests, or scripts.

If a secret is accidentally committed:

1. Stop work.
2. Notify Haisu immediately.
3. Do not amend, force-push, or rewrite history without explicit approval.
4. Rotate the exposed secret before relying on it again.

---

## Commit Rules

Do not commit unless Haisu explicitly asks.

When commits are approved, use this format:

```
<type>(<scope>): <imperative description>

[optional body: what changed and why]

[optional footer: issue reference]
```

Allowed types:

- `feat`
- `fix`
- `docs`
- `refactor`
- `test`
- `chore`
- `ci`

Rules:

- Use imperative mood.
- Keep the subject line concise.
- Reference the Linear issue in the commit when practical.
- Do not create WIP commits.
- Do not include unrelated changes in a commit.

---

## QA Rules

Every implementation should include a QA summary before commit.

QA summary should cover:

- Files changed.
- Acceptance criteria checked.
- Tests run.
- Linting or formatting checks run.
- Manual verification performed.
- Known risks or gaps.
- Confirmation that no secrets were introduced.
- Confirmation that only intentional files changed.

Suggested QA block:

```
## QA Summary

Files changed:
- <file>: <what changed>

Acceptance criteria:
- [ ] <criterion>: pass / fail / n/a

Commands run:
- <command>: <result>

Manual verification:
- <check>: <result>

Risks or gaps:
- <risk or none>

No secrets introduced: confirmed
Only intentional files changed: confirmed
```

If QA fails, do not commit until the issue is fixed or Haisu explicitly accepts the risk.

---

## Out of Scope Without Explicit Approval

AI agents must not do the following without explicit Haisu approval:

- Commit changes.
- Push `origin/main`.
- Force-push.
- Rewrite git history.
- Modify CI/CD configuration.
- Change deployment settings.
- Modify `.gitignore` to allow secret or generated files.
- Drop, truncate, or migrate databases.
- Send messages or notifications to external systems.
- Access or export production data.
- Add large dependencies.
- Perform broad refactors outside the issue.
- Create new product scope beyond the Linear issue.
- Drive large feature implementation through Cline.

---

## Decision Priority

When rules conflict, use this priority order:

1. Safety and secrets protection.
2. Haisu's explicit instruction.
3. Linear issue scope.
4. Existing project architecture and conventions.
5. Smallest reviewable change.
6. Agent-specific responsibilities in this document.
7. Speed.

If the correct action is unclear, stop and ask Haisu before changing files.

---

_Last updated: 2026-06-27 - RND-73_
