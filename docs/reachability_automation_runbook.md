# Reachability Automation Runbook

## 1. Installation

This repository supplies a daily reconciliation unit and timer. An operator may
install them after the RND-339 migration and application code are deployed:

```bash
sudo cp deploy/systemd/wecom-archive-reachability-check.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-archive-reachability-check.timer
```

The timer is **not** enabled by this repository or by any script. It runs daily
at `04:30:00` local time with `Persistent=true`, 73 minutes after the 03:17
backup. The existing archive worker invokes the incremental pass only after its
sync and decrypt children both succeed.

## 2. Observe status and journal

```bash
sudo systemctl status wecom-archive-reachability-check.timer --no-pager
sudo systemctl list-timers --all | grep reachability
sudo journalctl -u wecom-archive-reachability-check.service -n 100 --no-pager
```

Automation output is intentionally aggregate-only: mode, safe status, and
counts. It never logs tenant identifiers, message references, content, payloads,
or raw errors.

## 3. Manual invocation

Run an operator-requested complete reconciliation from the deployed backend:

```bash
sudo -iu wecomarchive
cd /srv/apps/wecom-archive-365/current/backend
source .venv/bin/activate
set -a; source .env; set +a
python scripts/run_reachability_automation_once.py reconcile
```

`incremental` is the corresponding local worker hook mode. Neither command
accepts a tenant argument; an archive-worker child supplies its tenant selector,
and an independent reconciliation discovers all active tenant configurations.
Do not run a real SDK/sync/decrypt command merely to test this diagnostic.

## 4. Local safe invocation

Use a disposable database and an isolated lock path, never a production URL:

```bash
DATABASE_URL='postgresql://…/wecom_archive_test' \
REACHABILITY_AUTOMATION_LOCK_PATH="$PWD/.reachability-test.lock" \
python backend/scripts/run_reachability_automation_once.py reconcile
```

The lock is shared by manual, daily, and incremental invocations. Lock-held is a
safe no-op. A complete reconciliation rechecks the seven-day success-decrypt
window plus messages behind active findings; only it may resolve a finding.

## 5. Failure handling

A failed, partial, or lock-held diagnostic never changes successful archive
sync/decrypt exit semantics. Inspect the unit journal and the latest
reachability-check snapshot. Do not treat incremental zero findings as a whole
archive healthy conclusion: only complete manual/reconcile evidence can do
that. Incremental negative evidence invalidates a prior healthy snapshot until
a complete full run replaces it.

## 6. Rollback

An operator first disables the new timer, then deploys a reviewed code rollback
that removes the worker hook/router/service. Finally, only after reviewing
finding retention, downgrade migration `0033` to remove the feature-owned
findings and automation history. RND-337 manual reachability checks remain
available throughout. Do not run `systemctl`, migrations, or rollback commands
from an agent session against production.
