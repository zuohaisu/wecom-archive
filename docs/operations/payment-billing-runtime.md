# Payment and Billing Runtime Runbook

**Status:** versioned intended state for GH-106. Production installation and
execution evidence require an approved Ops rollout; this document does not
claim that repository changes have already run on production.

## Authority and state machines

Commercial state is deliberately split into three authorities:

| Authority | Canonical state | Writer |
| --- | --- | --- |
| Payment funds | `creating -> pending -> paid_activation_pending -> succeeded`; unpaid orders can become `closed` or `failed` | verified callback or verified provider query only |
| Subscription | `trial` / `active -> grace -> expired`, or `canceled`; a trusted new payment can restore an entitled state | subscription activation and lifecycle service |
| Tenant service | `provisioning`, `active`, `frozen`, `suspended` | lifecycle projection or audited platform operation |

`paid_activation_pending` is an explicit recovery bridge, not a successful
subscription. A verified payment event is persisted with replay protection,
then idempotently activates or renews the subscription. `suspended` is an
independent safety state and payment must not clear it. `PaymentOrder`,
verified `PaymentEvent`, `Subscription`, `SubscriptionActivation`,
`SubscriptionTermGrant`, and their histories/audits are the durable facts;
a browser redirect, checkout display, or HTTP success is not.

The detailed business decisions remain [ADR-0004](../adr/0004-annual-plan-wechat-pay-gates.md),
[ADR-0005](../adr/0005-saas-billing-lifecycle-refunds-and-service-gates.md),
and [ADR-0006](../adr/0006-alipay-pc-page-pay.md).

## Required scheduled executors

The following pairs are listed in `deploy/systemd/MANAGED_UNITS`. On an
approved deploy to `main`, deploy step 10 copies changed units, reloads
systemd, and enables only the trigger units. The oneshot services are never
enabled directly.

| Unit | Cadence | Responsibility | Intended state |
| --- | --- | --- | --- |
| `wecom-billing-lifecycle.service/.timer` | every five minutes at `:07` | Row-locked, replay-safe projection of commercial Subscription time boundaries to Tenant service state; legacy tenants without a Subscription are not scanned. | Required |
| `wecom-billing-notifications.service/.timer` | every five minutes at `:02` | Plan/deliver the durable owner/operations notification outbox for expiry, freeze, payment activation and anomaly events. | Required |
| `wecom-payment-recovery.service/.timer` | every five minutes at `:04` | Query due **WeChat Pay** orders, apply only trusted provider facts, retry recoverable work, and create sanitized operational findings. | Required when historical/current `wechat_pay` orders exist and valid recovery credentials are configured |
| `wecom-payment-reconciliation.service/.timer` | daily `02:30 UTC` | Query the previous UTC day's and up to 30 days of unreconciled **WeChat Pay** orders without reopening checkout. | Required when historical/current `wechat_pay` orders exist and valid recovery credentials are configured |

`Persistent=true` is intentional on every timer: a missed scheduled time is
made up by systemd after it becomes available. The recovery and reconciliation
units use the existing `process_payment_recovery_once.py` entry point with the
explicit `recovery` and `reconciliation` modes; no managed unit refers to a
missing script.

### Provider boundary

The current scheduled recovery implementation deliberately filters on the
WeChat provider and constructs `get_wechat_pay_provider()`. It therefore
requires complete, valid WeChat credentials even if new checkout creation is
disabled by `WECHAT_PAY_ENABLED=false`; disabling new orders is not permission
to lose the ability to recover historical orders.

Alipay checkout/query is provider-neutral at the request path, but automatic
Alipay recovery/reconciliation is **deferred**: there is no Alipay timer or
provider-selecting batch entry point. ADR-0006 explicitly leaves Alipay
reconciliation to a separate decision and ticket. Before claiming the
lost-callback invariant for an Alipay order, Operations must use the audited
controlled query path and obtain approval for an Alipay recovery follow-up.
Do not point either WeChat unit at an Alipay order.

## Lost-callback recovery and safeguards

For an eligible WeChat order, the recovery timer claims only automatic,
non-leased work from the last 24 hours that is due for a query. Claiming uses
`FOR UPDATE SKIP LOCKED` and persists a four-minute lease, so overlapping
timer/manual runs cannot process the same candidate concurrently. Per-order
state updates are row-locked.

A verified `SUCCESS` query follows the same trusted-event validation and
idempotent activation path as a callback. A verified pending result is retried
with the bounded 5, 10, 20, 40, 80, 160 and 320 minute backoff. The eighth
failed query changes the recovery state to `manual_recovery`, stops automatic
queries, and records a deduplicated operational finding instead of guessing a
money state. Pending work beyond expiry plus five minutes receives a timeout
finding. A trusted closed/failed/fully applied result clears automatic recovery
metadata and resolves its findings.

The T+1 executor uses the same locking and trusted-query boundary. It checks
prior-day facts and up to 30 days of unreconciled WeChat orders, records
`last_reconciled_at` only after a successful trusted query, and never creates
or reopens checkout. Unique provider order/transaction IDs, persisted provider
event IDs, idempotency-key hashes, activation records, and subscription term
grants make callback, recovery, and reconciliation replay-safe.

The lifecycle executor separately locks each commercial tenant and its
Subscription, advances `active/trial -> grace -> expired` at UTC boundaries,
and projects `expired/canceled` to `frozen` without clearing `suspended`.
It retries an individual tenant at most the configured bounded count (default
2); one failure is counted without aborting the remaining batch.

The notification executor deduplicates each event/audience intent, commits
per intent, and keeps failed delivery observable. Delivery retries at most five
times with 5, 10, 20, and 40 minute delays before the terminal `failed`
outbox state. Notification failure never reverses a payment or subscription
transition.

## Pre-rollout assessment

Do this through an approved, least-privileged production inspection channel;
do not copy `backend/.env`, print environment variables, or include order,
tenant, transaction, or credential values in tickets/logs. First establish
whether the production rows named in the baseline include WeChat orders and
whether any require action. The following aggregate-only PostgreSQL query is
safe to adapt to that approved channel:

```sql
SELECT provider, status, recovery_state, count(*) AS orders
FROM payment_orders
GROUP BY provider, status, recovery_state
ORDER BY provider, status, recovery_state;

SELECT provider, count(*) AS stale_pending_orders
FROM payment_orders
WHERE status = 'pending'
  AND expires_at <= now() - interval '5 minutes'
GROUP BY provider;

SELECT provider, recovery_reason_code, count(*) AS manual_recovery_orders
FROM payment_orders
WHERE recovery_state = 'manual_recovery'
GROUP BY provider, recovery_reason_code;
```

Record only the aggregate result and the planned operator action. A stale
WeChat `pending` order is a candidate for the normal recovery executor; a
`manual_recovery` or reconciliation-mismatch finding remains an operator
review item. Do not alter an order directly in SQL and do not create a test
payment merely to exercise the timer.

Before enabling the notification timer, verify the existing production
`ADMIN_DOMAIN`, transactional email transport (see [`.env.example`](../../.env.example)), active owner/operations recipients, and
migration head through their normal controlled checks. Missing transport or a
recipient is intentionally retried and reported as a fixed failure code, never
reported as sent.

## Rollout and execution evidence

1. Merge the reviewed GH-106 change only after the normal CI gate. The next
   approved production deploy installs the eight managed files and enables the
   four timers. A deploy may warn rather than fail if the server's step-10
   sudoers grant is absent, so deployment success alone is not evidence.
2. Operations verifies each timer is `enabled` and active, and that each
   service file has the expected deployed `ExecStart`:

   ```bash
   sudo systemctl status \
     wecom-billing-lifecycle.timer \
     wecom-billing-notifications.timer \
     wecom-payment-recovery.timer \
     wecom-payment-reconciliation.timer --no-pager
   sudo systemctl list-timers --all \
     'wecom-billing-*' 'wecom-payment-*'
   sudo systemctl cat \
     wecom-billing-lifecycle.service \
     wecom-billing-notifications.service \
     wecom-payment-recovery.service \
     wecom-payment-reconciliation.service
   ```

3. Wait for a normal cadence (or perform an explicitly approved one-shot run)
   without creating a payment. Capture only the identifier-free completion
   lines from journald:

   ```bash
   sudo journalctl --since '30 minutes ago' --no-pager \
     -u wecom-billing-lifecycle.service \
     -u wecom-billing-notifications.service \
     -u wecom-payment-recovery.service \
     -u wecom-payment-reconciliation.service
   ```

   Required successful evidence is one `[INFO] billing_lifecycle
   batch_completed` line, one `[INFO] billing_notifications
   processing_completed` line, and both `[INFO] payment_recovery
   processing_completed mode=recovery` and `mode=reconciliation` lines. The
   aggregate counters (`claimed`, `recovered`, `pending`, `manual_recovery`,
   `failed`) are the recovery/reconciliation outcome telemetry. `claimed=0`
   is a successful empty batch, not evidence that a stale-order recovery was
   exercised; attach aggregate pre-rollout assessment plus the observed
   outcome when such an order exists.

4. Preserve the sanitized command output and aggregate counts as deployment
   evidence. Never attach raw journal lines containing configuration, payment
   references, customer data, or secrets.

## Disable and rollback

If a batch misbehaves, stop future starts first, then stop any active oneshot
services and inspect the sanitized journal output:

```bash
sudo systemctl disable --now \
  wecom-billing-lifecycle.timer \
  wecom-billing-notifications.timer \
  wecom-payment-recovery.timer \
  wecom-payment-reconciliation.timer
sudo systemctl stop \
  wecom-billing-lifecycle.service \
  wecom-billing-notifications.service \
  wecom-payment-recovery.service \
  wecom-payment-reconciliation.service
```

A stopped recovery worker can leave its already-persisted claim lease in place
for up to four minutes; allow that lease to expire rather than clearing it in
SQL. Do not downgrade billing/notification migrations during this rollback;
retain durable facts, outbox history, findings, and audit rows. Revert code
only through the standard approved deployment path. Re-enable timers only
after the failure is understood and the same pre-rollout assessment is
repeated.
