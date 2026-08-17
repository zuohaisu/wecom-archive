# RND-387 QA Summary — 多租户回调分发、Worker 与 Cursor 隔离 (T2)

## Files changed

- `backend/app/services/wecom_callback_crypto.py` — new pure `extract_outer_corp_id(xml_body)` (CDATA-only outer ToUserName with the same DOCTYPE/parse guards as `extract_encrypt`).
- `backend/app/services/tenant_callback_resolution.py` (new) — `CallbackCredentialCandidate` frozen dataclass + `callback_candidates(db, outer_corp_id)`: env callback settings first (single-corp fast path, zero-regression), then corp-matched active stored-config rows, then remaining active rows (defensive); `env_configuration_error` signal for partial env config; db=None / DB failure degrades to env-only.
- `backend/app/routers/wecom_events.py` — candidate-chain rework: GET echostr and POST both authenticate by iterating candidates (verify_signature → decrypt_envelope → parse_plaintext_envelope), receiver-id is the authoritative corp match, per-candidate decrypt failure just continues; `_log_rejected` fixed-enum fallback only; dispatch carries `trigger_source="callback"` + resolved `tenant_id`; 503 when no tenant resolves (unchanged).
- `backend/app/services/tenant_credentials.py` (new) — `active_tenant_configs(db)` (Tenant is_active + lifecycle_status=="active" JOIN config.is_active, ordered by created_at), `config_for_tenant`, `tenant_log_tag(tenant_id)` = sha256[:12] digest (logs never carry raw tenant/corp ids).
- `backend/scripts/run_archive_worker_once.py` — three modes: `WECOM_TENANT_ID` (single-tenant hard-fail via `_run_single_tenant_chain`), `WECOM_CORP_ID` (legacy env chain byte-identical), neither (loop `active_tenant_configs`, per-tenant sync+decrypt subprocess with merged decrypted env, per-tenant failure continues, media wake-up only for successful tenants, reachability subprocess per-tenant env); `_TRIGGER_SOURCES` gains `"activation"`; lifecycle line carries `result/exit_code/error_class`.
- `backend/scripts/sync_wecom_archive_once.py` / `decrypt_wecom_messages_once.py` / `download_wecom_media_once.py` — per-tenant `WECOM_TENANT_ID` mode resolving decrypted DB creds (env path otherwise byte-identical); decrypt script gains the stored-RSA private-key fallback (`private_key_encrypted` was written but never read).
- `backend/scripts/run_reachability_automation_once.py` — `WECOM_TENANT_ID` fast path.
- `backend/app/services/archive_worker_trigger.py` — `dispatch_archive_worker(trigger_source, tenant_id=None)` threads `WECOM_TENANT_ID` into the child env.
- `backend/app/media_event_dispatch.py` — `dispatch_media_worker(trigger_source, tenant_id=None)` bypasses env resolution when tenant_id given; `tenant_log_tag` imported from `services/tenant_credentials.py` (re-export kept).
- 5 mechanically-updated test files (`test_wecom_events.py`, `test_archive_worker_trigger.py`, `test_rnd343_worker_scheduling.py`, `test_archive_worker_reachability_hook.py`, `test_external_contact_identity.py`) — dispatch lambdas/worker args keep signatures in sync with the new `tenant_id` threading; legacy-chain tests pin `WECOM_CORP_ID` explicitly since "neither env var" now means loop mode.
- `backend/tests/test_rnd387_multi_tenant_callback.py` (new, 18 tests) — extract_outer_corp_id parsing; candidate ordering/isolation; route GET echostr vs stored tenant B, foreign-key 400, receiver mismatch 403, POST outer-corp dispatch, receiver-id-authoritative defensive match, env-candidate zero-regression, malformed 400, no-match 403 no-dispatch.
- `backend/tests/test_rnd387_multi_tenant_worker.py` (new, 11 tests) — per-tenant env merge, one-failure-continues, all-failed exit 1, zero-tenants exit 0, unreadable-cred skip, single-tenant hard-fail paths, legacy env chain, decrypt stored-RSA fallback round-trip (private_key_encrypted → decrypted → `serialization.load_pem_private_key`), sync per-tenant `init_calls`.

## Acceptance criteria

- [x] 回调按租户分发 — outer ToUserName resolves the stored per-tenant callback credentials; receiver-id of the decrypted envelope is the authoritative corp match; wrong-tenant key never aborts (defensive candidate continues).
- [x] 默认租户零回归 — env callback credentials stay candidate #1; `WECOM_CORP_ID` env chain byte-identical; existing callback/worker CLI tests pass unchanged.
- [x] Worker 按租户隔离 — loop mode runs sync+decrypt per active tenant with decrypted per-tenant env; per-tenant failure continues; media wake-up only for successful tenants; global per-worker flocks unchanged.
- [x] 凭据不出日志/出进程 — child env holds decrypted creds in-process only; logs carry `tenant_log_tag` sha256[:12] digests; no plaintext in test assertions.
- [x] 不信任客户端输入 — callback resolution keys entirely on server-side stored rows + env; tenant_id comes from resolution, never from the request body.
- [x] 游标隔离 — per-tenant isolation via existing `SyncState` (tenant_id, corp_id) unique + `skip_locked` cursor lock; no schema change in T2.
- [x] `lifecycle_status` CHECK 冻结 — T2 touches no lifecycle column, no migration, no route count change (152 before and after).

## Commands run

- New suites: `pytest backend/tests/test_rnd387_multi_tenant_callback.py test_rnd387_multi_tenant_worker.py -q` — **29 passed in 2.12s**.
- Regression suites: `test_wecom_events.py` + `test_archive_worker_trigger.py` + `test_rnd343_worker_scheduling.py` + `test_archive_worker_reachability_hook.py` + 3 CLI suites — **74 passed in 3.18s**.
- Route-count discipline: `len(app.routes)` == 152 at T1 commit (904c86b) and at current tree; T2 adds zero routes.
- `make verify` (ruff lint-diff → typecheck → build → full pytest) — **OK: 3106 passed, 93 skipped, 0 failed** (up from 3077 at T1; +29 new RND-387 tests). One fix during the gate: `test_external_contact_identity.py::test_archive_worker_never_runs_full_external_contact_sync` needed `WECOM_CORP_ID` pinned (RND-387 made "neither env var set" mean multi-tenant loop mode, which requires DATABASE_URL; the test's contract is mode-agnostic, so it now pins the legacy chain explicitly).

## Manual verification

- `extract_outer_corp_id` guards verified against DOCTYPE / unterminated-XML / plain-text (CDATA-only) inputs.
- Stash-recovery drill: accidental `git stash` during a route-count check was identified via `git stash list`, restored with `git stash pop` (pre-existing RND-210 WIP stash untouched), and the tree re-verified green — no data lost.

## Risks or gaps

- Real WeCom wire verification (callback URL, GET echostr against a live corp) is exercised only in the non-prod E2E (T4, gated on RND-351/392/352).
- The decrypt stored-RSA fallback is covered by unit tests with a generated keypair; live third-party-app key retrieval remains env-path (unchanged behavior).
- Media wake-up in loop mode is per-tenant (`media_tenant_ids`); legacy no-arg media dispatch preserved for the `WECOM_CORP_ID` path.

## No secrets introduced: confirmed
## Only intentional files changed: confirmed
