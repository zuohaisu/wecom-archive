# Deploy User Sudoers Model (GH-133)

> This document is the repo-tracked source of truth for what root capability
> the `wecomarchive` deploy user needs, why, and how Ops verifies/rolls back
> a sudoers change. It does **not** contain production secrets or the live
> `/etc/sudoers.d/*` files themselves — only the capability list and the
> reasoning behind it. This repo has no other sudoers source of truth before
> this document; production's `/etc/sudoers.d/wecomarchive` and
> `/etc/sudoers.d/wecom-archive-365` remain the actual runtime state, which
> only Ops mutates.

## Why this exists

Production's `/etc/sudoers.d/wecomarchive` grants the deploy user bare
`/usr/bin/dnf` and `/usr/bin/apt-get` — passwordless, arbitrary-argument
root package-manager execution. Neither is used by any deploy/bootstrap
logic in this repository:

- `scripts/deploy_server.sh` never calls `dnf` anywhere.
- It used to call `apt-get` (via `_ensure_ffmpeg`'s auto-install fallback),
  but that binary **does not exist** on the actual production host
  (Alibaba Cloud Linux 3 is `dnf`-based) — so even before GH-133, that
  code path could only ever fail there. GH-133 removes the call entirely
  (see `_check_ffmpeg` in `scripts/deploy_server.sh`).

A bare `NOPASSWD: /usr/bin/dnf` (or `apt-get`) grant is root-equivalent
code execution for anyone who can act as `wecomarchive` — `dnf install`
from an arbitrary repo, `dnf remove` of anything, `dnf --* ` with
scriptlets, etc. GH-133's goal is to remove both from the target sudoers
state without breaking CD, ffmpeg availability, or systemd unit
management.

## Capability matrix

| Capability | Does repo CD need it? | Root required? | Current sudo rule (production, as reported by Ops) | Target rule |
|---|---|---|---|---|
| App service restart (`systemctl restart wecom-archive-365.service`) | Yes — step 7 of `deploy_server.sh` | Yes | `wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl restart wecom-archive-365.service` | **KEEP**, unchanged |
| nginx reload (`systemctl reload nginx`) | Yes — `ssl-renew/renew-wildcard.sh` (not `deploy_server.sh`) | Yes | `wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx` | **KEEP**, unchanged (owned by [wildcard-ssl-renewal.md](wildcard-ssl-renewal.md), out of GH-133 scope) |
| systemd unit copy/install (`cp .../deploy/systemd/*.service\|*.timer\|*.path /etc/systemd/system/`) | Yes — step 10, `_sync_managed_systemd_units` | Yes | Documented, exact two-glob `cp` grants (see `deploy_server.sh` header) | **KEEP**, unchanged |
| `systemctl daemon-reload` | Yes — step 10, only when a unit file actually changed | Yes | Documented grant | **KEEP**, unchanged |
| Enable timers/paths (`systemctl enable --now wecom-*.timer` / `wecom-*.path`) | Yes — step 10 | Yes | Documented `wecom-*` wildcard grant | **KEEP** (see "systemd wildcard decision" below) |
| Webroot copy / mkdir (`cp`/`mkdir -p /var/www/*`) | Yes — step 9, static homepage publish, only on the root-required fallback tier | Yes (only when the webroot isn't already writable by the deploy user) | Exact two-glob `cp` + `mkdir -p /var/www/*` | **KEEP**, unchanged |
| `qiniu-ssl-renew-wildcard.timer` enable | Yes — one-time enable for wildcard TLS renewal | Yes | Exact grant added by #129, production-verified | **KEEP, untouched** — GH-133 does not modify #129's grant in any way |
| Bare `/usr/bin/dnf` | **No** — never called by any repo script | N/A | Present in production | **REMOVE** |
| Bare `/usr/bin/apt-get` | **No** — GH-133 removes the only caller; the binary is also absent on the real host | N/A | Present in production | **REMOVE** |
| ffmpeg installation via sudo package manager | **No** — ffmpeg is a host prerequisite (see below), never installed by the deploy user | N/A | N/A (was implicit in the `apt-get` grant) | **Never add this.** If ffmpeg needs installing, an operator/root runs `dnf install -y ffmpeg` directly — outside the deploy user's sudo scope entirely. |

## ffmpeg design decision: HOST PREREQUISITE

Two models were considered:

- **Model A — host prerequisite (chosen).** `deploy_server.sh` only checks
  `command -v ffmpeg`; if missing, it fails the deploy immediately with an
  actionable message, restores the working tree (same pattern every other
  pre-restart failure already uses), and does not touch the running
  service. Installing ffmpeg becomes a one-time root/operator action
  (`sudo dnf install -y ffmpeg` on Alibaba Cloud Linux 3), independent of
  the deploy user's own privileges.
- **Model B — controlled bootstrap.** A separate, narrowly-scoped
  mechanism installs ffmpeg automatically. Rejected: the only way to grant
  it without falling back to bare package-manager sudo is an exact,
  single-purpose sudo rule for something like
  `/usr/bin/dnf install -y ffmpeg` — but `dnf install` accepts multiple
  packages and options in ways sudoers command matching does not
  meaningfully constrain (unlike the two-argument `cp`/`systemctl` grants
  elsewhere in this file, an `install -y <pkg>` line still lets the caller
  substitute a different package name at that exact position). That
  reintroduces close to the same blast radius GH-133 exists to remove, for
  a dependency that is not in the request path of a single running
  request.

Deciding factors for Model A:

1. **ffmpeg already degrades gracefully at runtime.**
   `backend/app/voice_transcode.py::_run_ffmpeg` catches
   `FileNotFoundError`/`OSError`/`SubprocessError` and returns `None`;
   `voice_playback_pipeline.py` and `media_worker.py` explicitly document
   that "original download success never depends on ffmpeg" and "no
   ffmpeg outcome can change download success." ffmpeg is not required
   for the service to start, pass `/health/ready`, or archive messages —
   only for one optional playback conversion. There is no reason to grant
   root package-manager access to guarantee an already-optional
   capability.
2. **The old auto-install path was already broken for real production.**
   It shelled out to `apt-get`, which does not exist on Alibaba Cloud
   Linux 3. If ffmpeg had ever actually gone missing on production before
   GH-133, the deploy would already have failed — just with a confusing
   "apt-get: command not found" instead of a clear message. Switching to
   Model A is a **net improvement with no behavior regression** in the
   failure case, and removes a call that could never have succeeded on
   this host to begin with.
3. **Fail-fast beats silent skip.** The task explicitly does not want a
   missing dependency discovered only when a user first uploads a voice
   message. A deploy-time `command -v ffmpeg` check surfaces it
   immediately, in CI/CD logs, with the fix already known.

## systemd wildcard decision: KEEP

`wecom-*.timer` / `wecom-*.path` (the `enable --now` grant in step 10)
stays a wildcard rather than being narrowed to an explicit per-unit list.

**Why this is still minimum *practical* privilege, not just minimum
character count:**

- `deploy/systemd/MANAGED_UNITS` already lists ~15 timer/path units and
  is explicitly designed to grow (GH-104): each new scheduled workload PR
  adds a new `.timer` or `.path` there. An exact, per-unit sudoers line
  would mean every future `MANAGED_UNITS` addition also blocks on a
  separate, manual root-only sudoers edit before it can self-heal via
  CD — reintroducing precisely the "declared in the repo but never
  actually installed" drift GH-104 built step 10 to close (see
  [server-baseline-2026-08-28.md](server-baseline-2026-08-28.md), which
  found exactly this kind of drift already present for several units).
- The wildcard's blast radius is bounded on two independent axes:
  1. **Name.** `wecom-*.timer` / `wecom-*.path` cannot match
     `nginx.service`, `sshd.service`, any `qiniu-*` unit (which needed,
     and got, its own **exact** grant under #129 for exactly this
     reason), or any other unit outside the literal `wecom-` prefix and
     `.timer`/`.path` suffix.
  2. **Provenance.** The only sudo-granted way to *place* a new file
     under `/etc/systemd/system/` is the accompanying `cp` grant, which
     is itself restricted to copying from
     `$DEPLOY_DIR/deploy/systemd/*.timer` / `*.path` — i.e. files that
     are already tracked in this repository's `deploy/systemd/`
     directory and reached `main` through the normal PR/review/CI
     process. `enable --now wecom-*.timer` cannot be used to activate an
     arbitrary attacker-supplied unit unless that unit both (a) is
     already present under `/etc/systemd/system/` by some other means
     entirely outside this sudoers grant, and (b) happens to match the
     `wecom-*` naming convention.
- #129's own precedent (an **exact**, non-glob grant for
  `qiniu-ssl-renew-wildcard.timer`) is the right call there specifically
  *because* it was a single, one-off timer being onboarded — the cost of
  an exact line was low. Generalizing that same reasoning to `wecom-*`,
  which already covers over a dozen units and gains more on essentially
  every scheduled-workload PR, would generalize the wrong lesson: it
  would trade a bounded, well-understood wildcard for a recurring,
  unbounded operational cost.

If a future unit ever needs to live outside the `wecom-*` namespace (as
`qiniu-ssl-renew-wildcard.timer` did), follow #129's precedent: add an
exact, single-unit grant for it rather than broadening this wildcard.

## Production migration runbook

This is the step-by-step Ops needs to move production from its current
sudoers state to the GH-133 target state. **Dev does not execute any of
this** — no SSH, no sudoers edits, no production deploys were performed as
part of implementing GH-133.

### 0. Pre-check (read-only)

```bash
# Confirm what this deploy is currently running
cd /srv/apps/wecom-archive-365/current && git rev-parse HEAD

# Current effective sudo grants for the deploy user
sudo -l -U wecomarchive

# Back up the current sudoers drop-ins before touching anything
sudo cp /etc/sudoers.d/wecomarchive /etc/sudoers.d/wecomarchive.bak.$(date +%Y%m%d%H%M%S)
sudo cp /etc/sudoers.d/wecom-archive-365 /etc/sudoers.d/wecom-archive-365.bak.$(date +%Y%m%d%H%M%S)

# ffmpeg presence (must already be installed — GH-133 does not change this)
command -v ffmpeg && ffmpeg -version | head -1

# Relevant service/timer state
systemctl is-active wecom-archive-365.service
systemctl list-timers 'wecom-*' --no-pager
systemctl list-timers 'qiniu-ssl-renew-wildcard.timer' --no-pager
```

### 1. Sudoers mutation

In `/etc/sudoers.d/wecomarchive`:

| Line | Action |
|---|---|
| `/usr/bin/systemctl restart wecom-archive-365.service` | **KEEP** — unchanged |
| `/usr/bin/mkdir -p /var/www/*` | **KEEP** — unchanged |
| `/usr/bin/cp <two-glob shape>` (webroot publish) | **KEEP** — unchanged |
| `/usr/bin/dnf` | **REMOVE** |
| `/usr/bin/apt-get` | **REMOVE** |

In the systemd-management sudoers file (wherever the `cp .../deploy/systemd/*` / `daemon-reload` / `enable --now wecom-*.timer` / `enable --now wecom-*.path` grants live):

| Line | Action |
|---|---|
| `cp` grants for `*.service`/`*.timer`/`*.path` under `deploy/systemd/` | **KEEP** — unchanged |
| `/usr/bin/systemctl daemon-reload` | **KEEP** — unchanged |
| `/usr/bin/systemctl enable --now wecom-*.timer` | **KEEP** — unchanged (see "systemd wildcard decision" above) |
| `/usr/bin/systemctl enable --now wecom-*.path` | **KEEP** — unchanged |
| `/usr/bin/systemctl enable --now qiniu-ssl-renew-wildcard.timer` (#129) | **DO NOT TOUCH** |

Nothing is **ADD**ed or **CHANGE**d by GH-133 — this is a pure removal of
the two dead/dangerous lines. If those two lines are the entire content of
a given sudoers block (unlikely, given the table above), delete the block
rather than leaving an empty `NOPASSWD:` clause.

### 2. Validate before reloading

```bash
sudo visudo -cf /etc/sudoers.d/wecomarchive
sudo visudo -cf /etc/sudoers.d/wecom-archive-365   # or wherever the systemd grants live
sudo visudo -c
```

Do not proceed past a non-zero exit from any of the above.

### 3. Positive tests (every required grant still works)

```bash
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl restart wecom-archive-365.service
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl status wecom-archive-365.service
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl daemon-reload
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl enable --now wecom-archive-worker.timer
sudo -n -u wecomarchive sudo -n /usr/bin/mkdir -p /var/www/deploy-sudoers-probe
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl reload nginx
```

Each must exit `0` with no password prompt. `systemctl status` is the
positive sudo verification because it is the authorized service-inspection
capability. `deploy_server.sh` deliberately runs `systemctl is-active`
without sudo: ordinary users can query active state, so it must not be used
to imply a broader sudo grant. Clean up any probe artifacts (e.g.
`/var/www/deploy-sudoers-probe`) afterward.

### 4. Negative tests (removed/never-granted capability is actually rejected)

```bash
sudo -n -u wecomarchive sudo -n /usr/bin/dnf --version;    echo "exit=$?"   # expect nonzero, "a password is required" or "not allowed"
sudo -n -u wecomarchive sudo -n /usr/bin/apt-get --version; echo "exit=$?"  # expect nonzero, same reason

# Wildcard boundary: none of these unit names may be enabled via the
# wecom-*/qiniu-ssl-renew-wildcard grants.
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl enable --now nginx.service;    echo "exit=$?"
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl enable --now sshd.service;     echo "exit=$?"
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl enable --now qiniu-ssl-renew@media.crowntime.cn.timer; echo "exit=$?"
sudo -n -u wecomarchive sudo -n /usr/bin/systemctl enable --now arbitrary.service; echo "exit=$?"
```

All of these must be refused (nonzero exit, sudo's "not allowed" message)
before this migration is considered verified.

### 5. Deployment regression test

Trigger a normal, low-risk production deploy (a docs-only or otherwise
inert commit to `main` is safest) through the existing GitHub Actions
workflow rather than invoking `deploy_server.sh` by hand, and confirm:

- The workflow's SSH step and `deploy_server.sh` both complete with exit
  `0`.
- The log shows `[2/10] Checking ffmpeg host prerequisite …` with no
  install attempt (no `apt-get`/`dnf` in the log at all).
- `/health/ready` and the public health check both pass as usual.
- Step 10's systemd sync still reports success (or its usual non-fatal
  WARNs, unchanged from before this migration) for the units it manages.

### 6. Rollback

If anything above fails or production shows unexpected behavior after the
sudoers change:

```bash
sudo cp /etc/sudoers.d/wecomarchive.bak.<timestamp> /etc/sudoers.d/wecomarchive
sudo cp /etc/sudoers.d/wecom-archive-365.bak.<timestamp> /etc/sudoers.d/wecom-archive-365
sudo visudo -c
```

This restores the previous grants, including `dnf`/`apt-get`, exactly as
they were. Rolling back the sudoers file does **not** require reverting
any application code — `deploy_server.sh`'s ffmpeg check has no
dependency on those grants existing, so the application deploy path keeps
working (and keeps failing fast on missing ffmpeg) with either sudoers
state in place. No code rollback is needed purely to reverse this
migration.

## What GH-133 explicitly does not change

- `#129`'s exact `qiniu-ssl-renew-wildcard.timer` grant — untouched.
- TLS renewal cadence or `ssl-renew/renew-wildcard.sh` logic — untouched.
- nginx topology or its own reload grant — untouched.
- Payment/WeCom/application business logic — untouched.
- No production sudoers file, service, or deploy was modified or executed
  by this change; every verification above is Ops's to run.
