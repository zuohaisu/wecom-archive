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
- Refactor chain done through RND-215. **RND-216 MERGED to main (2026-07-24, commit `b2d46fc`)** — unblocks RND-217/218. **RND-217 已实现（工作区改动就绪、未提交）；须等 QA agent 验收通过且用户明确许可后才可 commit/push.** 218→{219|220|221}→222→223→224 still pending.
- **Chain progress (2026-07-25):** RND-218 (`5fa1ee6`)、RND-219 (`13e1cee`)、RND-220 (`4d55766`=HEAD)、RND-221 均已在 `main` 工作树实现（未提交/未独立提交）；**RND-221 已独立 QA 验收 PASS（16/16，comment `66ea1deb`，状态 In Progress）**，实现为未提交工作区改动。RND-222/223/224 仍 pending，须等链前置合入 origin/main。注：route 总数自测=33（QA 自测口径，baseline 缺失）。

## Hard Rules (AI agent 必须遵守)
- **Agent 绝不执行 git commit / push**：所有 git 提交与推送一律由用户本人操作。即便改动就绪、QA 已通过、或用户说「可以提交」，agent 也不代劳——只提示用户自行 commit/push。（2026-07-25 用户明确：git commit 暂时都由用户自己做。）
- **QA 先行**：开发 agent 实现后，先交独立 QA agent 验收（读言、不改实现、不 commit/push），QA 通过后由用户决定是否 commit。
- 交付物是「开发提示词 + 验收提示词」两份文件，由用户决定何时交给开发/QA agent 执行。
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
