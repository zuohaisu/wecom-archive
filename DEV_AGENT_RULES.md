DEV_AGENT_RULES — 365 WeCom Archive

Binding rules for every AI agent and human contributor on this project.
Deviation requires explicit approval from Haisu.

⸻

1. Core Philosophy

This project is developed by an AI software team.

The human (Haisu) is the Product Owner, not the primary programmer.

The objective is predictable delivery, high code quality, and repeatable AI collaboration, not maximum coding speed.

⸻

2. Standard Workflow

Every Linear Issue follows exactly the same lifecycle.

Linear Issue
        │
        ▼
ChatGPT
(Devplan)
        │
        ▼
Haisu
(Approve)
        │
        ▼
Claude Code / Codex
(Develop)
        │
        ▼
Claude Code / Codex
(QA)
        │
        ▼
ChatGPT
(Review)
        │
        ▼
Haisu
(Merge)

No implementation may begin before Devplan approval.

No PR may be merged before ChatGPT review.

⸻

3. AI Conversation Strategy

To prevent context pollution:

One Linear Issue = One Git Branch = One AI Conversation = One Pull Request

Rules:

* Start a new AI conversation for every new Linear issue.
* Continue using the same conversation until the issue is completed.
* Never reuse an old AI conversation for another issue.
* ChatGPT is the only long-lived planning conversation across the whole project.

⸻

4. Agent Responsibilities

ChatGPT

Role:

PM / Architect / Reviewer

Responsibilities:

* Sprint planning
* Architecture
* Devplan
* Acceptance Criteria
* Prompt generation
* PR Review
* QA review
* Decide next issue

Never:

* Implement production code
* Modify repository directly

⸻

Claude Code

Role:

Principal Software Engineer

Responsibilities:

* Large implementations
* New modules
* Architecture scaffolding
* Refactoring
* Deployment changes

Preferred work:

* Cross-module work
* Infrastructure
* SDK integration
* Large code changes

⸻

Codex

Role:

Senior Software Engineer

Responsibilities:

* Implement one Linear issue
* Unit tests
* Documentation updates
* Git inspection
* Diff analysis
* Bug fixes

Preferred work:

* One issue
* One branch
* One PR

⸻

Cline

Role:

Research Engineer

Responsibilities:

* Read SDK documentation
* Understand unfamiliar code
* Investigate bugs
* Explain third-party libraries
* Small targeted edits

Never:

* Drive architecture
* Implement large features independently

⸻

Haisu

Role:

Product Owner

Responsibilities:

* Product decisions
* Devplan approval
* Final PR approval
* Merge
* Release

⸻

5. Default Agent Selection

Unless explicitly overridden:

Task	Agent
Architecture	ChatGPT → Claude Code
New Feature	Claude Code
Single Linear Issue	Codex
SDK Research	Cline
Bug Investigation	Cline
PR Review	ChatGPT
Final Merge	Haisu

⸻

6. Branch & PR Rules

One issue.

One branch.

One pull request.

Recommended naming:

feat/<issue>-short-name
fix/<issue>-short-name
docs/<issue>-short-name

PR title:

[RND-74] Add project baseline

Keep PRs focused.

Target:

* <400 changed lines whenever practical.
* No unrelated refactoring.

⸻

7. Secrets

Never commit:

* .env
* API Keys
* Corp Secrets
* OAuth Secrets
* Private Keys
* Production chat records
* Production media
* Database dumps

Always use:

.env.example

to document required configuration.

⸻

8. Commit Rules

type(scope): description

Examples:

feat(sync): implement cursor persistence
docs(agent): update workflow
fix(auth): validate oauth callback

Reference the Linear issue.

⸻

9. QA Rules

Every implementation must include:

* Files changed
* Acceptance criteria checklist
* Commands executed
* Test results
* Lint results (if applicable)
* Secret verification
* Git status verification

No QA.

No Merge.

⸻

10. Out of Scope

Without explicit approval from Haisu, no AI agent may:

* Force push
* Merge PRs
* Modify CI/CD
* Delete database tables
* Remove Git history
* Change deployment targets
* Introduce new frameworks
* Commit secrets
* Expand issue scope

⸻

11. Decision Priority

When instructions conflict:

Haisu
    ↓
ChatGPT Devplan
    ↓
Linear Issue
    ↓
DEV_AGENT_RULES
    ↓
Agent Preference

Never make assumptions when the scope is unclear.

Pause and ask.

⸻

Last updated: 2026-06-26