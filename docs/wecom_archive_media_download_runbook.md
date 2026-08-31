# WeCom Generic Media Worker — Event Wake-up and Reconciliation Runbook

## One pipeline, one worker

`scripts/download_wecom_media_once.py` is the sole media-worker entrypoint.
It reuses the RND-199 pipeline for top-level `image`, `voice`, `video`,
`file`, `emotion`, and `audio_archive` media, plus actual media entities in
nested `mixed`/`chatrecord` payloads. It owns candidate selection, durable
`media_files` state, SDK lifecycle, storage, `.part` cleanup, and the shared
non-blocking media lock.

RND-343 does **not** add an image-only downloader. An archive-complete wake-up
only touches one coalescing systemd-path signal when a bounded read-only
preflight finds fresh or pending media after sync/decrypt commits. The path
starts this existing CLI in `wecom-archive-media-event.service`.

## Trigger sources

| Source | How it works | Does it wait for download? |
|---|---|---|
| `archive-complete` | Archive entrypoint commits sync/decrypt, releases its lock, then touches the coalescing path signal if fresh/pending media exists; `wecom-archive-media-event.service` starts this CLI | No |
| `timer` | `wecom-archive-media-download.timer` starts the same CLI | No caller waits |
| `manual` | Operator invokes the same CLI | Operator waits |

The public WeCom callback only requests Archive Worker execution. It never
parses, selects, or downloads media in the HTTP request. The normal media
entrypoint discovers every active `TenantWecomConfig`; each candidate query,
SDK initialization, quota check, storage reference, retry record, and outcome
is scoped to that tenant. Missing tenant context fails closed; no ambient
global WeCom setting is a media-worker selector.

## Default reconciliation schedule

The media timer uses:

```text
OnCalendar=*:15/30
--since-hours 72 --newest-first --limit 20 --retry --trigger-source timer
```

Archive reconciliation is at `:00` and `:30`, so generic media reconciliation
is at `:15` and `:45` (15-minute stagger). Both timers retain
`Persistent=true`. The media timer scans pending media and eligible retryable
failures; it is the compensation path for a failed dispatch, lock conflict,
service restart, or missed event.

## Lock and retry behavior

`MEDIA_DOWNLOAD_LOCK_PATH` defaults to:

```text
/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock
```

It is distinct from `WORKER_LOCK_PATH`. If a media worker is already active,
the later invocation logs `trigger=skipped-locked` and exits `0`; it does not
queue threads, futures, or duplicate downloads.

Every SDK attempt increments `media_files.download_attempts` before it begins.
For `--retry`, failed rows remain eligible only while they are below
`EVENT_MEDIA_DOWNLOAD_RETRY_COUNT` (default `3`) and outside the exponential
backoff derived from `EVENT_MEDIA_DOWNLOAD_BACKOFF_SECONDS` (default `30`).
Fresh/pending rows are never blocked behind an old failed row. Failed rows stay
recorded even after their retry budget is exhausted.

## Environment controls

The names below are retained for deployed RND-172 configuration compatibility,
but now govern generic media dispatch:

| Variable | Default | Purpose |
|---|---:|---|
| `EVENT_MEDIA_DOWNLOAD_ENABLED` | `true` | Set explicitly `false` only for an emergency event-wake-up rollback; timer reconciliation remains enabled. |
| `EVENT_MEDIA_DOWNLOAD_BATCH_LIMIT` | `20` | Bounded archive-complete worker batch. |
| `EVENT_MEDIA_DOWNLOAD_RECENT_WINDOW_HOURS` | `24` | Fresh media window for archive-complete wake-ups. |
| `EVENT_MEDIA_DOWNLOAD_RETRY_COUNT` | `3` | Durable failed-media retry cap. |
| `EVENT_MEDIA_DOWNLOAD_BACKOFF_SECONDS` | `30` | Exponential retry backoff base. |

`EVENT_MEDIA_DOWNLOAD_SWEEP_INTERVAL_SECONDS` is legacy configuration only;
RND-343 removed the in-process image-sweep thread.

## Manual invocation

```bash
sudo -iu wecomarchive
cd /srv/apps/wecom-archive-365/current/backend
source .venv/bin/activate
set -a; source .env; set +a

# This discovers all active tenant configurations. Do not add a global
# archive selector to this command.
# Fresh/pending generic media, newest first
python scripts/download_wecom_media_once.py --since-hours 72 --newest-first --limit 20 --trigger-source manual

# Explicit reconciliation including eligible failed media
python scripts/download_wecom_media_once.py --since-hours 72 --newest-first --limit 20 --retry --trigger-source manual
```

The normal output is aggregate-only, for example tenant-tagged `attempted`,
`succeeded`, `failed`, `retryable`, `skipped`, `duration_ms`, `cpu_ms`,
`peak_rss_kb`, and safe failure categories. A missing or unreadable tenant
configuration is a fail-closed per-tenant outcome; another configured tenant
may still reconcile. Every exit (including invalid configuration, a lock
no-op, and an unexpected failure) ends with one `lifecycle=ended` line that
contains `result`, `error_class`, and `completed_at`.
Do not add `sdkfileid`, a local path, storage key, signed URL, message body,
credentials, raw provider settings, or exception text to output or tickets.

## Deploy / verify (Ops-owned)

See [wecom_archive_worker_runbook.md](wecom_archive_worker_runbook.md) for the
backup-first unit installation procedure. After an upgrade, verify:

```bash
sudo systemctl daemon-reload
sudo systemctl status wecom-archive-media-event.path --no-pager
sudo systemctl status wecom-archive-media-download.timer --no-pager
sudo systemctl list-timers --all | grep wecom-archive
sudo journalctl -u wecom-archive-media-download.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8035/health/ready; echo
```

For a callback-path smoke test, use a non-sensitive test message, confirm the
callback returns promptly, then inspect only aggregate archive/media journal
entries. Confirm a no-media archive run logs `media_trigger=no-work`; confirm
an inserted supported media message produces an `archive-complete` accepted
wake-up and eventual media-worker aggregate result. Do not paste identifiers or
media URLs into the verification record.

## Emergency cadence rollback

The versioned 5-minute rollback drop-in is:

```text
deploy/systemd/overrides/wecom-archive-media-download-5min.conf
```

Install it as `/etc/systemd/system/wecom-archive-media-download.timer.d/reconciliation.conf`
and install the matching archive override at the same time; this restores the
prior `:00/:05/...` archive and `:02/:07/...` media cadence. Commands are in
[wecom_archive_worker_runbook.md](wecom_archive_worker_runbook.md#safe-cadence-configuration-and-emergency-rollback).
Do not disable the media timer as a rollback strategy.

## Failure handling

| Observation | Action |
|---|---|
| `trigger=no-work` | Correct: no fresh/pending media met the bounded event criteria. |
| `trigger=skipped-locked` | Correct: another media worker owns the shared media lock. Allow the next timer reconciliation. |
| `trigger=dispatch-failed` | Archive data is committed. Inspect safe aggregate logs; the timer compensates. |
| `failed > 0`, `retryable > 0` | Preserve the failure record; timer retries only after backoff. |
| `retryable=0` for a failed row | Retry cap reached; investigate with aggregate operational evidence, do not silently reset/delete state. |

The media worker is deliberately independent of archive cursor, decryption,
normalisation, revoke, message persistence, signed URL, and frontend serving
semantics.
