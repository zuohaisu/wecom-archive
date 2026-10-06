# Transactional email: Resend cutover and explicit rollback (GH-148)

## Boundary and evidence

Normal application email uses `app.email` → Resend HTTPS API. SMTP is available
only when an operator explicitly selects `EMAIL_PROVIDER=smtp`; provider errors
never silently fall back to QQ. This document is a runbook, **not evidence that
DNS, credentials, production configuration or inbox delivery have been verified**.
No production hosts, mailbox contents or secrets were accessed for this change.

Repository inventory:

| Producer / caller | Existing business semantics retained |
|---|---|
| `routers/auth.py`: password recovery | Persists hashed reset token before sending; public response does not enumerate accounts. Failed delivery is logged, never claims provider acceptance. |
| `routers/users.py`: administrator reset | Sender failure returns the existing service error; token semantics unchanged. |
| `routers/auth.py`: individual/batch invitations | Pending account/invitation persists before email; a repeat invitation reuses its token. |
| `routers/platform.py`: tenant provisioning | Existing owner invite result reports success/failure separately from tenant creation. |
| `services/platform_accounts.py`: operator invitation | Existing requirement makes invitation delivery transactional; failure rolls back the invitation. This intentional exception is unchanged. |
| `services/export_jobs.py`: export ready | Durable notification state, row lock, bounded attempt count; provider failure does not undo a ready artifact. |
| `services/billing_notifications.py`: payment, security/payment-channel, refund, subscription lifecycle | Durable intents/attempts, row lock, five attempts and exponential delay; delivery failure does not undo subscription/payment/refund facts. |
| `deploy/systemd/wecom-job-failure-alert@.service` | Calls `ssl-renew/notify.sh`, a webhook, not email. |
| `scripts/backup_once.sh`, `scripts/disk_usage_check.sh`, SSL renewal | Existing `ssl-renew/notify.sh` webhook/log alerts, not personal SMTP; unchanged. |
| `scripts/dr_pull.sh` / DR helpers | No direct SMTP sender found in repository search. External deployed tooling remains unverified. |

Repository searches found SMTP delivery only inside `app.email`. The four
template-producing entry points now share one transport. No routes, database
schema, dependencies, CI/CD files or systemd settings are added or modified.

## Configuration and readiness

Use the existing ignored `backend/.env`/approved secret provisioning path. Do not
put a real key in an issue, command argument, screenshot, shell history or PR.

| Variable | Meaning |
|---|---|
| `EMAIL_PROVIDER` | `resend` by default; `smtp` only during explicit rollback. Unknown values fail closed. |
| `RESEND_API_KEY` | Secret, required for Resend; use a sending-only key scoped to the verified domain where supported. |
| `EMAIL_FROM` | Owner-approved default `康冠时代会话存档 <notifications@mail.crowntime.cn>`; single mailbox under exactly `mail.crowntime.cn` required on Resend path. |
| `EMAIL_REPLY_TO` | Empty by default; omit Reply-To. Only set to an explicitly approved monitored address. Sending-domain verification does not make notifications a monitored human inbox. |
| `APP_ENV` | `production` on production. Other values prepend `[NON-PRODUCTION]` to the subject. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Retained only for explicit rollback. Complete authenticated implicit TLS configuration required; port defaults to 465. |

The sender reads `EmailSettings` from environment, as before. The settings UI's
database SMTP fields did not feed the old sender and still do not feed this
sender. Resend credentials are deliberately not exposed as tenant-editable UI
configuration; do not mistake a successful settings-page save for runtime
readiness. Changing the deployed environment requires the normal approved
service restart/deployment procedure; nothing in this ticket performs it.

`email_delivery_ready()` validates local configuration only; it does not prove
remote domain verification or inbox delivery. Missing key, invalid sender,
unknown provider, or invalid settings returns false; no console-preview success.
Export request readiness uses this provider-neutral check. Billing attempts
record `transport_unconfigured` when the local check fails.

## Delivery, privacy and duplication boundaries

- Fixed `https://api.resend.com/emails`, no redirects or ambient HTTP proxies.
- One attempt per invocation, HTTPX timeout 10 seconds per timeout phase,
  no SDK/transport retry and no automatic provider fallback. This is not an
  absolute wall-clock deadline; HTTPX phase timeouts and DNS behaviour apply.
- Accept success only for HTTP 200 with a UUID provider ID. Logs record a fixed
  outcome, status code on rejection, or validated provider ID on acceptance.
  Never log recipient, message, raw response, exception text, traceback or key.
- HTTPX/httpcore wire loggers are filtered in the active send context only;
  unrelated HTTP traffic retains its diagnostics. Do not add request tracing
  middleware that dumps authorization or message bodies.
- A SHA-256 hash of the operation identity and normalized payload forms the
  idempotency key without exposing plaintext identity, token or recipient.
  Billing uses the durable intent ID, stable across worker retries/restarts;
  different intents get different keys even with identical message bodies.
  Each explicit invitation send creates a fresh operation ID while preserving
  the pending user's token. Technical invitation retries must explicitly reuse
  that operation ID; the current invitation path has no automatic retry or
  durable retry queue. Reset/export messages retain payload-derived identity
  (their action token/export expiry distinguishes the operation). Do not print
  keys unnecessarily. Payload/config changes can change a retry's key.
- A timeout means **unknown acceptance**, not definitely unsent. Existing billing
  and export state/retry budgets remain the source of truth. Resend idempotency
  expires after 24 hours; a delayed retry beyond that window can duplicate an
  accepted-but-unrecorded email. Do not reset attempts/replay queues blindly,
  especially after a provider switch or template/config change.
- SMTP rollback has no provider idempotency guarantee. Row locks and durable
  sent states prevent normal duplicates but cannot guarantee exactly-once
  delivery across the remote-accept/local-commit gap.

## DNS plan — Owner approval required before mutation

1. Capture a sanitized record inventory and health baseline for existing Tencent
   Enterprise Mail: root MX, SPF, DKIM selectors, DMARC and any subdomain policy.
   Verify `hs@crowntime.cn` still sends/receives through Tencent. Do not expose
   mailbox contents or DNS-provider credentials.
2. Add **only `mail.crowntime.cn`** as a sending domain in Resend. Keep receiving
   disabled unless separately approved. Do not add the root domain or migrate
   human mailboxes.
3. Copy the exact DNS names/types/values/region supplied by the Resend dashboard.
   Do not invent SPF/DKIM/MX values. Prepare a mutation table with old value,
   new value, TTL, purpose, approval and rollback for each record **before** edits.
4. Add only the approved Resend sending authentication records at its specified
   subdomain names. Any sending/return-path MX is not a root mailbox migration.
   Stop if a proposed change collides with existing records or touches root
   MX/SPF/DKIM; compatibility proof and separate approval are required.
5. Review inherited root DMARC policy. If needed, propose a dedicated
   `_dmarc.mail.crowntime.cn` policy; select reporting destination and policy with
   the Owner. Do not blindly weaken root DMARC or immediately enforce a new
   rejection policy without alignment evidence.
6. Wait for Resend verification; record sanitized domain status and time. Send
   a controlled email after authorization and verify SPF/DKIM and DMARC alignment
   in received headers. Then repeat the Tencent human-mail baseline check.

DNS rollback: restore exactly the prior subdomain records/TTL in the approved
mutation table, removing only records added for this change. Never overwrite
root-domain mail records. There is no generic destructive rollback command.

## Controlled rollout — do not run without separate authorization

1. Pass offline checks and review the final diff. Prepare keys securely, complete
   sending-domain verification and approve the Reply-To policy. The sender
   display name is already Owner-approved: `康冠时代会话存档`.
2. The existing nonproduction deployment policy prohibits SMTP and production
   credentials. **Do not bypass or modify that deployment policy here.** Arrange
   an Owner-approved isolated test process/environment with a sending-only key,
   `APP_ENV=staging`, no production database/archive credentials, and an
   Owner-controlled recipient. Prefer the same `app.email` boundary and synthetic
   body; do not use customer addresses or password-reset secrets in smoke tests.
3. Record accepted provider ID, received From/Reply-To, `[NON-PRODUCTION]` marker,
   auth results and inbox/spam outcome. An API success alone is insufficient.
4. Before first deployment of this code, provision complete Resend configuration
   through the approved path **or explicitly retain `EMAIL_PROVIDER=smtp`** until
   ready. The new default will not infer SMTP from old fields. A rollout without
   either configuration intentionally fails closed and blocks export requests.
5. Obtain explicit production cutover/deployment approval. Snapshot old secret
   references/config securely, set Resend environment, use approved deployment/
   restart procedure across API and notification/export worker processes. No
   infrastructure or service edits are performed by the dev agent.
6. Send one approved synthetic production notification to an Owner-controlled
   recipient. Check From identity, Reply-To policy, auth alignment, API outcome,
   inbox arrival and unchanged Tencent human mail. Check normal runtime selector
   is Resend in every email-producing process; verify no new normal SMTP sends
   during the agreed observation period using sanitized logs/provider counters.
7. Record the explicit short rollback-window deadline and Owner. After stability
   approval, securely remove obsolete QQ credential references from all relevant
   runtime secrets/configurations. Do not remove unrelated business mailboxes.

## Rollback

Trigger on sustained provider rejection/network problems or failed authenticated
delivery. Obtain explicit approval; assess unknown accepted sends before replay.
Set `EMAIL_PROVIDER=smtp` and restore the secured complete legacy SMTP environment
through the same approved deployment/restart path across all producers. Only one
transport is selected at a time. Preserve durable sent/attempt states and budgets;
do not reset intents or retry counts. Verify one Owner-approved synthetic send
and report return to a personal sender as a temporary degraded state, with a
deadline to return to Resend. Automated fallback is deliberately absent.

Offline tests verify explicit SMTP selection, TLS timeout, rejection/error
handling, and no Resend calls on rollback; actual SMTP delivery is unverified.

## Observability limitation and follow-up

Webhook ingestion is deferred: this repository has no canonical email event
ingestion boundary and this ticket does not add a new public webhook route.
Current `sent` notification state means provider accepted, not delivered.
Resend's dashboard is the interim source for delivered/bounced/complained events;
ops must review failures and complaints during rollout and the observation period.

Before materially increasing volume, scope a follow-up for signature-verified
delivery/bounce/complaint webhook ingestion, deduplication of event IDs, provider
ID correlation, safe bounded retention, and suppression/alert handling. Do not
silently reinterpret billing or archive state based on untrusted webhook input.

Official references reviewed 2026-10-06:

- https://resend.com/docs/api-reference/emails/send-email
- https://resend.com/docs/dashboard/domains/introduction
- https://resend.com/docs/dashboard/emails/idempotency-keys
- https://resend.com/docs/webhooks/introduction

## Evidence checklist (pending unless explicitly recorded)

- [ ] Resend domain verified and DNS mutation table/rollback approved.
- [x] Sender display name approved. Owner-approved sender display name: 康冠时代会话存档
- [ ] Reply-To policy approved.
- [ ] Owner-controlled isolated nonproduction send arrived with aligned headers.
- [ ] Production cutover authorized and applied through approved deployment.
- [ ] Controlled production send arrived; normal QQ sender absent after cutover.
- [ ] Tencent human mail unchanged and healthy.
- [ ] Observation window complete; rollback credentials decommissioned or expiry recorded.

Keep ticket-specific validation/evidence under ignored `tasks/GH-148-*.md`.