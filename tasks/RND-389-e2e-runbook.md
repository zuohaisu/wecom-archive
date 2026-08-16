# RND-389 E2E Runbook — 非生产环境自助开通全链路验证

> Execution is gated on RND-351 (non-prod third-party app), RND-392 (non-prod deploy env)
> and RND-352 (test corp). This commit ships the driver + guards; the run itself happens
> after those three are Done, and its results land in `RND-389-qa-report-template.md`
> (filled as `RND-389-qa-summary.md`).

## 1. Preconditions

- Deployment with T1 (RND-386), T2 (RND-387), T3 (RND-388) merged and running.
  `alembic upgrade head` applied (0055, 0056).
- Test corp (RND-352) authorized against the non-prod third-party app (RND-351).
- Operator has a browser session on the non-prod deployment logged in as the test corp's
  provisioning owner admin (created by the third-party auth flow — the claim/browser
  handoff is the only browser-assisted step).
- Archive worker cron/timer chain running (T2's `run_archive_worker_once` multi-tenant
  loop, `WECOM_TENANT_ID` support live).
- 可信 IP of the deployment added to the test corp's 会话存档 IP allowlist in the WeCom
  admin console; 会话存档 public key (version N) uploaded; callback URL verified:
  `GET {BASE_URL}/api/wecom/events/callback` echostr check against the corp's Token/AESKey.

## 2. Environment

| Var | Meaning |
|---|---|
| `RND389_E2E=1` | arming flag — the driver refuses to run without it |
| `APP_ENV` | must NOT be `production` (guard refuses; production entry stays closed) |
| `RND389_BASE_URL` | non-prod deployment base URL, e.g. `https://staging.example.com` |
| `RND389_SESSION_ID` | provisioning-session cookie value copied from the operator browser (DevTools → Application → Cookies → `session_id`) |
| `RND389_ARCHIVE_SECRET` | test corp's 会话存档 Secret (from the corp admin console) |
| `RND389_PRIVATE_KEY_FILE` or `RND389_PRIVATE_KEY_PEM` | RSA private key (must match the uploaded public key; exactly one source) |
| `RND389_PUBLIC_KEY_VERSION` | public key version uploaded in the corp admin console |
| `RND389_CALLBACK_TOKEN` / `RND389_CALLBACK_AES_KEY` | callback Token / EncodingAESKey the corp configured (same values pasted into the wizard) |
| `RND389_WRONG_ARCHIVE_SECRET` | a deliberately wrong secret for the negative probe |
| `RND389_SESSION_ID_2` | (optional) second test corp's provisioning session — enables the second-tenant isolation probe |
| `RND389_POLL_SECONDS` | status poll interval (default 5) |
| `RND389_ACTIVATION_TIMEOUT` | seconds to wait for ready/active (default 120) |
| `RND389_MESSAGE_TIMEOUT` | seconds to wait for the first archived message (default 180) |
| `DATABASE_URL` | (optional) non-prod DB — enables DB-side assertions (trial row, sync_states, archive_messages); without it DB steps skip |

## 3. Run

```bash
cd backend
export RND389_E2E=1 APP_ENV=staging \
  RND389_BASE_URL=... RND389_SESSION_ID=... \
  RND389_ARCHIVE_SECRET=... RND389_WRONG_ARCHIVE_SECRET=... \
  RND389_PRIVATE_KEY_FILE=... RND389_PUBLIC_KEY_VERSION=... \
  RND389_CALLBACK_TOKEN=... RND389_CALLBACK_AES_KEY=... \
  RND389_SESSION_ID_2=... DATABASE_URL=...
.venv/bin/python scripts/e2e_rnd389_self_service_activation.py
```

Exit 0 = all enabled steps PASS (second-tenant and DB steps may legitimately skip);
exit 1 = at least one step FAILED. The step table prints status + safe detail per phase.

### Phases

1. `provisioning_session` — the cookie is a live provisioning-scoped owner session; lifecycle=provisioning.
2. `negative_wrong_secret` — save a wrong secret → config test fails → activate blocked with `credentials_invalid`/`connectivity_failed`. Proves negative gating.
3. `negative_no_entitlement` — DB probe inserts an expired subscription → activate blocked `no_entitlement`, no grant, no promotion; row removed afterwards. (Requires `DATABASE_URL`.)
4. `configure` + `self_test` — save the real secret + RSA key + callback creds; config test (local decrypt round-trips + live connectivity probe) all_ok.
5. `wait_ready` — the PUT/test auto-trigger evaluates the gates; waits for `ready` (or `active`, if the auto-trigger already promoted).
6. `activate` — manual activate (or 401 surface-closed if auto-promotion won the race); DB asserts exactly one `self_service_trial` subscription.
7. `replay_idempotent` — a second activate is unreachable (401, surface closed after promotion) and the DB still shows exactly one trial row — no double grant.
8. `post_activation_access` — provisioning routes answer 401 (session promoted to admin); `/admin/login` bounces to `/dashboard`.
9. `billing_probe` — GET plan (annual code); if payment is configured, create an order with an `Idempotency-Key` and close it; asserts `closed`.
10. `first_message` — operator sends one 会话存档 message in the test corp (the driver prints a prompt and waits); asserts the tenant's `sync_states` row exists and `archive_messages` grows tenant-scoped. (Requires `DATABASE_URL`.)
11. `second_tenant_isolation` — with a second corp session: distinct tenant resolution, its own sync state; workers/callbacks stay per-tenant. (Skips without `RND389_SESSION_ID_2`.)

## 4. Teardown

- Freeze/suspend or deactivate the test tenants via the platform console; delete or mark
  the trial subscriptions expired in the non-prod DB (subscriptions `status=expired`).
- Remove the test corp's authorization in the WeCom admin console if the corp is reused.
- Unset all `RND389_*` env vars; never leave secrets in shell history (use a file with
  0600 perms or an env-file loader, delete afterwards).
- Confirm the production deployment still has no RND-389 surface: the driver's
  `APP_ENV=production` refusal is the backstop; nothing is deployed from this ticket.

## 5. Rollback notes

- Driver only touches the non-prod deployment; no migrations ship in this ticket.
- If a step regresses: `git revert` is not needed — fix the failing component ticket
  (T1/T2/T3) and re-run; the driver is idempotent phase-wise (activate replay short-circuits,
  wrong-secret/no-entitlement probes are self-cleaning).
