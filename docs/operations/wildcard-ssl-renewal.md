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
`deploy/systemd/qiniu-ssl-renew-wildcard.timer`:

1. **`acme.sh --renew --dns <provider> -d crowntime.cn -d *.crowntime.cn`**
   (DNS-01, same DNSPod-backed provider as every other domain in this
   subsystem — see `ssl-renew/renew.sh`). Exit code contract:
   - `0` — renewed; proceed.
   - `2` — **not due yet. This is a successful no-op** — acme.sh's own
     internal "not due" check is a normal, expected outcome (most days),
     not a failure. Nothing further runs this cycle.
   - anything else — hard failure.
2. **Local fingerprint check.** `sha256sum` of the freshly-renewed (or
   already-current) `fullchain.cer`, compared against the last recorded
   deployed fingerprint. If they match, the certificate is already fully
   deployed — exit success without re-binding, re-copying, or reloading
   anything.
3. **Qiniu upload + bind** (only on a fingerprint change): one upload
   produces a single certID for the wildcard certificate, via the same
   `qiniu_helper.py`/official Qiniu SDK path `renew.sh` uses — no second
   signing implementation.
   - Bind to `media.crowntime.cn` (CDN): **failure is a hard failure.**
   - Bind to `media-origin.crowntime.cn` (origin): **failure is a warning
     only** — this asymmetry is production's real, deliberate behavior,
     not an oversight. The CDN domain is what end users hit.
4. **nginx deployment.** Copies `fullchain.cer` → `fullchain.pem` and the
   private key → `privkey.pem` into the nginx-facing certificate
   directory (mode `640`), then records the new deployed fingerprint.
5. **`sudo systemctl reload nginx`: failure is a warning only.** The
   certificate is already on disk at that point; a stuck reload leaves
   nginx serving the previous (still valid) certificate rather than
   treating a reload hiccup as a renewal failure.

## Environment / configuration

`EnvironmentFile=/etc/qiniu-ssl-renew/media.crowntime.cn.env` (see
[`ssl-renew/examples/wildcard-domain.env.example`](../../ssl-renew/examples/wildcard-domain.env.example)
for the variable names this file documents — **never their real values**).
Required: `DOMAIN`, `QINIU_ACCESS_KEY`, `QINIU_SECRET_KEY`. Optional, with
code defaults: `CDN_DOMAIN`, `ORIGIN_DOMAIN`, `CERT_DIR`, `ACME_SH`,
`DNS_PROVIDER`, `NGINX_CERT_DIR`, `QINIU_HELPER_PYTHON`,
`VERIFY_HTTPS_SKIP_CHAIN`, `ALERT_WEBHOOK_URL`. The systemd unit also sets
`Environment=HOME=/home/wecomarchive` directly (not via the EnvironmentFile)
since acme.sh's own default cert directory depends on `$HOME`, which
systemd services don't otherwise inherit.

## Sudoers dependency

The **only** elevated command this script runs is:

```
sudo systemctl reload nginx
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

## Known, deliberately-preserved gap: `LOCK_FILE`

`renew-wildcard.sh` declares a `LOCK_FILE` variable (parity with
`renew.sh`'s own locking convention) but — matching production exactly —
never acquires an `flock` on it. 41/41 production runs have never
overlapped (a single daily timer, `RandomizedDelaySec`, `Type=oneshot`).
Adding real locking is a legitimate future hardening candidate, but is
explicitly **not** this ticket's scope — see GH-104 Follow-up B's task
description. Do not add it without a separate, deliberate change.

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
