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
- **Done (2026-07-22):** RND-225 (P1, OAuth fail-open security) — verified/shipped.
- **In flight:** RND-159 (P2, search contacts+chat) In Progress — executed by **Trae + DeepSeek V4 flash**.
- **Open P2 Todo, ready & parallel-safe (no migration/CI/WeCom-name-change dependency):** RND-226 (nested-media entity-context fix), RND-210 (official msg-type + card rendering, child of RND-195), RND-191 (API/SQL perf — unblocked now RND-190 baseline Done), RND-160 (2c2g server opt), RND-212 (modular-monolith refactor).
- **Coupling:** RND-226 & RND-210 BOTH edit `app/routers/conversations.py` (overlap zone ~1777-1822: nested-media check + media classification); RND-210 also touches `app/message_type_registry.py` / `app/structured_message_parser.py`. RND-226 changes nested-media `access_url` shape that RND-210's card rendering consumes → logical coupling. **Do on ONE branch / ONE engine; do NOT split across two parallel engines (text conflict + URL↔rendering mismatch). Order: RND-226 first, then RND-210.**
- **Decision (2026-07-23):** user chose to do them SEPARATELY — RND-226 first (alone), THEN RND-210 on a branch off main **after RND-226 merges** (never on pre-226 branch). RND-210 execution prompt prepared in advance: `.workbuddy/prompts/rnd-210-execution-prompt.md`. RND-226 prompt: `.workbuddy/prompts/rnd-226-execution-prompt.md`.
- **Backlog P2:** RND-221/220/219 (service-extraction chain), RND-211 (sync-aware refresh + "sync now"), RND-195 (all WeCom msg types — umbrella for RND-210), RND-166/165 (SaaS ops/payments).
- **FROZEN — In Review, blocked by WeCom enterprise name change (NOT within ~1 wk):** RND-104/107/108/129/130/175. Do not merge / don't advise merge.
- Refactor chain done through RND-215; 216→217→218→{219|220|221}→222→223→224 still pending if pursued.
- NOTE: user-level memory still lists RND-159/160/161/167/176 as "Todo quick wins" — stale. Live: RND-159 In Progress; RND-160/161/167/176 are Backlog.

## Linear API quirks
- Markdown `_` is parsed as emphasis; always wrap code identifiers (e.g. `media_files.thumbnail_ref`) in backticks
- API response `description` field returns plain-text rendering; actual stored markdown renders correctly in Linear UI
- New issue default state may be "Backlog"; explicitly set status="Todo" on creation if it should be actionable
- RND-210 is already taken (message type fix, child of RND-195) — do not reuse

## Status Update (2026-07-24)
- **Search-cluster current state:**
  - RND-159 (search contacts+chat): Done & shipped (merged earlier this week).
  - **RND-228 (backend search scalability): MERGED + DEPLOYED 2026-07-24** via PR #5 (merge commit `f37a42a`), CI/CD green (CI Test Suite ~3m15s, Deploy to ECS 16s). Scope: `backend/app/routers/search.py` (SQL-level LIMIT + dedup shared helper), `backend/tests/test_rnd_228_search_scalability.py`, prompt doc only. No schema / config / frontend change. GitHub Actions Node 20 deprecation warning (functional, non-blocking).
  - **RND-229 (frontend search results page + jump-to-message): MERGED + DEPLOYED 2026-07-24** via GitHub PR; GitHub Actions CI/CD all checks passed. Scope: `backend/app/main.py` — dedicated `/admin/search` results page + click→jump back with auto-scroll/highlight (`focusMessage`, globals `focusMsgId`/`focusPending`). The `focusPending is not defined` QA-test risk was resolved before merge (test preamble fix applied → `make verify` green). Frontend-only, no schema change. **Unblocks RND-230.**
  - RND-230 (multi-filter on results page): still Backlog, **now UNBLOCKED** (RND-229 shipped). Next natural search-cluster task.
