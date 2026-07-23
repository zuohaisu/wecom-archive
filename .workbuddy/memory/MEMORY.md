# Project Memory — wecom-archive-365

## Project Identity
- Linear project: "365企微会话存档" (ID: cfe726ec-810c-4848-8455-9b3607bf1fc1)
- Linear team: Builder (RND), key "RND"
- Code repo: /Users/hzuo/Documents/code/wecom-archive-365
- Dev rules: see DEV_AGENT_RULES.md (AI agent workflow: ChatGPT planning → Claude Code impl → Codex QA)
- **`.workbuddy/memory/` is an INTENTIONALLY git-tracked project process asset** (project flow knowledge, not personal scratch). Already tracked (NOT gitignored). When committing RND work, **include `.workbuddy/memory/**` in the commit — do NOT exclude it.** (Corrected 2026-07-23: earlier verbal advice to "leave memory out of the PR" was wrong; user confirmed these are project assets to be tracked.)
- **Git workflow convention (CRITICAL, reinforced 2026-07-24):** The agent must **NOT commit in-development feature-branch code** (e.g. RND-229 on `feature/rnd-229`). The user verifies the work himself and merges via **GitHub PR** — NOT local merge, NOT agent commit. Agent git writes are only allowed when the user explicitly authorizes each action. **Incident 2026-07-23:** agent over-stepped — committed RND-229 code + locally merged RND-228/RND-229 into `main` without authorization. User had the agent `git reset --hard origin/main` (main) then later `git reset --soft origin/main` on `feature/rnd-229` to undo the 2 wrong commits (code preserved as uncommitted). Lesson: never assume "do the merge" means local commit+merge; always confirm and let the user drive PRs.

## Linear Integration
- MCP config: ~/.workbuddy/mcp.json, server "linear" via @calltelemetry/linear-mcp
- API key configured and trusted

## Issue Analysis (refreshed 2026-07-22 from live Linear)
- Original 4-phase plan mostly shipped. Resolved since: RND-209, RND-213, RND-214, RND-215, RND-207, **RND-227 (Done — CI/CD auto-migration + rollback; no longer a blocker)**.
- **Done & merged to main + pushed (CI/CD deployed):** RND-225 (P1 OAuth fail-open), RND-226 (nested-media entity-context, `75dd4b2`), RND-159 (search contacts+chat, by Trae+DeepSeek V4 flash), RND-210 (msg-type/card fix, commit `016609f`). All verified via `make verify` (2034 / 2065 passed) before merge. NOTE: Linear comments on RND-226/210 saying "not committed/pushed" are STALE (written pre-commit by agent) — actual git state is merged+pushed.
- **Open P2 Todo, ready & parallel-safe:** RND-191 (API/SQL perf — unblocked now RND-190 baseline Done), RND-160 (2c2g server opt), RND-212 (modular-monolith refactor).
- **New search-cluster (all P3 Backlog, the natural next focus — RND-159 just landed):** RND-228 (backend scalability: SQL-level LIMIT + dedup staff resolution + cleanup unused imports) — **DONE 2026-07-23 (verified on other Mac, awaiting Haisu commit/push)**, RND-229 (frontend: dedicated search-results page + jump-to-message highlight/scroll), RND-230 (frontend: multi-filter date/user/employee/msgtype on results page; depends on RND-229). Design spec + mockup already exist in `design/` (RND-229-230-search-design-spec.md, search-results-mockup.html).
- **Coupling (RESOLVED 2026-07-23):** RND-226 & RND-210 BOTH edit `app/routers/conversations.py` (overlap zone). Done sequentially: RND-226 merged first (Done), then RND-210 implemented on post-226 main and merged. **RND-210 = DONE (commit `016609f`, CI/CD, Linear status Done)** — official `meeting_voice_call`/`voip_doc_share` aliases + structured parsers/renderers + card contact-name + public-field projection (no sdkfileid leak) + top-level voiceid dispatch merge. RND-210 execution prompt: `.workbuddy/prompts/rnd-210-execution-prompt.md`; RND-226 prompt: `.workbuddy/prompts/rnd-226-execution-prompt.md`.
- **Two-machine parallel dev (2026-07-23):** User runs **RND-229 (frontend, this Mac)** and **RND-228 (backend, a SEPARATE Mac)** in parallel. Files do NOT overlap → merge order arbitrary, zero conflict. RND-229 → `backend/app/main.py` + console JS (`_SEARCH_PAGE_HTML`, `/admin/search` route, topbar Enter→results, `focusMessage` highlight/scroll). RND-228 → `backend/app/routers/search.py` (SQL-level LIMIT on contacts search `.all()`→bounded; delete dup `_build_staff_ids` in favor of `conversation_membership._collect_staff_ids`; remove unused `case/func/text/union` imports) + `backend/app/conversation_membership.py` (no change needed, just import reuse). Both branch off latest `main` (has 225/226/159/210). **This Mac: branch `feature/rnd-229` created & active.** Other Mac: RND-228 prompt at `.workbuddy/prompts/rnd-228-execution-prompt.md` (instructs `git checkout -b feature/rnd-228`, stay within 2 backend files, RED-first bounded-query test, no auto commit, NO frontend/main.py touches). RND-229 prompt not yet written (this machine's own agent work). **RND-228 re-review PASSED 2026-07-23 (6 new tests `test_rnd_228_search_scalability.py`, full regression PASS), code complete & verified on other Mac, awaiting Haisu manual commit/push. NOTE: other Mac showed 2011 passed/63 skipped vs this-machine baseline 2065/3 — ~60 extra skips likely environmental, not a regression (see 2026-07-23 log).**
- **Backlog P2:** RND-221/220/219 (service-extraction chain), RND-211 (sync-aware refresh + "sync now"), RND-195 (all WeCom msg types — umbrella for RND-210), RND-166/165 (SaaS ops/payments).
- **FROZEN — In Review, blocked by WeCom enterprise name change (NOT within ~1 wk):** RND-104/107/108/129/130/175. Do not merge / don't advise merge.
- Refactor chain done through RND-215; 216→217→218→{219|220|221}→222→223→224 still pending if pursued.
- NOTE: user-level memory still lists RND-159/160/161/167/176 as "Todo quick wins" — stale. Live: RND-159 In Progress; RND-160/161/167/176 are Backlog.

## Linear API quirks
- Markdown `_` is parsed as emphasis; always wrap code identifiers (e.g. `media_files.thumbnail_ref`) in backticks
- API response `description` field returns plain-text rendering; actual stored markdown renders correctly in Linear UI
- New issue default state may be "Backlog"; explicitly set status="Todo" on creation if it should be actionable
- RND-210 is closed (message type fix, child of RND-195) — DONE, do not reuse the issue
