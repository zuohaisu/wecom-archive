# WeCom Archive Recent Image Media Download Timer — Runbook

## Overview

`download_wecom_image_media_once.py` (RND-147/RND-151) is the existing manual
one-shot image media downloader. RND-168 adds a systemd timer that runs it
automatically and periodically, so newly ingested image messages become
previewable without an operator running the script by hand.

The timer always invokes the script with:

```
--since-hours 72 --newest-first --limit 20
```

- `--since-hours 72` restricts candidate selection to recently ingested
  messages, avoiding old candidates whose WeCom media retrieval window has
  likely already expired.
- `--newest-first` prioritizes the most recently ingested images within that
  window.
- `--limit 20` bounds each run to a small batch.
- `--retry` is intentionally **not** used by the timer — retrying failed
  media is left to manual/operator invocation, not automatic scheduling.

This is a separate systemd service/timer pair from the existing archive
worker (`wecom-archive-worker.service` / `.timer`). It does not modify, run
inside, or block that worker:

- It is triggered independently by its own timer.
- It uses its own lock file (`MEDIA_DOWNLOAD_LOCK_PATH`, distinct from
  `WORKER_LOCK_PATH`), so it can never contend with the archive worker's
  lock.
- The archive worker's sync/decrypt pipeline is unchanged; media download
  only ever reads already-decrypted `archive_messages` rows.

---

## Overlap prevention

Two layers, per the existing RND-147/RND-151 script behavior
(`backend/scripts/download_wecom_image_media_once.py`):

1. **systemd**: a `Type=oneshot` service invoked by `OnUnitActiveSec=5min`
   will not be started again by the timer while the previous invocation is
   still running (a timer does not start a new instance of a service that is
   still active).
2. **Script-level file lock**: the script itself acquires a non-blocking
   `fcntl.flock` on `MEDIA_DOWNLOAD_LOCK_PATH` before touching the database.
   If another invocation (timer-triggered, or a manual run) already holds
   the lock, the script exits `0` immediately without selecting any
   candidates. This is a safe no-op, not an error — so even a manual run
   overlapping with the timer, or a slow run spilling past the next timer
   tick, cannot race or double-process the same candidates.

---

## Shared lock directory setup

**Machine:** Aliyun ECS
**User:** root
**Directory:** any
**Virtualenv:** not required
**.env:** not required

If the shared lock directory was already created for the archive worker
(see `docs/wecom_archive_worker_runbook.md`), no further action is required
— this timer uses a different lock *file* in the same directory. Otherwise:

```bash
sudo mkdir -p /srv/apps/wecom-archive-365/shared/run
sudo chown wecomarchive:wecomarchive /srv/apps/wecom-archive-365/shared/run
sudo chmod 750 /srv/apps/wecom-archive-365/shared/run
```

The lock file itself (`wecom-media-download.lock`) is created automatically
by the script on first run.

---

## Environment variables

All environment variables required by
`scripts/download_wecom_image_media_once.py` must already be set in the
same `.env` used by the archive worker and backend service — see the
script's module docstring for the full list (`DATABASE_URL`,
`WECOM_CORP_ID`, `WECOM_SDK_LIB_PATH`, `WECOM_ARCHIVE_SECRET`,
`STORAGE_LOCAL_PATH`). Since RND-151 already verified production downloads
work, no new environment variables are required for this timer.

| Variable                   | Default                                                                | Description                                    |
|-----------------------------|-------------------------------------------------------------------------|-------------------------------------------------|
| `MEDIA_DOWNLOAD_LOCK_PATH`  | `/srv/apps/wecom-archive-365/shared/run/wecom-media-download.lock`      | Path to this script's own lock file             |

---

## Manual script run (for testing, outside the timer)

**Machine:** Aliyun ECS
**User:** wecomarchive
**Directory:** /srv/apps/wecom-archive-365/current/backend
**Virtualenv:** required
**.env:** source required

```bash
sudo -iu wecomarchive
cd /srv/apps/wecom-archive-365/current/backend
source .venv/bin/activate
set -a
source .env
set +a
python scripts/download_wecom_image_media_once.py --since-hours 72 --newest-first --limit 20
```

Successful output ends with:

```
[PASS] download_wecom_image_media_once completed
```

---

## systemd timer installation

**Machine:** Aliyun ECS
**User:** root
**Directory:** /srv/apps/wecom-archive-365/current
**Virtualenv:** not required
**.env:** not required

```bash
sudo cp deploy/systemd/wecom-archive-media-download.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-media-download.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-media-download.timer
sudo systemctl status wecom-archive-media-download.timer --no-pager
```

### Timer schedule

The timer fires 5 minutes after boot, then every 5 minutes thereafter
(`OnBootSec=5min`, `OnUnitActiveSec=5min`). With `Persistent=true`, if the
system was powered off during a scheduled fire, the timer catches up
shortly after boot rather than waiting a full interval.

### Manual trigger (for immediate run)

```bash
sudo systemctl start wecom-archive-media-download.service
```

The script's own lock file prevents this from racing a concurrently
running timer-triggered invocation.

---

## Log check

**Machine:** Aliyun ECS
**User:** root
**Directory:** any
**Virtualenv:** not required
**.env:** not required

```bash
sudo journalctl -u wecom-archive-media-download.service -n 100 --no-pager
sudo systemctl list-timers --all | grep wecom-archive
```

Expected `[INFO]`/`[PASS]` lines never contain `sdkfileid`, `local_path`,
message bodies, or other internal identifiers — see the script's own
"Safety constraints" docstring section.

---

## Verification checklist

- Confirm the deployed commit on the production checkout
  (`git -C /srv/apps/wecom-archive-365/current rev-parse HEAD`) matches the
  expected RND-168 commit before enabling the timer.
- `sudo systemctl status wecom-archive-media-download.timer --no-pager`
  shows `active (waiting)` with a sensible "Trigger" time.
- `sudo systemctl list-timers --all | grep wecom-archive` shows both the
  worker timer and this timer scheduled independently.
- `sudo journalctl -u wecom-archive-media-download.service -n 50 --no-pager`
  shows `[PASS] download_wecom_image_media_once completed` (or the safe
  "another instance already holds the run lock" no-op) after each fire.
- Capture before/after aggregate media counts to confirm the timer is
  making progress: run `python scripts/download_wecom_image_media_once.py
  --count-only` (see "Manual script run" above for environment setup)
  once before enabling the timer and again after a few fires, and confirm
  `candidates_with_existing_media_row` increases while `candidate_total`
  decreases accordingly. This is aggregate-only output — the script's
  `--count-only` mode never prints per-row identifiers.
- `wecom-archive-worker.timer` and `wecom-archive-365.service` are
  unaffected — check `sudo systemctl status wecom-archive-worker.timer
  --no-pager` and the existing health endpoint
  (`curl http://127.0.0.1:8035/health`) still report normally.
- Admin timeline continues to show inline previews for newly downloaded
  images without a manual script run (RND-144/RND-151 behavior, now
  automatic).

---

## Disable timer

**Machine:** Aliyun ECS
**User:** root
**Directory:** any
**Virtualenv:** not required
**.env:** not required

```bash
sudo systemctl disable --now wecom-archive-media-download.timer
```

Disabling this timer does not affect `wecom-archive-worker.timer` or the
main backend service — they are independent units.

---

## Troubleshooting

| Symptom                                            | Likely cause                             | Action                                                  |
|------------------------------------------------------|------------------------------------------|-----------------------------------------------------------|
| `[FAIL] Cannot create lock directory`                | Missing permissions on shared dir        | Run the shared lock directory setup commands above         |
| `[FAIL] Environment variable not set or empty: …`    | Missing `.env` value                     | Verify `.env` has all variables the script's docstring requires |
| `another instance already holds the run lock`        | Previous run still in progress (safe)    | No action — exits 0, next timer fire will pick up remaining candidates |
| Subprocess exits non-zero                            | SDK/download/DB failure                  | Run `sudo journalctl -u wecom-archive-media-download.service -n 100 --no-pager` |
| Timer not firing                                     | Timer not enabled/started                | `sudo systemctl status wecom-archive-media-download.timer --no-pager` |
| Archive worker or backend service affected           | Should never happen — independent units  | Confirm via `wecom-archive-worker.timer` status and `/health`; file a bug if correlated |
