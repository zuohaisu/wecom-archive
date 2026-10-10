# Production Scheduled Workload Manifest (GH-104)

**Purpose.** GH-104 closed the gap the 2026-08-28 server infrastructure
baseline (Ops record, PR #103; captured in git history) found between three inventories that used to drift independently:

1. the repository's own `deploy/systemd/*.service|*.timer|*.path` files;
2. `deploy/systemd/MANAGED_UNITS`, the plain allowlist
   `scripts/deploy_server.sh` step 10 actually installs/enables from;
3. what was really installed/enabled/active on the production host.

**The one authoritative source is now
[`deploy/systemd/WORKLOAD_MANIFEST`](../../deploy/systemd/WORKLOAD_MANIFEST)**.
It classifies every production-relevant unit as `required`, `deferred`,
`manual-oneshot`, `deprecated`, `static-helper`, `template`, or
`out-of-scope`, and records whether standard deployment auto-installs and
auto-enables it. `MANAGED_UNITS` is a mechanical view of that manifest's
`auto_install=true` rows — `scripts/assert_scheduled_workloads.sh` and
`backend/tests/test_gh104_scheduled_workload_reconciliation.py` both fail
if the two ever disagree. Do not hand-edit `MANAGED_UNITS` without also
updating the manifest.

This document is the human-readable canonical table; the manifest file is
the machine-checkable source of truth. If the two ever disagree, the
manifest wins and this document is stale — file a fix.

## Canonical table

| Workload | Classification | Trigger | Production auto-enabled | Reason |
|---|---|---|---|---|
| Archive worker | Required | timer (fallback; callback is primary) | Yes | Primary sync/decrypt path; already managed pre-GH-104. |
| External-contact daily reconcile | Required | timer, daily 04:15 | Yes | Full metadata + avatar-sync backstop; already managed pre-GH-104. |
| External-contact incremental refresh | Required | **path** (primary) + timer (fallback) | Yes | See "External-contact refresh vs. reconcile" below. |
| Media event wake-up | Required | **path** (primary) | Yes | See "Media event vs. media-download timer" below. |
| Generic media download/reconciliation | Required | timer, every 30 min (fallback) | Yes | Canonical RND-343 cadence; must not regress to the old 5-minute polling observed on production. |
| Export jobs | Required | timer, every 5 min | Yes | ZIP generation, notification retry, 7-day cleanup. See "Export jobs storage" below. |
| AI KB reindex | Required | timer, hourly :15 | Yes | No-op if `AI_SUPPORT_ENABLED` unset. |
| AI KB eval | Required | timer, daily 05:00 | Yes | Retrieval-quality launch gate. |
| AI KB gap report | Required | timer, weekly Mon 06:00 | Yes | Docs-only report; never touches GitHub Issues. |
| AI retention sweep | Required | timer, daily 03:30 | Yes | Deletes AI chat data past `AI_RETENTION_DAYS`. Distinct from the deferred public sweep below. |
| Billing lifecycle/notifications | Required | timer, every 5 min | Yes | GH-106 commercial loop; already managed pre-GH-104. |
| Payment recovery/reconciliation | Required | timer | Yes | GH-106; never creates new checkout; already managed pre-GH-104. |
| Backup | Required | timer, daily 03:17 | Yes (newly) | Already running on production; was "required but unmanaged" — GH-104 closes that drift only. Cadence/retention/encryption unchanged; #105 owns off-host/offsite DR. |
| Disk/resource usage check | Required | timer, every 15 min | Yes (newly) | Same "required but unmanaged" drift as backup. Cadence unchanged. |
| Job-failure alert (`@` template) | Static helper | `OnFailure=` on demand | Installed, never enabled | GH-107; systemd instantiates it, it is never `enable`d/`start`ed by name. |
| Production wildcard SSL renewal | Required | timer | Yes | Captured from production (GH-104 Follow-up B) — see [wildcard-ssl-renewal.md](https://github.com/zuohaisu/wecom-archive/wiki/Wildcard-SSL-Renewal). |
| Qiniu Kodo per-domain SSL template | Template | timer (per instance) | No | Not currently instantiated on production; kept for a future single-domain Qiniu CDN cert. Distinct from, not superseded by, the wildcard flow. |
| Internal reachability check | Deferred | timer, daily 04:30 | No | Manual/deferred per PM decision; internal reachability RECONCILIATION, a different responsibility from the external uptime workflow (paused separately — see below). |
| Message purge (recycle bin) | Deferred, **destructive** | timer, daily 02:45 | No | Retention-policy activation not yet approved. |
| Message cleanup (bulk) | Deferred, **destructive** | timer, every 10 min | No | Same unapproved-policy gate as message purge. |
| AI public retention sweep | Deferred | timer, daily 04:00 | No | Exists in repo; manual/deferred pending explicit policy activation. Not destructive, but still gated. |
| Thumbnail backfill | Manual one-shot | none (no `[Install]`) | No | RND-207 historical backfill; run by hand in controlled batches. |
| Telegram relay | Deprecated | — | No (not in repo) | See "Telegram relay" below. |
| Non-production web service | Out of scope | — | N/A | Owned by the separate controlled non-production deployment (`deploy-nonprod.yml`). |
| Main production web service | Out of scope | — | N/A | See "Main web service" below. |

## External-contact refresh vs. reconcile

- **`wecom-external-contact-refresh`** is the **incremental, low-latency**
  path: a producer enqueues a bounded, tenant-scoped refresh task and
  touches a coalescing mtime-only `.path` signal. The `.path` unit is the
  **primary** trigger. The `.timer` (`OnBootSec=10min`,
  `OnUnitInactiveSec=15min`) is a **fallback/recovery** mechanism only —
  it recovers tasks after a restart or a missed/failed path wake-up, it is
  not a competing low-latency path and must not be re-tuned into one.
- **`wecom-external-contact-reconcile`** is the **daily full**
  reconciliation (04:15) that also drives avatar sync (RND-371). It
  backstops the incremental path but runs far less often.
- The worker (`backend/scripts/refresh_external_contacts_once.py`) is
  idempotent, bounded (`EXTERNAL_CONTACT_REFRESH_BATCH_SIZE`, default 25),
  tenant-scoped (each tenant's failure is isolated and rolled back
  independently), and retry-safe by construction — it only claims a
  bounded batch of persisted, durable task rows per invocation.
- GH-104 did not clear the pending-task backlog observed in the 2026-08-28
  baseline (99 rows and growing) by any database mutation. Restoring the
  managed trigger lets the existing worker drain it in bounded batches
  over time — see the Rollout Plan for what "bounded drain" evidence looks
  like.

## Media event vs. media-download timer

- **`wecom-archive-media-event.path`** is the **primary** low-latency
  trigger: the archive worker touches this coalescing mtime-only signal
  only after a successful commit left pending media, and the event
  service it wakes invokes the exact same generic media-worker CLI as the
  timer below — there is no second downloader and no way for the two
  triggers to create duplicate media records.
- **`wecom-archive-media-download.timer`** is the **fallback/recovery**
  cadence: every 30 minutes (`OnCalendar=*:15/30`), staggered 15 minutes
  off the archive worker's own `:00/:30` cadence. This is the PM-approved
  canonical design (RND-343) — production was observed running an older
  server copy polling every 5 minutes and missing `--retry`/
  `--trigger-source timer`; GH-104's managed-unit sync overwrites that
  stale copy with the versioned 30-minute unit on every deploy.
- The existing media-download lock (`MEDIA_DOWNLOAD_LOCK_PATH`) already
  collapses manual/event/timer overlap safely, so restoring the path
  alongside the timer cannot cause double-processing.
- `deploy/systemd/overrides/wecom-archive-media-download-5min.conf` is an
  **operator-installed emergency rollback drop-in**, not part of the
  managed set. If an operator has it installed as
  `wecom-archive-media-download.timer.d/reconciliation.conf`, it silently
  overrides the base 30-minute cadence even after GH-104's deploy — check
  for it explicitly during rollout (see the Rollout Plan).

## Export jobs storage

The 2026-08-28 baseline found a server-only, untracked
`wecom-export-jobs.service.d/local-export-storage.conf` drop-in forcing
`MEDIA_STORAGE_PROVIDER=local` and a different `ExecStart`. This was left
by the RND-360 incident mitigation and is **already documented** (see
`docs/DEPLOYMENT.md` §5, "Before deploying this change, inspect
`systemctl cat wecom-export-jobs.service`...") as debt that must be
**removed**, not preserved: `_qiniu_export_provider()`
(`backend/app/services/export_jobs.py`) requires the effective worker
environment to resolve `qiniu_kodo`, or new full-media export jobs fail
closed. GH-104 does not invent a second config source for this — the
canonical configuration is `backend/.env` alone (`EnvironmentFile=` in the
versioned unit). The Rollout Plan below includes removing this drop-in as
an explicit Ops step, and `scripts/assert_scheduled_workloads.sh` treats
`wecom-export-jobs.service` as a normal required unit with no drop-in of
its own recorded in the manifest — its presence in production would be
drift, not intended state.

## Destructive retention workloads

`wecom-message-purge` and `wecom-message-cleanup` are destructive
(permanent deletion) and remain in the repository intentionally —
presence in `deploy/systemd/` is not the same thing as approval to run in
production. `WORKLOAD_MANIFEST` flags both `destructive=true`, which
structurally bars `auto_install`/`auto_enable` from ever being `true` for
them (enforced by `scripts/assert_scheduled_workloads.sh` and the GH-104
test suite) regardless of any future manifest edit. A post-deploy
assertion that finds either enabled in production must **FAIL**, not
warn — and their absence must never be treated as a deployment or
assertion failure. See [Message Deletion](https://github.com/zuohaisu/wecom-archive/wiki/Message-Deletion) for the
retention-policy approval this classification is gated on.

`wecom-ai-public-retention-sweep` is deferred for the same
"exists-but-not-approved" reason, though it is not classified destructive
(it sweeps AI chat session data, not archived customer messages).

## Internal reachability vs. external uptime monitoring

These are two different responsibilities and must not be conflated:

- **`wecom-archive-reachability-check`** is an internal reachability
  **reconciliation** job (deferred/manual; see
  `docs/reachability_automation_runbook.md`).
- **`.github/workflows/uptime-check.yml`** is the **external** uptime
  monitor that runs outside the ECS host. PM separately paused its
  schedule trigger (see the `ops: pause scheduled external uptime checks`
  commit) — that decision does not change the reachability-check
  classification, and GH-104 does not re-enable either.

## Wildcard SSL renewal

**Status: captured (GH-104 Follow-up B).** Production renews the
`*.crowntime.cn` wildcard certificate with
`qiniu-ssl-renew-wildcard.service`/`.timer` plus `ssl-renew/renew-wildcard.sh`
— until Follow-up B, this script had never been committed to this
repository (confirmed via `git log --all` — zero history prior to
capture), a real reproducibility gap this PR closes, not an oversight
carried forward. It was introduced during the 2026-08-03 production domain cutover
(runbook since moved out of the tree; see GH-203) as a hand-authored,
already-field-proven script (41/41 successful runs observed in the 30
days before capture) that performs `acme.sh` DNS-01 renewal for the
wildcard domain, binds it to Qiniu's `media.crowntime.cn` (CDN) and
`media-origin.crowntime.cn` (origin) domains, deploys the resulting
certificate under `shared/certs/wildcard.crowntime.cn/`, and reloads
nginx through a narrowly-scoped sudoers grant. Full behavior contract
(including a known production defect that currently makes every failure
path — CDN bind, origin bind, and nginx deployment alike — a hard,
uninformative stop rather than the graceful degradation it was designed
for), environment variables, sudoers dependency, and manual verification
commands are in [wildcard-ssl-renewal.md](https://github.com/zuohaisu/wecom-archive/wiki/Wildcard-SSL-Renewal) — this
section stays a summary.

This is conceptually **different** from `deploy/systemd/qiniu-ssl-renew@.service`
(the repo's existing template): that template renews a **single Qiniu
Kodo CDN custom domain** and binds the cert through Qiniu's own HTTPS API
— it does not touch nginx and cannot express a wildcard SAN (its
`validate_domain` in `ssl-renew/lib/common.sh` rejects a leading `*.`
label by design). Follow-up B captured the real, already-proven
production script rather than repurposing this template — see
the production-parity `ssl-renew/renew-wildcard.sh` and
[the wildcard renewal behavior contract](https://github.com/zuohaisu/wecom-archive/wiki/Wildcard-SSL-Renewal).

`WORKLOAD_MANIFEST` now carries this as `repo_status=present`,
`auto_install=true` for both units, `auto_enable=true` for the timer
only — the same shape as every other required workload:

```
qiniu-ssl-renew-wildcard.service|required|present|true|false|oneshot|false|...
qiniu-ssl-renew-wildcard.timer|required|present|true|true|timer|false|...
```

One residual, explicitly-tracked deployment dependency remains: standard
deployment's `systemctl enable --now` sudoers grant is documented as
scoped to `wecom-*.timer`/`wecom-*.path` — `qiniu-ssl-renew-wildcard.timer`
needs one additional, **exact** (non-glob) sudoers line before the first
automated enable can succeed. See this PR's rollout plan and
`MANAGED_UNITS`' own comment for the precise grant. Until that grant
exists, `scripts/deploy_server.sh` degrades to a non-fatal WARN for this
one unit — identical to every other unit's missing-sudoers behavior — it
does not block or roll back the rest of the deploy.

## Qiniu Kodo per-domain SSL template

`qiniu-ssl-renew@.service`/`.timer` is kept as a `template`-classified
row, not deleted: the 2026-08-28 baseline found no active
`qiniu-ssl-renew@<domain>.timer` instance on production. GH-104 Follow-up B
confirmed why — `media-origin.crowntime.cn` (the domain this template was
originally built for) is now bound to the wildcard certificate by
`ssl-renew/renew-wildcard.sh` itself (see
[wildcard-ssl-renewal.md](https://github.com/zuohaisu/wecom-archive/wiki/Wildcard-SSL-Renewal)), so no per-domain
instance of this template is currently needed. The template remains a
valid, tested mechanism for a future single-domain Qiniu CDN certificate
outside the wildcard's coverage. Deleting it would be an irreversible,
unforced move; marking it clearly template-only here is sufficient to
prevent it being confused with the wildcard flow.

## Telegram relay

`qiniu-telegram-relay.service` is **not present in this repository** and
never has been. The 2026-08-28 baseline found it server-only, disabled,
never run, with a missing entrypoint. It is classified Deprecated:
GH-104 does not "fix" the missing entrypoint or resurrect it — the repo
already correctly declares nothing for it, and
`scripts/assert_scheduled_workloads.sh` / the GH-104 test suite assert no
`telegram` string exists anywhere in `deploy/systemd/`,
`MANAGED_UNITS`, or `WORKLOAD_MANIFEST`, so it cannot silently re-enter
deployment through this repository. Removing the stale artifact from the
server itself is an Ops rollout action, not a repository change.

## Main web service

`wecom-archive-365.service` (the FastAPI app under uvicorn) is
intentionally **not** part of this scheduled-workload manifest. Its
lifecycle — restart, health-gate, automatic rollback — is owned entirely
by `scripts/deploy_server.sh`'s own steps 6-8, a fundamentally different
contract (a long-running service restarted and gated on every deploy, not
a periodic/event-driven one-shot job installed once and left running).
The unit file itself is also not versioned in this repository at all —
see `docs/DEPLOYMENT.md` §1 and §4. Folding it into
`WORKLOAD_MANIFEST`/`MANAGED_UNITS` would conflate two different
deployment mechanisms; it stays listed here as `out-of-scope` only so its
absence is legible rather than an apparent oversight.

`wecom-archive-365-nonprod.service` is real, versioned, and does appear
in `deploy/systemd/` — but it is deployed exclusively by the separate,
manually-dispatched `deploy-nonprod.yml` workflow guarded by the
`non-production` GitHub Environment, never by the
production scheduled-workload sync this manifest governs.
