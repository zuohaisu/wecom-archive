# Onboarding Guide — Crowntime WeCom Archive

## Recommended Reading Order

If you are a new AI agent (or human developer) encountering this project for the first time, read the following in order:

### Step 1: Project Overview

**Read:** [docs/ai/project-overview.md](project-overview.md) (5 min)

Understand what this project is, what it does, and whether it's relevant to your task.

### Step 2: Architecture

**Read:** [docs/ARCHITECTURE.md](../ARCHITECTURE.md) (15 min)

Understand the system components, data flow, auth model, and deployment. This is the most important document for avoiding mistakes.

### Step 3: Current Status

**Read:** [docs/ai/current-status.md](current-status.md) (5 min)

Know what's done, what's in progress, and what's planned. Prevents duplicating work or building something that's already implemented.

### Step 4: Business Terms

**Read:** [docs/ai/business-terms.md](business-terms.md) (5 min)

Learn the domain terminology (Tenant, Corp, Archive, Conversation, Monitored Account, etc.). Prevents confusion about what different terms mean.

### Step 5: Working Rules

**Read:** [DEV_AGENT_RULES.md](../../DEV_AGENT_RULES.md) (10 min)

Mandatory reading for all AI agents. Covers workflow, git rules, secrets policy, QA requirements, and agent responsibilities.

### Step 6: Data Model

**Read:** [docs/DATA_MODEL.md](../DATA_MODEL.md) (10 min if needed)

Skip unless your task involves database queries, migrations, or data analysis. Read on demand.

### Step 7: API Surface

**Read:** [docs/API.md](../API.md) (10 min if your task touches routes)

Read this before changing auth, review-console APIs, search endpoints, or the
WeCom callback surface.

### Step 8: Deployment Boundary

**Read:** [docs/DEPLOYMENT.md](../DEPLOYMENT.md) (5 min if your task touches ops)

This clarifies which deploy assets are versioned here and which are
operator-managed outside the repo.

### Step 9: Agent Roster

**Read:** [docs/AGENTS.md](../AGENTS.md) (5 min)

Understand which AI agents are involved and their handoff protocol.

### Step 10: Known Pitfalls

**Read:** [docs/ai/known-pitfalls.md](known-pitfalls.md) (5 min)

Learn about common traps and mistakes to avoid.

---

## Quick Navigation

| Need | Document |
|------|----------|
| "What is this project?" | [project-overview.md](project-overview.md) |
| "How does the system work?" | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| "What's implemented?" | [current-status.md](current-status.md) |
| "What terms mean what?" | [business-terms.md](business-terms.md) |
| "What are my rules?" | [DEV_AGENT_RULES.md](../../DEV_AGENT_RULES.md) |
| "What does the DB look like?" | [DATA_MODEL.md](../DATA_MODEL.md) |
| "What routes exist?" | [API.md](../API.md) |
| "What deploy assets are actually in repo?" | [DEPLOYMENT.md](../DEPLOYMENT.md) |
| "Who else is working on this?" | [AGENTS.md](../AGENTS.md) |
| "What breaks easily?" | [known-pitfalls.md](known-pitfalls.md) |
| "How to deploy/run the worker?" | [wecom_archive_worker_runbook.md](../wecom_archive_worker_runbook.md) |
| "What env vars do I need?" | [.env.example](../../.env.example) |

---

*Part of the docs/ai/ set — maintained for AI agent onboarding.*
