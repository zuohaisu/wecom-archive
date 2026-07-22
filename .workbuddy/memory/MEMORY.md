# Project Memory — wecom-archive-365

## Project Identity
- Linear project: "365企微会话存档" (ID: cfe726ec-810c-4848-8455-9b3607bf1fc1)
- Linear team: Builder (RND), key "RND"
- Code repo: /Users/hzuo/Documents/code/wecom-archive-365
- Dev rules: see DEV_AGENT_RULES.md (AI agent workflow: ChatGPT planning → Claude Code impl → Codex QA)

## Linear Integration
- MCP config: ~/.workbuddy/mcp.json, server "linear" via @calltelemetry/linear-mcp
- API key configured and trusted

## Issue Analysis (refreshed 2026-07-22 from live Linear)
- Original 4-phase plan mostly shipped. Resolved since: RND-209, RND-213, RND-214, RND-215, RND-207, **RND-227 (Done — CI/CD auto-migration + rollback; no longer a blocker)**.
- **In flight (Haisu):** RND-225 (P1, OAuth fail-open security) In Progress; RND-159 (P2, search contacts+chat) In Progress.
- **Open P2 Todo, ready & parallel-safe (no migration/CI/WeCom-name-change dependency):** RND-226 (nested-media entity-context fix), RND-210 (official msg-type + card rendering, child of RND-195), RND-191 (API/SQL perf — unblocked now RND-190 baseline Done), RND-160 (2c2g server opt), RND-212 (modular-monolith refactor).
- **Backlog P2:** RND-221/220/219 (service-extraction chain), RND-211 (sync-aware refresh + "sync now"), RND-195 (all WeCom msg types — umbrella for RND-210), RND-166/165 (SaaS ops/payments).
- **FROZEN — In Review, blocked by WeCom enterprise name change (NOT within ~1 wk):** RND-104/107/108/129/130/175. Do not merge / don't advise merge.
- Refactor chain done through RND-215; 216→217→218→{219|220|221}→222→223→224 still pending if pursued.
- NOTE: user-level memory still lists RND-159/160/161/167/176 as "Todo quick wins" — stale. Live: RND-159 In Progress; RND-160/161/167/176 are Backlog.

## Linear API quirks
- Markdown `_` is parsed as emphasis; always wrap code identifiers (e.g. `media_files.thumbnail_ref`) in backticks
- API response `description` field returns plain-text rendering; actual stored markdown renders correctly in Linear UI
- New issue default state may be "Backlog"; explicitly set status="Todo" on creation if it should be actionable
- RND-210 is already taken (message type fix, child of RND-195) — do not reuse
