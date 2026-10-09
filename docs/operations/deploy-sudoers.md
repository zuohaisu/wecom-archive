# Deploy User Sudoers Model (GH-133)

> This document is the repo-tracked source of truth for the sudoers
> **capability model** the deploy user needs, the reasoning behind each
> grant, and how Ops verifies a sudoers change. It intentionally does not
> describe any specific host's live `/etc/sudoers.d/*` state — that is an
> Ops-owned, host-local concern. What ships here is the model every
> deployment of this project should converge to, plus the verification
> procedure that proves it.

## Why this exists

A bare `NOPASSWD: /usr/bin/dnf` (or `apt-get`) grant for a deploy user is
**root-equivalent code execution**: `dnf install` from an arbitrary repo,
`dnf remove` of anything, `dnf` scriptlets, etc. An earlier version of the
bootstrap path auto-installed ffmpeg through such a grant; GH-133 removed
that call entirely (see `_check_ffmpeg` in `scripts/deploy_server.sh`) and
this document records the capability model that replaced it — without
breaking CD, ffmpeg availability, or systemd unit management.

## Capability matrix

| Capability | Does repo CD need it? | Root required? | Target rule |
|---|---|---|---|
| App service restart (`systemctl restart wecom-archive-365.service`) | Yes — step 7 of `deploy_server.sh` | Yes | Exact grant: `deploy-user ALL=(root) NOPASSWD: /usr/bin/systemctl restart wecom-archive-365.service` |
| nginx reload (`systemctl reload nginx`) | Yes — `ssl-renew/renew-wildcard.sh` (not `deploy_server.sh`) | Yes | Exact grant (owned by [wildcard-ssl-renewal.md](https://github.com/zuohaisu/wecom-archive/wiki/Wildcard-SSL-Renewal)) |
| systemd unit copy/install (`cp .../deploy/systemd/*.service\|*.timer\|*.path /etc/systemd/system/`) | Yes — step 10, `_sync_managed_systemd_units` | Yes | Exact two-glob `cp` grants (see `deploy_server.sh` header) |
| `systemctl daemon-reload` | Yes — step 10, only when a unit file actually changed | Yes | Exact grant |
| Enable timers/paths (`systemctl enable --now wecom-*.timer` / `wecom-*.path`) | Yes — step 10 | Yes | `wecom-*` wildcard grant (see "systemd wildcard decision" below) |
| Webroot copy / mkdir (`cp`/`mkdir -p /var/www/*`) | Yes — step 9, static homepage publish, only on the root-required fallback tier | Yes (only when the webroot isn't already writable by the deploy user) | Exact two-glob `cp` + `mkdir -p /var/www/*` |
| `qiniu-ssl-renew-wildcard.timer` enable | Yes — one-time enable for wildcard TLS renewal | Yes | Exact, single-unit grant (#129 pattern) |
| Bare `/usr/bin/dnf` | **No** — never called by any repo script | N/A | **Never grant.** |
| Bare `/usr/bin/apt-get` | **No** — GH-133 removed the only caller | N/A | **Never grant.** |
| ffmpeg installation via sudo package manager | **No** — ffmpeg is a host prerequisite (see below), never installed by the deploy user | N/A | **Never add this.** If ffmpeg needs installing, an operator/root runs the package-manager command directly — outside the deploy user's sudo scope entirely. |

## ffmpeg design decision: HOST PREREQUISITE

Two models were considered:

- **Model A — host prerequisite (chosen).** `deploy_server.sh` only checks
  `command -v ffmpeg`; if missing, it fails the deploy immediately with an
  actionable message, restores the working tree (same pattern every other
  pre-restart failure already uses), and does not touch the running
  service. Installing ffmpeg becomes a one-time root/operator action
  (e.g. `sudo dnf install -y ffmpeg`), independent of the deploy user's
  own privileges.
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
2. **The old auto-install path was environment-fragile.** It shelled out
   to `apt-get`, which does not exist on `dnf`-based distributions — the
   call could only ever fail there. Model A is a **net improvement with no
   behavior regression** in the failure case.
3. **Fail-fast beats silent skip.** A missing dependency should not be
   discovered only when a user first uploads a voice message. A
   deploy-time `command -v ffmpeg` check surfaces it immediately, in
   CI/CD logs, with the fix already known.

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
  actually installed" drift step 10 exists to close.
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

## Applying and verifying the target state (Ops runbook)

This is the step-by-step procedure for converging a host's sudoers state
to the model above, and for verifying it. **Dev does not execute any of
this** — sudoers edits, production deploys, and host access are Ops
actions under their own authorization.

### 0. Pre-check (read-only)

```bash
# Confirm what this deploy is currently running
cd /srv/apps/wecom-archive-365/current && git rev-parse HEAD

# Current effective sudo grants for the deploy user
sudo -l -U <deploy-user>

# Back up the current sudoers drop-ins before touching anything
for f in /etc/sudoers.d/<deploy-grant-files>; do
  sudo cp "$f" "$f.bak.$(date +%Y%m%d%H%M%S)"
done

# ffmpeg presence (host prerequisite — never installed by the deploy user)
command -v ffmpeg && ffmpeg -version | head -1

# Relevant service/timer state
systemctl is-active wecom-archive-365.service
systemctl list-timers 'wecom-*' --no-pager
systemctl list-timers 'qiniu-ssl-renew-wildcard.timer' --no-pager
```

### 1. Sudoers mutation

In the deploy-user grant file, keep the capability-matrix rows marked
KEEP and remove any bare package-manager lines:

| Line | Action |
|---|---|
| `/usr/bin/systemctl restart wecom-archive-365.service` | **KEEP** — unchanged |
| `/usr/bin/mkdir -p /var/www/*` | **KEEP** — unchanged |
| `/usr/bin/cp <two-glob shape>` (webroot publish) | **KEEP** — unchanged |
| `/usr/bin/dnf` | **REMOVE** if present |
| `/usr/bin/apt-get` | **REMOVE** if present |

In the systemd-management grant file:

| Line | Action |
|---|---|
| `cp` grants for `*.service`/`*.timer`/`*.path` under `deploy/systemd/` | **KEEP** — unchanged |
| `/usr/bin/systemctl daemon-reload` | **KEEP** — unchanged |
| `/usr/bin/systemctl enable --now wecom-*.timer` | **KEEP** — unchanged (see "systemd wildcard decision" above) |
| `/usr/bin/systemctl enable --now wecom-*.path` | **KEEP** — unchanged |
| `/usr/bin/systemctl enable --now qiniu-ssl-renew-wildcard.timer` (#129) | **DO NOT TOUCH** |

This is a pure removal of dead/dangerous lines — nothing is **ADD**ed or
**CHANGE**d. If a removed line is the entire content of a sudoers block,
delete the block rather than leaving an empty `NOPASSWD:` clause.

### 2. Validate before reloading

```bash
sudo visudo -cf /etc/sudoers.d/<each-edited-file>
sudo visudo -c
```

Do not proceed past a non-zero exit from any of the above.

### 3. Positive tests (every required grant still works)

```bash
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl restart wecom-archive-365.service
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl status wecom-archive-365.service
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl daemon-reload
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl enable --now wecom-archive-worker.timer
sudo -n -u <deploy-user> sudo -n /usr/bin/mkdir -p /var/www/deploy-sudoers-probe
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl reload nginx
```

Each must exit `0` with no password prompt. `systemctl status` is the
positive sudo verification because it is the authorized service-inspection
capability. `deploy_server.sh` deliberately runs `systemctl is-active`
without sudo: ordinary users can query active state, so it must not be used
to imply a broader sudo grant. Clean up any probe artifacts (e.g.
`/var/www/deploy-sudoers-probe`) afterward.

### 4. Negative tests (removed/never-granted capability is actually rejected)

```bash
sudo -n -u <deploy-user> sudo -n /usr/bin/dnf --version;    echo "exit=$?"   # expect nonzero, "a password is required" or "not allowed"
sudo -n -u <deploy-user> sudo -n /usr/bin/apt-get --version; echo "exit=$?"  # expect nonzero, same reason

# Wildcard boundary: none of these unit names may be enabled via the
# wecom-*/qiniu-ssl-renew-wildcard grants.
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl enable --now nginx.service;    echo "exit=$?"
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl enable --now sshd.service;     echo "exit=$?"
sudo -n -u <deploy-user> sudo -n /usr/bin/systemctl enable --now arbitrary.service; echo "exit=$?"
```

All of these must be refused (nonzero exit, sudo's "not allowed" message)
before this migration is considered verified.

### 5. Deployment regression test

Trigger a normal, low-risk deploy (a docs-only or otherwise inert commit
to `main` is safest) through the existing GitHub Actions workflow rather
than invoking `deploy_server.sh` by hand, and confirm:

- The workflow's SSH step and `deploy_server.sh` both complete with exit
  `0`.
- The log shows `[2/10] Checking ffmpeg host prerequisite …` with no
  install attempt (no `apt-get`/`dnf` in the log at all).
- `/health/ready` and the public health check both pass as usual.
- Step 10's systemd sync still reports success (or its usual non-fatal
  WARNs, unchanged from before this migration) for the units it manages.

### 6. Rollback

If anything above fails or the host shows unexpected behavior after the
sudoers change:

```bash
sudo cp /etc/sudoers.d/<file>.bak.<timestamp> /etc/sudoers.d/<file>
sudo visudo -c
```

This restores the previous grants exactly as they were. Rolling back the
sudoers file does **not** require reverting any application code —
`deploy_server.sh`'s ffmpeg check has no dependency on those grants
existing, so the application deploy path keeps working (and keeps failing
fast on missing ffmpeg) with either sudoers state in place. No code
rollback is needed purely to reverse this migration.

## What this model explicitly does not change

- #129's exact `qiniu-ssl-renew-wildcard.timer` grant — untouched.
- TLS renewal cadence or `ssl-renew/renew-wildcard.sh` logic — untouched.
- nginx topology or its own reload grant — untouched.
- Payment/WeCom/application business logic — untouched.
- No sudoers file, service, or deploy is modified by documentation alone;
  every verification above is Ops's to run under its own authorization.
