# RND-406 QA Evidence — 收费 T14 购买/续费/退款/到期冻结与恢复 非生产 E2E

## Scope and method

RND-406 requires repeatable, auditable non-prod evidence for the full annual-plan
commercial lifecycle, as the Go/No-Go gate before RND-390 (production WeChat Pay
enablement). All six blocking tickets (RND-389, 401, 402, 403, 404, 405) are Done and
verified present on `main` — RND-405 in particular was confirmed in the repo despite a
non-obvious commit subject (`d7d77b3 "feat(operations): add authoritative billing
controls"`, body ends `RND-405`), not just trusted from Linear status.

Every scenario below is driven through the **real** FastAPI app (`create_app()`), real
HTTP routes, real service functions (`billing_lifecycle`, `subscription_activation`,
`refunds`, `wechat_refunds`, `service_access`, `billing_lifecycle_batch`,
`platform_operations`), and real sqlite-backed persistence — never a mocked business
layer, never an HTTP-200-only or rendered-HTML check standing in for a real state
transition. The only faked boundary is the WeChat Pay/refund network call itself
(`CombinedFakeProvider`, implementing the full `PaymentProvider` protocol), which is
exactly the **"provider fixture"** the ticket's own scope sanctions in place of a live
WeChat Pay sandbox merchant — no production merchant, tenant, or customer data is ever
touched, and `WECHAT_PAY_ENABLED` is never toggled.

- New file: `backend/tests/test_rnd406_billing_e2e.py` — 14 tests covering the 13
  required scenarios (scenario 9 splits into 9a/9b, matching its two independent
  claims: frozen-renews-and-restores, and manual-suspension-is-never-cleared).
- No product code changes ship with this ticket. This file and this report are the
  entire deliverable.
- Fully automated and CI-safe: no live network, no browser, no real deployment
  required. Can be re-run on demand with `pytest backend/tests/test_rnd406_billing_e2e.py`.

## Scenario results

| # | Scenario (Linear text) | Test | Result |
|---|---|---|---|
| 1 | 新客户扫码支付成功后开通套餐并恢复服务 | `test_scenario1_new_customer_purchase_activates_and_restores_service` | PASS |
| 2 | 有效订阅续费从当前 `ends_at` 顺延 | `test_scenario2_renewal_while_active_extends_current_ends_at` | PASS |
| 3 | 已到期订阅续费从新支付成功时间重新起算 | `test_scenario3_renewal_after_expired_restarts_from_payment_time` | PASS |
| 4 | 退款申请被受理但未 SUCCESS 时不得回退订阅 | `test_scenario4_refund_accepted_not_success_does_not_roll_back` | PASS |
| 5 | Provider 确认退款 SUCCESS 后只撤销对应支付授予的期限 | `test_scenario5_refund_success_revokes_only_that_payments_term` | PASS |
| 6 | 已存在后续续费且期限无法安全回退时进入人工恢复队列，不自动猜测 | `test_scenario6_refund_success_after_later_renewal_goes_to_manual_recovery` | PASS |
| 7 | 到期后进入 7 天宽限期；宽限期内继续归档接收并提示续费 | `test_scenario7_expiry_enters_seven_day_grace_and_keeps_archiving` | PASS |
| 8 | 宽限期结束进入 frozen；普通用户、同步、媒体和导出受限；Owner 仍可进入账单并续费 | `test_scenario8_grace_end_freezes_and_restricts_service` | PASS |
| 9a | frozen Tenant 续费后恢复 | `test_scenario9a_frozen_tenant_renews_and_restores` | PASS |
| 9b | superadmin 手工 suspended 不得被支付自动解除 | `test_scenario9b_manual_suspension_is_never_cleared_by_payment` | PASS |
| 10 | Owner "到期不续费" 不提前终止、不自动退款 | `test_scenario10_owner_cancel_at_period_end_does_not_end_early_or_refund` | PASS |
| 11 | 支付查询、日终对账、退款查询/通知及异常恢复均可幂等重放 | `test_scenario11_idempotent_replay_across_query_notify_and_batch` | PASS |
| 12 | Owner 客户端与 superadmin 均显示一致的套餐/订阅/支付/退款状态 | `test_scenario12_owner_and_superadmin_views_agree` | PASS |
| 13 | 至少两个 Tenant 并行验证数据和授权隔离 | `test_scenario13_two_tenants_do_not_cross_contaminate` | PASS |

**14/14 PASS. 0 FAIL.**

### What each scenario actually proves (not HTTP-200 theater)

- **1**: before payment, `/api/billing/capacity` reports `state=unavailable`,
  `is_entitled=false`; after a real purchase → webhook round-trip, capacity flips to
  `state=normal`/`can_accept_new_media=true` and the subscription overview shows
  `display_state=paid_active` — the persisted `Subscription`/`SubscriptionActivation`
  rows are asserted directly, not just the HTTP response.
- **2/3**: exercises `subscription_activation.activate_or_renew_subscription`'s exact
  branch condition (`effective_status in {trial,active,grace}` → renewal from current
  `ends_at`; otherwise → restart from `trusted_at`) end to end through the payment
  webhook, with calendar-month arithmetic (`_add_calendar_months`) verified via test's
  own reference implementation of the identical algorithm.
- **4/5/6**: drives the real refund state machine (`refunds.py`) through the superadmin
  HTTP surface (`POST .../refunds`, `/api/refunds/wechat/notify`) — 4 proves a
  `processing` refund never touches the subscription; 5 proves a clean SUCCESS reverses
  exactly the prior snapshot (`SubscriptionTermGrant` → `reversed`); 6 proves that when
  a later renewal has moved the subscription past the refunded payment's snapshot,
  `_grant_matches_current` fails closed to `manual_recovery_required` with
  `failure_code=later_or_ambiguous_subscription_change` — no guessed rollback, subscription
  byte-identical to what the renewal left it.
- **7/8**: drives `reconcile_tenant_billing_lifecycle` (the same function the real
  scheduled batch calls) through real time-boundary math, then asserts against the
  single authoritative `tenant_service_denial`/`tenant_service_allows` policy table
  (`app/services/service_access.py`) that every router and worker gate already
  delegates to — grace keeps `WORKER_SYNC`/`INTERACTIVE` allowed (folded into `active`);
  frozen denies `INTERACTIVE`/`WORKER_SYNC`/`WORKER_MEDIA`/`WORKER_EXPORT` with
  `DENY_FROZEN` while `OWNER_BILLING` stays allowed, cross-checked live via
  `GET /admin/billing` (200) and a real renewal order creation (201) for the frozen
  Owner.
- **9a/9b**: 9a proves `restore_tenant_after_paid_subscription` actually flips a frozen
  tenant back to `active` on renewal. 9b proves the inverse two ways: the HTTP write
  surface is closed (`403`) for a suspended tenant before any payment can race it, *and*
  a direct call to `restore_tenant_after_paid_subscription` on a suspended tenant is a
  hard no-op (`changed=False`) — closing the race-window gap, not just the common path.
- **10**: `cancel_at_period_end=true` leaves `ends_at` and `Tenant.lifecycle_status`
  untouched and creates zero `RefundOrder` rows.
- **11**: four independent idempotent-replay proofs in one flow — (a) a duplicate
  `provider_event_id` payment webhook does not double-activate (`PaymentEvent` count
  stays 1, subscription unchanged); (b) `POST .../orders/{id}/refresh` after success
  short-circuits without ever calling the provider's query endpoint
  (`query_payment_calls == 0`); (c) a duplicate `Idempotency-Key` on the superadmin
  refund-query endpoint short-circuits via `authorize_platform_operation`'s replay path
  (`query_refund_calls == 1` after two calls); (d) `run_lifecycle_batch_once` run twice
  at the same instant is a true no-op the second time (`changed == 0`).
- **12**: after independently reconciling a tenant into `grace`, the Owner's
  `/api/billing/subscription` and the superadmin's
  `/api/platform/operations/tenants/{id}` agree on lifecycle status, effective
  subscription status, `ends_at`, and `cancel_at_period_end` — then a submitted refund's
  `processing` status agrees between the Owner's read-only view and the superadmin
  detail's `refund_orders` list.
- **13**: two tenants (`tenant-alpha` frozen via the real lifecycle batch,
  `tenant-beta` cleanly renewed) interleaved in one flow — alpha's expiry timeline is
  untouched by beta's renewal and vice versa, the batch's `tenant_transitions` count
  attributes exactly one `active->frozen` transition, and the superadmin's filtered
  tenant list (`?lifecycle_status=frozen` / `=active`) never cross-leaks a row between
  the two tenants.

## Commands run

- `pytest tests/test_rnd406_billing_e2e.py -v` — **14 passed in ~2s**.
- `pytest tests/test_architecture_boundary.py -q` — **25 passed** (guardrail unaffected;
  this ticket adds no new `app/` module).
- Full regression: `pytest tests -q` — **3373 passed, 158 skipped, 0 failed** (158 skips
  are pre-existing, environment-gated — unrelated to this ticket).
- `make lint-diff` — **OK** (0 findings on the new file after removing 2 unused imports
  ruff caught).
- `make typecheck` — **OK** (import/syntax-level; no mypy/pyright configured repo-wide).
- `make build` — **OK** (backend compiles; all static JS syntax-checks clean).

## Known limitations / explicit residual manual step

- This evidence does not include a live WeChat Pay sandbox round-trip or an HTTP driver
  against a real non-prod deployment. Unlike RND-389 (which needed one, because WeCom's
  encrypted-callback authorization flow cannot be faked), every RND-406 scenario is
  provider- and time-boundary business logic already reachable through real HTTP
  endpoints and real persistence with a controllable provider fixture — the ticket's own
  scope explicitly sanctions this in place of "微信支付沙箱/隔离商户配置". If a live
  WeChat Pay sandbox merchant becomes available before RND-390, a confirmatory live run
  (order → real sandbox QR → real sandbox notify) is a reasonable additional check, but
  is not required by this ticket's acceptance criteria and is not blocking.
- The provisioning → WeCom-authorization → self-test → explicit `/activate` chain that
  promotes a genuinely brand-new signup out of `provisioning` is RND-389's dedicated,
  already-Done E2E surface (live driver + offline guard suite) and is intentionally not
  re-derived here; scenario 1 instead proves the billing half of "new customer pays"
  (subscription creation, entitlement grant, capacity unlock) for an already-onboarded
  tenant, which is the piece RND-406 owns.
- Two-tenant isolation (scenario 13) is proven within one process/DB using two distinct
  `tenant_id`s and independent session/PlatformAdmin scoping — not across two physically
  separate deployments. Cross-process/cross-deployment isolation is outside a functional
  E2E test's reach and is not part of this ticket's scope.

## Go/No-Go recommendation for RND-390

**Go.** All 13 required scenarios pass against real service logic and real persistence,
covering purchase, renewal-while-active, renewal-after-expiry, refund
accepted/succeeded/manual-recovery, grace, freeze, frozen-restore,
suspension-immune-to-payment, cancel-at-period-end, idempotent replay across every
query/notify/batch surface, Owner/superadmin view consistency, and two-tenant isolation.
No known gaps block production WeChat Pay enablement from a billing-lifecycle-logic
standpoint. The one residual item (a live WeChat Pay sandbox confirmatory run) is
optional evidence, not a blocker, per the reasoning above.

## QA Summary

Files changed:
- `backend/tests/test_rnd406_billing_e2e.py` (new): 14 E2E tests covering RND-406's 13
  required scenarios, driven through the real app with a full-protocol fake payment/
  refund provider.
- `tasks/RND-406-e2e-evidence.md` (new, this file): QA/Go-No-Go evidence report.

Acceptance criteria:
- [x] 关键场景具有可复跑的测试，脱敏证据保存于本文件
- [x] 不以 HTTP 200、模拟 HTML 或历史 PASS 代替真实状态转换、持久化和用户可见结果 — 见上方逐场景说明
- [x] 明确列出通过项、失败项、已知限制以及生产 Go/No-Go — 见上方
- [x] 全量 CI 通过 — `pytest tests -q`: 3373 passed, 158 skipped, 0 failed
- [x] 不部署生产、不切换 `WECHAT_PAY_ENABLED` — 全程仅使用本地 sqlite + fake provider

Commands run:
- `pytest tests/test_rnd406_billing_e2e.py -v`: 14 passed
- `pytest tests -q`: 3373 passed, 158 skipped
- `make lint-diff`: OK
- `make typecheck`: OK
- `make build`: OK

Manual verification: n/a (backend-only, no UI change)

Risks or gaps: none blocking; see "Known limitations" above for the one optional
follow-up (live WeChat Pay sandbox confirmatory run).

No secrets introduced: confirmed
Only intentional files changed: confirmed (`backend/tests/test_rnd406_billing_e2e.py`,
`tasks/RND-406-e2e-evidence.md`)
