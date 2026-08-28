# Production Secrets & Observability Hardening (GH-107)

**Status:** the logging fix (Scope A) and the alert-delivery/OnFailure wiring
(Scope B) are versioned and shipped in this repository. The external
uptime workflow (Scope C) is versioned and runs unconditionally on its
schedule, but its alert leg — and every host-local alert leg described
below — is only as live as the `ALERT_WEBHOOK_URL` value an operator
configures. This document does not claim that value has been set anywhere
yet; see "Outstanding operator action" at the end.

Source: 2026-08-28 Server Infrastructure Baseline
([docs/operations/server-baseline-2026-08-28.md](server-baseline-2026-08-28.md)),
PR #103. DR/off-host backup is tracked separately in #105 and is out of
scope here.

## Scope A — secret-safe logging

**Finding:** `external_contact_sync.main()` (the daily
`wecom-external-contact-reconcile` timer) called
`logging.basicConfig(level=logging.INFO)`. httpx/httpcore's own
request/response logger prints the full request URL at INFO, and WeCom's
API takes `access_token` (and, for the token-mint call, `corpsecret`) as a
query-string parameter rather than a header — so every reconcile run wrote
those secrets to journald verbatim (~11.7k lines/24h observed).

**Fix:** [`backend/app/log_safety.py`](../../backend/app/log_safety.py)
provides `configure_secret_safe_logging()`, which:

- sets the `httpx` and `httpcore` loggers to `WARNING` (their INFO request
  lines are never emitted, regardless of the root logger's level);
- attaches a redacting filter to those two loggers as defense-in-depth, so
  that if either is ever deliberately raised back to INFO/DEBUG for local
  debugging, `access_token=`/`corpsecret=`/`suite_access_token=`/
  `provider_access_token=` values are scrubbed to `[REDACTED]` instead of
  printed;
- is idempotent and safe to call from multiple modules regardless of
  import order.

It is called from every process that talks to WeCom/payment/AI provider
APIs over httpx:

- `app/main.py` (the web process — previously relied on a side effect of
  importing `app.services.wecom_org_authorization`, which happened to run
  first; now explicit);
- `app/services/wecom_org_authorization.py` (unchanged behavior, now
  delegates to the shared helper instead of its own duplicate
  `setLevel` calls);
- `app/services/external_contact_sync.py:main()` (the actual leak site).

**What still gets logged (operator diagnosability):** `main()` keeps its
existing structured summary line
(`external_contact_reconcile status=completed tenant_count=... total=...
inserted=... failed=...`) and per-tenant failure warnings
(`external_contact_reconcile tenant=%s result=failed
error_class=tenant_processing_failed`) — these carry outcome counts and a
tenant tag, never a token or raw contact/message payload. Per-request
httpx lines were the only thing suppressed.

**Reviewed for the same integration surface (no equivalent leak found):**
`app/wecom_contacts.py`, `app/auth.py` (`get_wecom_token`),
`app/routers/auth.py`, `app/services/wecom_org_authorization.py`,
`app/services/wechat_pay.py`, `app/services/alipay.py`,
`app/services/avatar_sync.py`, `app/services/branding.py`,
`app/services/ai/llm_provider.py` all call httpx with a secret in `params`
or none at all, and every exception handler in this surface already logs
only `type(exc).__name__` or a fixed message — never `str(exc)` — so an
httpx exception's own string form (which, like its request log line,
includes the full URL) cannot reach a log line either.

Tests: [`backend/tests/test_gh107_log_safety.py`](../../backend/tests/test_gh107_log_safety.py).

## Scope B — host-local alert delivery

The alert *transport* already existed before GH-107
(`ssl-renew/notify.sh`, documented in
[2c2g-runbook.md](2c2g-runbook.md)): POST a JSON payload
`{domain, hostname, failed_stage, timestamp, error_summary}` to
`ALERT_WEBHOOK_URL`; if that variable is unset, log a `[DEGRADED]` line and
exit 0 rather than fail the caller. `lib/common.sh`'s `filter_secrets`
redacts known secret-shaped tokens from any message before it reaches a
log line or the webhook payload. What GH-107 adds is coverage for
scheduled-workload failures, which nothing previously alerted on:

| Alert source | Mechanism | Status |
| --- | --- | --- |
| Disk / inode threshold | `scripts/disk_usage_check.sh` → `notify.sh` | pre-existing |
| Memory / swap pressure | `scripts/disk_usage_check.sh` → `notify.sh` | pre-existing |
| PostgreSQL connection/availability | `scripts/disk_usage_check.sh` → `notify.sh` (reports `DEGRADED`, not a false `WARN`, when unreachable) | pre-existing |
| Backup job failure | `scripts/backup_once.sh` → `notify.sh` | pre-existing |
| `wecom-archive-worker` failure | `OnFailure=` → `wecom-job-failure-alert@.service` → `notify.sh` | **GH-107** |
| `wecom-external-contact-reconcile` failure | same | **GH-107** |
| `wecom-billing-lifecycle` failure | same | **GH-107** |
| `wecom-billing-notifications` failure | same | **GH-107** |
| `wecom-payment-recovery` failure | same | **GH-107** |
| `wecom-payment-reconciliation` failure | same | **GH-107** |

`deploy/systemd/wecom-job-failure-alert@.service` is the standard systemd
"failure notification" recipe: a templated oneshot unit, listed in
`MANAGED_UNITS` (so CD deploys it) but never `enable`d itself — systemd
instantiates `wecom-job-failure-alert@<unit-name>.service` on demand when
a unit with `OnFailure=wecom-job-failure-alert@%n.service` fails, and it
calls `notify.sh ERROR <unit-name> "systemd unit failed — see journalctl
-u <unit-name> for detail"`. It never puts journal content or credentials
in that message, only the unit name.

Disk/backup checks were deliberately left alone (no `OnFailure=` added):
they already call `notify.sh` themselves on their own richer WARN/failure
paths; an `OnFailure=` alert would be a second, less specific alert for
the same event.

### Configuring `ALERT_WEBHOOK_URL` (server-side)

Store it in `backend/.env` (production runtime config — the same
approved, gitignored file that already holds every other production
secret) as `ALERT_WEBHOOK_URL=https://...`. Never commit it, never pass it
on a command line that lands in shell history or `ps`, never log it —
`filter_secrets` also strips `ALERT_WEBHOOK_URL=...` specifically if it
ever appears in a captured command's output.

`wecom-job-failure-alert@.service` reads the same `backend/.env` via
`EnvironmentFile=-...` (the leading `-` means a missing file degrades
instead of failing the alert unit itself).

### Testing the alert path without a real outage

```
CURRENT_STAGE=manual-test ./ssl-renew/notify.sh WARN "manual-test" "operator-triggered test alert, not a real incident"
```

Confirm one `[NOTIFY] [OK] webhook delivered (HTTP 2xx)` line and that the
message actually arrives at the configured destination.

### Known compatibility gap — pick an approved destination

`notify.sh`'s payload is a **generic** JSON object. Chat-bot webhooks with
their own required envelope (e.g. a WeCom group-robot webhook expects
`{"msgtype": "text", "text": {...}}`, not this schema) will reject it
outright rather than degrade gracefully. Before pointing
`ALERT_WEBHOOK_URL` at a real destination, either:

- pick a receiver that accepts an arbitrary JSON body (many
  generic-webhook/alerting relays do), or
- put a small relay/transform in front of the actual chat channel.

This decision (which destination, and any relay needed) is a product/ops
choice this document does not make.

## Scope C — external uptime monitoring

[`.github/workflows/uptime-check.yml`](../../.github/workflows/uptime-check.yml)
runs on a GitHub Actions runner — outside the production ECS — on a
`*/10 * * * *` schedule (plus `workflow_dispatch` for an on-demand run or
test alert):

- `GET https://archive.crowntime.cn/` must return HTTP 200 (following
  redirects);
- `GET https://archive.crowntime.cn/health/ready` must return HTTP 200
  (see `app/main.py`'s `_readiness_body` — 503 on a DB outage or
  unmigrated schema, by design, never a bare 200).

On failure it POSTs the same generic JSON schema as `notify.sh` to the
`ALERT_WEBHOOK_URL` **repository secret** (`Settings → Secrets and
variables → Actions` — this is a separate value/store from the server's
`backend/.env`; point both at the same destination if you want one
channel). If that secret is unset, the run still fails (visible in the
Actions tab / any configured GitHub notification), it just cannot also
push an outbound alert.

A scheduled GitHub Actions workflow can be delayed under platform load;
treat this as a fast, low-maintenance check, not a hard SLA-grade monitor.

### Sending a test alert (Scope C)

Actions → "External Uptime Check" → Run workflow → set
`send_test_alert` to `true`. This does **not** touch production; it only
verifies the webhook path end-to-end.

## Operator response runbook

| Alert `failed_stage` | Meaning | First response |
| --- | --- | --- |
| `disk-usage-check` (disk/inode/memory/swap/pg_connections) | A resource threshold was breached, or PostgreSQL was unreachable when checked | `ssh` in, `systemctl status wecom-disk-usage-check.service` / `journalctl -u wecom-disk-usage-check.service -n 50` for the specific failing check; see [2c2g-runbook.md](2c2g-runbook.md) |
| `backup-once` / backup job name | The nightly encrypted backup did not complete | Check `journalctl -u wecom-backup.service`; re-run manually (`scripts/backup_once.sh`) once the underlying cause is fixed; do not let two consecutive nights fail silently |
| `wecom-archive-worker.service` | Sync+decrypt failed for one or more tenants | `journalctl -u wecom-archive-worker.service`; check WeCom credential validity and reachability before assuming a code regression |
| `wecom-external-contact-reconcile.service` | Daily full reconcile failed | `journalctl -u wecom-external-contact-reconcile.service`; the incremental refresh path (`wecom-external-contact-refresh.service`, not yet installed per the 2026-08-28 baseline) is not currently a safety net for this |
| `wecom-billing-*` / `wecom-payment-*` `.service` | A commercial-loop batch failed | Treat as high priority — see [payment-billing-runtime.md](payment-billing-runtime.md); do not manually re-trigger checkout, only investigate the batch job |
| `external-uptime-check` (`public_endpoint_or_readiness`) | Production is unreachable or not ready from outside the ECS | Check `archive.crowntime.cn` reachability yourself; `ssh` in and check `systemctl status wecom-archive-365.service`, `nginx`, and `postgresql`; this is the alert most likely to indicate a full host/network outage rather than a single job failure |

Runbook ownership: whoever owns `docs/operations/*` for this project
today (see `git log` on this file for the most recent editor) is the
default owner of this runbook until the team names a specific on-call
role; this document does not invent a named owner that doesn't exist yet.

## Non-goals

Unchanged from the parent issue: off-host backup/restore (#105), a full
metrics/trace platform, SIEM, HA architecture, or a broad logging refactor
unrelated to secret leakage.

## Outstanding operator action

Code/config in this repository is ready but inert until an operator:

1. picks an approved alert destination compatible with the generic JSON
   payload (or fronts it with a relay);
2. sets `ALERT_WEBHOOK_URL` in production `backend/.env` and confirms a
   manual test alert arrives;
3. adds the same (or a different, equally approved) value as the
   `ALERT_WEBHOOK_URL` GitHub Actions repository secret and runs the
   workflow's `send_test_alert` input once;
4. deploys `main` so `deploy/systemd/wecom-job-failure-alert@.service` and
   the six updated `OnFailure=` unit files land on the server (steps 7 and
   10 of `scripts/deploy_server.sh`; no manual install step needed since
   all six are already in `MANAGED_UNITS`).
