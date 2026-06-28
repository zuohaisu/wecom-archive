# WeCom Archive Worker — Runbook

## Overview

`run_archive_worker_once.py` is the shared entrypoint for the archive pipeline.
It acquires a file lock, runs `sync_wecom_archive_once.py`, and — if sync
succeeds — runs `decrypt_wecom_messages_once.py`.

The worker is designed to be triggered by:

- **systemd timer** (recommended for unattended production)
- **manual CLI invocation** (for testing and one-off runs)
- **future event-triggered callers** (e.g. WeCom callback webhook)

All three triggers coexist safely because the file lock guarantees only one
worker runs at any time.

---

## Shared lock directory setup

**Machine:** Aliyun ECS
**User:** root
**Directory:** any
**Virtualenv:** not required
**.env:** not required

Run once during initial deployment:

```bash
sudo mkdir -p /srv/apps/wecom-archive-365/shared/run
sudo chown wecomarchive:wecomarchive /srv/apps/wecom-archive-365/shared/run
sudo chmod 750 /srv/apps/wecom-archive-365/shared/run
```

The lock file itself is created automatically by the worker.  If the parent
directory cannot be created at worker start (e.g. missing permissions), the
worker fails clearly with a `[FAIL]` message.

---

## Environment variable

| Variable           | Default                                                               | Description                     |
|--------------------|-----------------------------------------------------------------------|---------------------------------|
| `WORKER_LOCK_PATH` | `/srv/apps/wecom-archive-365/shared/run/wecom-archive-worker.lock`    | Path to the shared lock file    |

All other environment variables required by `sync_wecom_archive_once.py` and
`decrypt_wecom_messages_once.py` must also be set in `.env`.

---

## Manual worker run

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
python scripts/run_archive_worker_once.py
```

Successful output ends with:

```
[PASS] run_archive_worker_once completed successfully
```

If another worker is already running:

```
archive_worker lock already held; exiting
```

(exit code 0 — this is a safe no-op.)

---

## systemd timer installation

**Machine:** Aliyun ECS
**User:** root
**Directory:** /srv/apps/wecom-archive-365/current
**Virtualenv:** not required
**.env:** not required

```bash
sudo cp deploy/systemd/wecom-archive-worker.service /etc/systemd/system/
sudo cp deploy/systemd/wecom-archive-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-worker.timer
sudo systemctl status wecom-archive-worker.timer --no-pager
```

### Timer schedule

The timer fires every 5 minutes at `:00, :05, :10, …` (`OnCalendar=*:0/5`).
With `Persistent=true`, if the system was powered off during a scheduled
fire, the timer catches up on boot.

### Manual trigger (for immediate run)

```bash
sudo systemctl start wecom-archive-worker.service
```

The lock prevents concurrent runs triggered this way.

---

## Log check

**Machine:** Aliyun ECS
**User:** root
**Directory:** any
**Virtualenv:** not required
**.env:** not required

```bash
sudo journalctl -u wecom-archive-worker.service -n 100 --no-pager
sudo systemctl list-timers --all | grep wecom-archive
```

---

## Disable timer

**Machine:** Aliyun ECS
**User:** root
**Directory:** any
**Virtualenv:** not required
**.env:** not required

```bash
sudo systemctl disable --now wecom-archive-worker.timer
```

---

## Troubleshooting

| Symptom                                            | Likely cause                             | Action                                                  |
|----------------------------------------------------|------------------------------------------|---------------------------------------------------------|
| `[FAIL] Cannot create lock directory`              | Missing permissions on shared dir        | Run the shared lock directory setup commands above       |
| `[FAIL] Environment variable not set or empty: …`  | Missing `.env` value                     | Verify `.env` has all required variables                |
| Subprocess exits non-zero                          | Sync or decrypt failure                  | Run `sudo journalctl -u wecom-archive-worker.service -n 100 --no-pager` |
| Timer not firing                                   | Timer not enabled/started                | `sudo systemctl status wecom-archive-worker.timer --no-pager` |
