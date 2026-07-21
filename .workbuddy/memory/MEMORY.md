# Project Memory — wecom-archive-365

## Project Identity
- Linear project: "365企微会话存档" (ID: cfe726ec-810c-4848-8455-9b3607bf1fc1)
- Linear team: Builder (RND), key "RND"
- Code repo: /Users/hzuo/Documents/code/wecom-archive-365
- Dev rules: see DEV_AGENT_RULES.md (AI agent workflow: ChatGPT planning → Claude Code impl → Codex QA)

## Linear Integration
- MCP config: ~/.workbuddy/mcp.json, server "linear" via @calltelemetry/linear-mcp
- API key configured and trusted

## Issue Analysis (2026-07-21)
- 4-phase execution plan established for remaining issues
- Phase 0: RND-225 (security), RND-213/214 (testing) — parallel, immediate
- Phase 1: RND-209 (arch eval, P2), RND-207/191/226/210 — parallel independent
- Phase 2: Backend refactoring chain (215→218→{219|220|221}→222→223→224) + Frontend (216→217)
- Phase 3: Features & ops (195 close, 211, 192/193, 183, 176/202/208)
- Blocking relationships set: RND-209, RND-213, RND-214 all block RND-215
