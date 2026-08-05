# WeCom Archive Worker — Event-First Reconciliation Runbook

## Purpose and trigger model

`backend/scripts/run_archive_worker_once.py` remains the **single** archive
entrypoint. It retains the existing `WORKER_LOCK_PATH` `fcntl.flock`, then
runs sync and decrypt unchanged. It is called by:

1. a validated WeCom callback (primary, non-blocking HTTP acknowledgement);
2. the authenticated manual Sync Now path;
3. the low-frequency systemd reconciliation timer;
4. an operator's manual CLI invocation.

A successful run releases the archive lock and then performs a read-only,
bounded preflight for fresh/pending media. Only when that finds work does it
touch one coalescing signal consumed by
`wecom-archive-media-event.path`; that path starts the existing
`backend/scripts/download_wecom_media_once.py` entrypoint with
`--trigger-source archive-complete` in its own systemd service. The callback
handler never parses or downloads media itself.

The media worker owns its own `MEDIA_DOWNLOAD_LOCK_PATH`; it never uses the
archive lock. A lock conflict is a safe no-op and the next media timer run is
the durable compensation path.

## Default schedule (RND-343)

| Worker | Versioned default | Purpose |
|---|---:|---|
| Archive reconciliation | `OnCalendar=*:0/30` (`:00`, `:30`) | Catch missed callbacks, failed dispatches, restarts, and cursor reconciliation |
| Generic media reconciliation | `OnCalendar=*:15/30` (`:15`, `:45`) | Pending/retryable generic media reconciliation |
| External-contact incremental refresh | path signal + `OnUnitInactiveSec=15min` retry | Drain small, persisted metadata-refresh batches |
| External-contact full reconciliation | daily at `04:15` | Correct missed/unreadable/stale customer metadata in resumable batches |

The 15-minute stagger exceeds the required 10-minute offset. Callback-driven
archive work remains the low-latency path; the timer is not removed.

### External-contact identity reconciliation (RND-170)

When `WECOM_EXTERNAL_CONTACT_SECRET` is configured, a validated
`change_external_contact` callback persists a bounded, coalesced refresh task
for that one customer and immediately acknowledges. A successfully decrypted
direct inbound message from an external-user identifier persists the same
task after archive commit. Group messages and messages sent by an archive
seat are deliberately excluded, so a group burst cannot create customer API
work.

The callback and decrypt workers never call the external-contact API. They
only write the durable task row and touch an identifier-free systemd-path
signal. `wecom-external-contact-refresh.service` drains a small batch and
commits every customer independently; unavailable records are retried with a
bounded backoff. `wecom-external-contact-reconcile.timer` performs one daily
full reconciliation at 04:15, with periodic commits, as the fallback. The
30-minute archive timer no longer performs a full contact API sweep.

Never put customer IDs, remarks, nicknames, callback ciphertext, or secrets
in journal queries, tickets, or manual command arguments.

## Shared lock setup

Run once as root:

```bash
sudo mkdir -p /srv/apps/wecom-archive-365/shared/run
sudo chown wecomarchive:wecomarchive /srv/apps/wecom-archive-365/shared/run
sudo chmod 750 /srv/apps/wecom-archive-365/shared/run
```

The archive and media lock files are separate:

```text
/srv/apps/wecom-archive-365/shared/run/wecom-archive-worker.lock
/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock
```

## Installation / upgrade (Ops-owned)

> Production scheduling changes are performed by the operations agent only.
> Preserve the currently installed units before replacing them.

```bash
sudo install -d -m 0750 /srv/apps/wecom-archive-365/shared/rollback/rnd-343
sudo cp -a /etc/systemd/system/wecom-archive-worker.{service,timer} \
  /etc/systemd/system/wecom-archive-media-download.{service,timer} \
  /srv/apps/wecom-archive-365/shared/rollback/rnd-343/
sudo cp -a /etc/systemd/system/wecom-archive-media-event.{service,path} \
  /srv/apps/wecom-archive-365/shared/rollback/rnd-343/ 2>/dev/null || true

sudo cp deploy/systemd/wecom-archive-worker.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-worker.timer /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-media-download.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-media-download.timer /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-media-event.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-media-event.path /etc/systemd/system/
sudo cp deploy/systemd/wecom-external-contact-refresh.{service,path,timer} /etc/systemd/system/
sudo cp deploy/systemd/wecom-external-contact-reconcile.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-worker.timer
sudo systemctl enable --now wecom-archive-media-download.timer
sudo systemctl enable --now wecom-archive-media-event.path
sudo systemctl enable --now wecom-external-contact-refresh.path
sudo systemctl enable --now wecom-external-contact-refresh.timer
sudo systemctl enable --now wecom-external-contact-reconcile.timer
```

The event signal is one mtime-only file under the existing shared `run/`
directory. It contains no identifiers and cannot grow into a queue; database
pending/retryable state remains the task source. The event service and timer
both reuse the same generic media CLI and media lock.

## Safe cadence configuration and emergency rollback

The versioned defaults above are the production recommendation. To restore the
previous 5-minute cadence, install the reviewed drop-ins (they preserve the
old two-minute offset), then reload systemd:

```bash
sudo install -d /etc/systemd/system/wecom-archive-worker.timer.d
sudo install -d /etc/systemd/system/wecom-archive-media-download.timer.d
sudo install -m 0644 deploy/systemd/overrides/wecom-archive-worker-5min.conf \
  /etc/systemd/system/wecom-archive-worker.timer.d/reconciliation.conf
sudo install -m 0644 deploy/systemd/overrides/wecom-archive-media-download-5min.conf \
  /etc/systemd/system/wecom-archive-media-download.timer.d/reconciliation.conf
sudo systemctl daemon-reload
sudo systemctl restart wecom-archive-worker.timer wecom-archive-media-download.timer
```

To return to the versioned 30-minute defaults, remove only those drop-ins and
reload/restart the timers:

```bash
sudo rm -f /etc/systemd/system/wecom-archive-worker.timer.d/reconciliation.conf
sudo rm -f /etc/systemd/system/wecom-archive-media-download.timer.d/reconciliation.conf
sudo systemctl daemon-reload
sudo systemctl restart wecom-archive-worker.timer wecom-archive-media-download.timer
```

For a full operational rollback, disable the new path watcher, restore the
files saved under `shared/rollback/rnd-343/`, remove the newly introduced event
units if they did not exist in the backup, then run `daemon-reload` and restart
both timers. Do not delete either reconciliation timer:

```bash
sudo systemctl disable --now wecom-archive-media-event.path
sudo rm -f /etc/systemd/system/wecom-archive-media-event.service \
  /etc/systemd/system/wecom-archive-media-event.path
sudo cp -a /srv/apps/wecom-archive-365/shared/rollback/rnd-343/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart wecom-archive-worker.timer wecom-archive-media-download.timer
```

## Manual checks

Run the archive entrypoint as `wecomarchive` after sourcing the deployed `.env`:

```bash
sudo -iu wecomarchive
cd /srv/apps/wecom-archive-365/current/backend
source .venv/bin/activate
set -a; source .env; set +a
python scripts/run_archive_worker_once.py
```

Expected safe aggregate lines include sync fetch/insert counts, decrypt counts,
`archive_worker trigger_source=manual`, an
external-contact refresh `selected`/`refreshed`/`unavailable` counts, duration,
child CPU/RSS aggregates, and the archive-complete media dispatch outcome.
Every exit (including a lock no-op or failure) ends with a
`lifecycle=ended` line containing `result`, a safe `error_class`, and
`completed_at`. No token, secret, message body, media ID, signed URL, local
path, raw provider setting, or exception text should appear.

Check timers, locks, and the API while workers run:

```bash
sudo systemctl list-timers --all | grep wecom-archive
sudo systemctl status wecom-archive-worker.timer --no-pager
sudo systemctl status wecom-archive-media-event.path --no-pager
sudo systemctl status wecom-archive-media-download.timer --no-pager
sudo systemctl status wecom-external-contact-refresh.path --no-pager
sudo systemctl status wecom-external-contact-refresh.timer --no-pager
sudo systemctl status wecom-external-contact-reconcile.timer --no-pager
sudo journalctl -u wecom-archive-worker.service -n 100 --no-pager
sudo journalctl -u wecom-archive-media-download.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8035/health/ready; echo
```

For production evidence, capture before/after timer empty-run counts,
aggregate `candidate_total`/`candidate_selected`, worker duration, and
CPU/RSS (`systemd-cgtop` or `ps`) during one callback-driven run and one timer
run. Record only aggregates; do not export messages or media identifiers.

## Failure handling

| Observation | Safe meaning / action |
|---|---|
| `trigger=skipped-locked` | Another archive dispatch is pending/running; do not kill it. The timer will reconcile. |
| `media_trigger=no-work` | Archive committed no fresh/pending media in the bounded window; no media process was started. |
| `media_trigger=dispatch-failed` | Archive data is already committed; inspect safe journal categories and rely on the media timer. |
| Archive sync/decrypt failure | No archive-complete media dispatch occurs. Investigate the archive worker journal; cursor/idempotency semantics are unchanged. |
| Callback missed or callback dispatch fails | The archive reconciliation timer eventually re-pulls through the same shared entrypoint and lock. |
| `external_contact_refresh unavailable>0` | Archive sync/decrypt may still have succeeded. The customer may not currently have a readable external-contact relationship; leave the durable task to back off and rely on daily reconciliation. Do not retry with customer IDs in shell history. |

Never paste callback query strings, `.env` contents, token/secret values,
message content, `sdkfileid`, signed URLs, or storage paths into tickets or
logs.
