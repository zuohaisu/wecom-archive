# DEV_AGENT_RULES — 365 WeCom Archive

> Binding rules for every AI agent and human contributor on this project.
> Deviation requires explicit sign-off from Haisu.

---

## 1. Core Workflow: Devplan → Develop → QA

```
Issue created (Linear / GitHub)
  │
  ▼
[Devplan] — ChatGPT writes a short plan (approach, files, risks)
  │         Haisu reviews and approves before any code is written
  ▼
[Develop] — Claude Code / Codex / Cline implements exactly what the plan says
  │          One issue = one branch = one focused PR
  ▼
[QA]      — Claude Code or Codex verifies the change against acceptance criteria
              Haisu performs final human review before merge
```

**Rules:**
- No agent may skip the Devplan step.
- No agent may implement anything not covered in the approved Devplan.
- QA must be documented (see Section 7) before a PR is opened for human review.

---

## 2. Branch and PR Rules

| Rule | Detail |
|------|--------|
| One issue = one branch | Never mix unrelated changes |
| Branch naming | `feat/<issue-id>-short-slug`, `fix/<issue-id>-short-slug`, `docs/<issue-id>-short-slug` |
| PR title | `[<issue-id>] <imperative verb> <what>` — e.g. `[RND-73] Add agent working rules` |
| PR size | Aim for < 400 lines diff. Split larger changes into sequential issues. |
| Draft PRs | Open as Draft until QA is complete |

---

## 3. Role Definitions

### ChatGPT — PM / Architect / Reviewer
- Owns the Devplan for every issue.
- Writes acceptance criteria and QA checklist.
- Reviews PR before Haisu merges.
- Does **not** write implementation code.

### Claude Code — Primary implementer
- Reads the approved Devplan before touching any file.
- Implements only what is in scope.
- Writes the QA summary after implementation (see Section 7).
- Runs tests and linting before opening a PR.

### Codex — Secondary implementer / code search
- Used when Claude Code is unavailable or for parallel subtasks.
- Same scope discipline as Claude Code.
- Must not open PRs without a QA summary.

### Cline — IDE-embedded agent
- Used for short, targeted edits within an approved Devplan.
- Must not create new files outside the plan.
- Defers architecture decisions to ChatGPT.

### Haisu — Human owner
- Approves Devplans before implementation starts.
- Performs final review and merges PRs.
- The only person who can override these rules.
- Does not write implementation code except for emergency hotfixes.

---

## 4. Secrets and Sensitive Data

### Never commit:
- API keys, tokens, passwords, connection strings
- `.env` files (`.env`, `.env.local`, `.env.production`, etc.)
- Private keys (`*.pem`, `*.key`, `*.p12`, `*.pfx`)
- Service account JSON files (`*-service-account.json`, `credentials.json`)
- WeCom Corp Secret or App Secret values
- Any real user data, chat content, or media from production

### Required setup:
- All secrets go in `.env` (gitignored).
- Use `.env.example` with placeholder values to document required variables.
- Code reads secrets from environment variables only — never hardcoded.

### If a secret is accidentally committed:
1. Do not amend/force-push without Haisu's explicit approval.
2. Notify Haisu immediately.
3. Rotate the secret before doing anything else.

---

## 5. Commit Format

```
<type>(<scope>): <imperative description>

[optional body — what and why, not how]

[optional footer — issue ref, breaking change]
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`

**Examples:**
```
feat(archive): add WeCom message ingestion endpoint
fix(auth): handle expired token refresh correctly
docs(agents): add DEV_AGENT_RULES and AGENTS.md
chore(deps): pin wecom-sdk to 1.2.3
```

**Rules:**
- Subject line ≤ 72 characters, imperative mood, no trailing period.
- Reference the issue: `Refs RND-73` or `Closes RND-73` in the footer.
- No "WIP" commits on `main`. Squash before merge if needed.

---

## 6. What Must Never Be Committed

In addition to secrets (Section 4):

- Auto-generated files that belong in `.gitignore` (build artifacts, caches, `__pycache__/`, `.venv/`, etc.)
- Large binary files > 5 MB (use cloud storage instead)
- Commented-out code blocks left as "just in case"
- Debugging print statements / `console.log` not behind a debug flag
- Incomplete features without a feature flag
- Direct database dumps or exports

---

## 7. QA Summary Format

Every PR description must include a QA block:

```markdown
## QA Summary

**Files changed:** list each file and what changed
**Acceptance criteria checked:**
- [ ] criterion 1 — pass / fail / n/a
- [ ] criterion 2 — pass / fail / n/a
**Tests run:** `<command>` — result
**Linting:** `<command>` — result
**No secrets committed:** confirmed
**git status clean (only intentional changes):** confirmed
```

If any criterion fails, the PR stays in Draft until fixed.

---

## 8. Out of Scope for AI Agents (Without Explicit Haisu Approval)

- Changing CI/CD pipeline configuration
- Modifying `.gitignore` to un-ignore secret files
- Force-pushing to `main`
- Dropping or truncating database tables
- Sending messages or notifications to external systems (Slack, email, WeCom)
- Merging PRs
- Creating or deleting GitHub branches other than the agent's own working branch

---

_Last updated: 2026-06-26 — RND-73_
