# Wildcard SSL Renewal (`*.crowntime.cn`)

**GH-104 Follow-up B.** This captures — with the smallest possible semantic
delta — the production wildcard TLS renewal implementation that was
already running successfully (41/41 observed runs in the 30 days before
capture) but had never been committed to this repository. It was
introduced by the 2026-08-03 RND-261 domain-cutover
([docs/ops/rnd-261-domain-cutover-runbook.md](../ops/rnd-261-domain-cutover-runbook.md))
as a hand-authored script that stayed server-only until now. This document
describes what production actually does; it is not a design proposal.

Do not confuse this with `deploy/systemd/qiniu-ssl-renew@.service` (the
per-domain **Qiniu Kodo CDN custom-domain** certificate template — a
different mechanism entirely, see
[scheduled-workload-manifest.md](scheduled-workload-manifest.md)).

## Authoritative domains

| Role | Domain(s) |
|---|---|
| Certificate SAN | `crowntime.cn` (apex) + `*.crowntime.cn` (wildcard) |
| Qiniu CDN bind (hard-fail if it fails) | `media.crowntime.cn` |
| Qiniu origin bind (warn-only if it fails) | `media-origin.crowntime.cn` |
| nginx vhosts actually served by this certificate | `crowntime.cn`, `www.crowntime.cn`, `qwhhcd.crowntime.cn`, `staging-archive.crowntime.cn` |
| **NOT** served by this certificate | `archive.crowntime.cn` — currently listens on `:80` only. Adding HTTPS there is independent scope; this PR does not do it. |

## Renewal mechanism

`ssl-renew/renew-wildcard.sh`, triggered daily by
`deploy/systemd/qiniu-ssl-renew-wildcard.timer`. `WILDCARD_DOMAIN`,
`CDN_DOMAIN`, and `ORIGIN_DOMAIN` are **hardcoded constants in the
script** (`crowntime.cn` / `media.crowntime.cn` / `media-origin.crowntime.cn`)
— not environment-driven, even though a `DOMAIN=` line exists in the
EnvironmentFile for naming-convention consistency with every other
`.env` in this subsystem. The script does not read it.

1. **`acme.sh --renew --dns <provider> -d crowntime.cn -d *.crowntime.cn`**
   (DNS-01, same DNSPod-backed provider as every other domain in this
   subsystem — see `ssl-renew/renew.sh`). Exit code contract:
   - `0` — renewed; falls through to the fingerprint check below.
   - `2` — **not due yet**, logged, and this **also falls through to the
     fingerprint check** — it is not an early exit. If the locally-issued
     certificate somehow differs from what was last recorded as deployed
     (e.g. a first-ever run, or the `.deployed_fp` record is missing or
     stale), the deploy steps below still run even though acme.sh itself
     didn't renew anything this cycle.
   - anything else — see "Known production defect" below: this is
     supposed to be a hard failure, but the mechanism that enforces it is
     broken.
2. **Local fingerprint check.** `sha256sum` of the current `fullchain.cer`,
   compared against the last recorded deployed fingerprint. If they
   match, the certificate is already fully deployed — exit success
   without re-binding, re-copying, or reloading anything. If they don't
   match, every step below runs.
3. **Qiniu upload + bind**: one upload produces a single certID for the
   wildcard certificate, via the same `qiniu_helper.py`/official Qiniu SDK
   path `renew.sh` uses — no second signing implementation.
   - Bind to `media.crowntime.cn` (CDN) — *intended* to be a hard failure.
   - Bind to `media-origin.crowntime.cn` (origin) — *intended* to be a
     non-fatal warning; the CDN domain is what end users hit. See "Known
     production defect" below for why this intent is not actually what
     happens today.
4. **nginx deployment — only if all three already exist**: the nginx
   certificate destination directory, the local `fullchain.cer`, and the
   local private key. If any is missing, the script does **not** create
   the directory (no `mkdir -p` here) — see "Known production defect"
   below for what actually happens on that path today. When all three are
   present: copies `fullchain.cer` → `fullchain.pem` and the private key →
   `privkey.pem` (mode `640`), then records the new deployed fingerprint.
5. **`sudo -n /usr/bin/systemctl reload nginx`** — exact command shape,
   absolute path, non-interactive (`-n`). *Intended* to be a non-fatal
   step (the certificate is already on disk by this point). See "Known
   production defect" below.

## Known production defect: `warn`/`die` are called but never defined

**This is real, already-in-production behavior — not introduced by
capturing it.** Every failure path in `renew-wildcard.sh` calls `warn
"..."` or `die "..."`, exactly like `renew.sh` does — but unlike
`renew.sh`, which defines its own local `warn()`/`die()` functions after
sourcing `lib/common.sh`, this script defines neither. `lib/common.sh` and
`lib/qiniu.sh` (the only two files it sources) do not provide them either.

Consequence, verified empirically against the real script: an undefined
bash function is "command not found" (exit 127). Because `set -e` is back
in effect for the entire script body once the initial `acme.sh` call's own
`set +e`/`set -e` bracket closes, that 127 is **not** swallowed — it
**immediately halts the script at that exact line**. In practice this
means, for every failure path below, the *intended* message is never
printed (it was only ever an argument to a command that never ran), and
**nothing after that line executes**:

| Failure | Intended behavior | Actual behavior today |
|---|---|---|
| `acme.sh --renew` exits neither 0 nor 2 | Hard failure with a clear message | Halts at exit 127 with a bare "command not found"; no clear message |
| Qiniu upload fails | Hard failure | Halts at exit 127; `.deployed_fp` never written |
| CDN bind fails | Hard failure | Halts at exit 127; same as above |
| **Origin bind fails** | **Non-fatal warning, continue to nginx deploy** | **Halts at exit 127 — nginx never receives the certificate even though the CDN bind already succeeded**, and `.deployed_fp` is never written |
| nginx dir/files missing | Non-fatal warning, skip nginx step | Halts at exit 127 instead of skipping gracefully |
| `sudo -n systemctl reload nginx` fails | Non-fatal warning (cert already on disk) | Not independently exercised by the current control flow the same way — see the script for the exact statement — but the same undefined-command hazard applies to every other `warn`/`die` call site |

The net effect: what was designed as a resilient pipeline with two
deliberately-non-fatal steps (origin bind, nginx reload) currently behaves
as a fully hard-fail pipeline that stops on the very first problem,
without ever explaining why, and — worse — an origin-bind hiccup can
prevent a CDN-bind-successful certificate from ever reaching nginx.
**41/41 observed production runs never hit any of these paths (every run
so far genuinely succeeded end-to-end), which is exactly why this has
never surfaced.**

**This ticket intentionally does not fix it.** GH-104 Follow-up B is a
reproducibility capture, not SSL hardening (see the task's own core
principle: capture first, harden separately). A follow-up issue is
recommended: **"Harden wildcard SSL renewal failure paths and alerting"**
— define local `warn()`/`die()` (or source a shared implementation),
decide whether `origin bind failure` and `nginx reload failure` should
really be non-fatal (and if so, make that true), and add real alerting
via `notify.sh` if desired. None of that is in scope here.

## Environment / configuration

`EnvironmentFile=/etc/qiniu-ssl-renew/media.crowntime.cn.env` (see
[`ssl-renew/examples/wildcard-domain.env.example`](../../ssl-renew/examples/wildcard-domain.env.example)
for the variable names this file documents — **never their real values**).
`QINIU_ACCESS_KEY`/`QINIU_SECRET_KEY` are read by `qiniu_helper.py` from
the environment. `DOMAIN`, `CDN_DOMAIN`, and `ORIGIN_DOMAIN` may be present
in the file for convention/record-keeping but are **not read by the
script** — see "Renewal mechanism" above. Optional, with code defaults:
`CERT_DIR`, `ACME_SH`, `DNS_PROVIDER`, `NGINX_CERT_DIR`,
`QINIU_HELPER_PYTHON`, `VERIFY_HTTPS_SKIP_CHAIN`, `ALERT_WEBHOOK_URL`
(the last two are not currently wired into this script's own control
flow — see the script source). The systemd unit also sets
`Environment=HOME=/home/wecomarchive` directly (not via the EnvironmentFile)
since acme.sh's own default cert directory depends on `$HOME`, which
systemd services don't otherwise inherit.

## Sudoers dependency

The **only** elevated command this script runs is:

```
sudo -n /usr/bin/systemctl reload nginx
```

Production's confirmed grant is exactly:

```
wecomarchive ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx
```

This PR does not modify production sudoers. See the Rollout Plan (this
PR's body) for the one additional, narrowly-scoped grant standard
deployment needs to auto-enable the timer — it is an exact unit-name
match, never a `qiniu-*` glob.

## Log path

`/var/log/qiniu-ssl-renew/renew.log` (journal + file, same convention as
`qiniu-ssl-renew@.service`). Confirm this directory exists, is owned by
`wecomarchive`, and is writable before relying on a fresh install of this
unit — this repository does not create it; it is an existing, already-
proven server directory shared with the per-domain template.

## "Not due yet" success semantics

acme.sh reporting `exit=2` ("not due yet") is the **normal, expected**
outcome on almost every day between renewals — it is not a degraded or
partial success, and post-deploy assertions must not treat it as one. The
most recent observed production run (2026-08-30 ~00:25 CST) succeeded via
exactly this path; the certificate does not come due again until closer to
its 2026-10-19 expiry.

## Other known, deliberately-preserved gap: `LOCK_FILE`

`renew-wildcard.sh` declares a `LOCK_FILE` variable (parity with
`renew.sh`'s own locking convention) but — matching production exactly —
never acquires an `flock` on it. 41/41 production runs have never
overlapped (a single daily timer, `RandomizedDelaySec`, `Type=oneshot`).
Adding real locking is a legitimate future hardening candidate, but is
explicitly **not** this ticket's scope. Recommended as part of the same
"Harden wildcard SSL renewal failure paths and alerting" follow-up issue
as the `warn`/`die` defect above — do not add it here as a separate,
undiscussed change.

## Manual verification (read-only; never forces a renewal)

```bash
# Timer state
systemctl status qiniu-ssl-renew-wildcard.timer --no-pager
systemctl list-timers qiniu-ssl-renew-wildcard.timer --no-pager

# Effective unit (confirm no unexpected drop-in changed it)
systemctl cat qiniu-ssl-renew-wildcard.service
systemctl cat qiniu-ssl-renew-wildcard.timer

# Most recent run's outcome
journalctl -u qiniu-ssl-renew-wildcard.service -n 50 --no-pager

# Certificate still valid, still served by nginx
echo | openssl s_client -connect crowntime.cn:443 -servername crowntime.cn 2>/dev/null \
  | openssl x509 -noout -enddate -subject
```

Do **not** run `renew-wildcard.sh` by hand against production to "test" it
— acme.sh's own not-due check makes an ad hoc manual run harmless most
days, but there is no reason to invoke it outside its timer for this PR's
acceptance; historical evidence (41/41) plus repo/server file-content
equivalence is sufficient (see the Rollout Plan).

## Rollback

If the repo-managed copy behaves unexpectedly after a deploy:

1. Restore the previously-extracted, production-proven unit files and
   script (kept in this PR's commit history — `git revert` the capture
   commit, or `git checkout` the prior commit's copies).
2. `sudo systemctl daemon-reload` and restart the timer only if necessary.
3. Do **not** revoke the current certificate, delete acme.sh's state
   directory (`~/.acme.sh/crowntime.cn/`), or force a new issuance —
   rollback restores the *renewal automation*, never the certificate
   material itself. The existing certificate remains valid regardless of
   which script version renews it next.
4. Do not delete `EnvironmentFile=/etc/qiniu-ssl-renew/media.crowntime.cn.env`
   or the nginx configuration — both are operator-managed and outside this
   repository's ownership.
