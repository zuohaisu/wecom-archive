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
installs a process-wide `LogRecord` factory from `app.__init__`, before any
application entrypoint creates an outbound HTTP client. The factory composes
with any existing factory and scrubs query credentials from the final message,
deferred `%s`/mapping arguments, stack info, and URL-bearing exception text
before a logger propagates to a handler or a formatter writes to a sink.

It covers application, `httpx`, `httpcore`, and other normal Python logger
records without changing their levels. The investigated outbound URL keys are
WeCom `access_token`, `corpsecret`, and `suite_access_token`, plus Qiniu's
short-lived signed-download `token`; key matching is case-insensitive. A
handler-compatible filter reuses the same primitive where an embedding process
needs an explicit handler filter.

The helper remains explicitly called at `app/main.py`,
`app/services/wecom_org_authorization.py`, and
`app/services/external_contact_sync.py:main()` as an idempotent guard for
embedded/import-order-customized processes.

**What still gets logged (operator diagnosability):** `main()` keeps its
existing structured summary line
(`external_contact_reconcile status=completed tenant_count=... total=...
inserted=... failed=...`) and per-tenant failure warnings
(`external_contact_reconcile tenant=%s result=failed
error_class=tenant_processing_failed`). Per-request HTTP diagnostics also
remain available with method, host, path, protocol/status, and non-sensitive
query parameters; only credential values appear as `[REDACTED]`.

**GH-139 exposure-surface audit:**

| Path | HTTP client | Credential transport | URL-log exposure | Central boundary |
| --- | --- | --- | --- | --- |
| External-contact reconcile/refresh, display-name and group-chat sync (`app/wecom_contacts.py`) | httpx | `access_token` query parameter | Yes | Redacted |
| WeCom token mint and OAuth user lookup (`app/auth.py`, `app/routers/auth.py`) | httpx | `corpsecret` and `access_token` query parameters | Yes | Redacted |
| Third-party organization authorization | httpx | `suite_access_token` query parameter | Yes | Redacted |
| Qiniu internal signed reads | httpx | signed `token` query parameter | Yes | Redacted |
| Avatar fetch | httpx | no configured credential query; a provider-signed `token` URL is still covered | Possible | Redacted |
| WeChat Pay, Alipay, DeepSeek | httpx | Authorization header or request body, not a credential query | No URL credential identified | Headers/bodies are not emitted by normal httpx request summaries; existing exception paths log a class/fixed message |
| Branding DNS-over-HTTPS | httpx | none | No | Not applicable |
| WeCom archive/media SDK | C SDK | SDK arguments, not a Python HTTP URL | No | Not applicable |
| Self-service activation E2E helper | requests | no credential query; not a production systemd entrypoint | No identified production path | Not applicable |

No `aiohttp`, `urllib`, or `urllib3` application call site was found. The
application-side exception handlers in this surface continue to log only
`type(exc).__name__` or a fixed message; the centralized boundary also handles
URL-bearing exception text before a handler can format it.

Tests: [`backend/tests/test_gh107_log_safety.py`](../../backend/tests/test_gh107_log_safety.py).

### GH-139 historical-exposure disposition

A read-only, value-free production audit before the GH-139 rollout found the
following retained matches. It emitted counts and dates only, never matching
log text or credential values:

- `wecom-external-contact-reconcile.service` journal records: 124,664 retained
  query-secret matches (2026-08-18 through 2026-08-27 UTC).
- rsyslog-managed `/var/log/messages-*`: 222,467 retained query-secret matches
  from 2026-08-05 and 2026-08-18 through 2026-08-28 local log dates.
- The affected producer was the external-contact reconciliation path. The
  journal uses `SystemMaxUse=500M` and `MaxRetentionSec=14day`; rsyslog is
  active and uses the system's weekly, four-rotation policy for its local
  messages files. No active journal-upload/log-shipper service or active
  rsyslog forwarding action was found. The versioned application backup script
  backs up database/media artifacts, not host logs; cloud/host snapshots remain
  an Ops confirmation item.

**Rotation decision: ROTATION REQUIRED.** The pre-GH-107 request URLs exposed
short-lived access tokens and the long-lived WeCom `corpsecret` values used by
the external-contact and OAuth token-mint paths. A WeCom owner must rotate the
corresponding configured app secrets, update the protected production runtime
configuration through the approved secret channel, and restart/deploy without
putting either old or new value in a command, ticket, PR, or log. The agent
must not perform that console/configuration action.

**Log-handling decision:** after rotation, an authorized Ops owner must choose
between early removal of the identified rotated rsyslog files plus affected
journal archives, or retention until their configured expiry. Selective
redaction is not possible for existing journal/syslog records; early removal
also removes unrelated operational evidence, so it requires explicit approval
and a post-action count-only rescan. Any cloud snapshot or external-log copy
must be handled under its owning retention/deletion process.

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
| `wecom-external-contact-refresh` failure | same | **GH-104 Follow-up A** |
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
| `wecom-external-contact-reconcile.service` | Daily full reconcile failed | `journalctl -u wecom-external-contact-reconcile.service`; the incremental refresh path (`wecom-external-contact-refresh.service`) backstops this between daily runs |
| `wecom-external-contact-refresh.service` | Incremental refresh queue drain failed (GH-104 Follow-up A: a missing sys.path bootstrap previously made every run fail with `ModuleNotFoundError`, silently, with no alert — this OnFailure= wiring closes that blind spot) | `journalctl -u wecom-external-contact-refresh.service`; the daily full reconcile above is the safety net between refresh cycles, not a substitute for fixing the refresh path itself |
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
   the seven updated `OnFailure=` unit files land on the server (steps 7 and
   10 of `scripts/deploy_server.sh`; no manual install step needed since
   all seven are already in `MANAGED_UNITS`).
