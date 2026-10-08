# Wildcard SSL Renewal (`*.crowntime.cn`)

**GH-104 Follow-up B / GH-126 baseline; GH-123 hardening.** The production
wildcard TLS renewal implementation was captured after 41/41 observed
successful runs in the 30 days before capture. GH-123 deliberately changes
only its previously-unimplemented failure reporting and alerting behavior;
the production-proven successful path and its wildcard/domain topology remain
unchanged. The original flow was field-proven during the 2026-08-03 production
domain cutover (runbook since moved out of the tree; see GH-203).

This document distinguishes the captured successful flow from GH-123's
explicit failure policy. It is not a proposal to redesign certificate
issuance, Qiniu bindings, nginx topology, or locking.

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
`deploy/systemd/qiniu-ssl-renew-wildcard.timer`. The script deliberately
sources its helpers from the production checkout's absolute
`/srv/apps/wecom-archive-365/current/ssl-renew` path; this is production
truth, not a portable-script redesign. `WILDCARD_DOMAIN`, `CDN_DOMAIN`, and
`ORIGIN_DOMAIN` are **hardcoded constants in the script** (`crowntime.cn` /
`media.crowntime.cn` / `media-origin.crowntime.cn`)
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
   - anything else — hard failure with a structured diagnostic and exit `1`.
2. **Local fingerprint check.** `sha256sum` of the current `fullchain.cer`,
   compared against the last recorded deployed fingerprint. If they
   match, the certificate is already fully deployed — exit success
   without re-binding, re-copying, or reloading anything. If they don't
   match, every step below runs.
3. **Qiniu upload + bind**: one upload produces a single certID for the
   wildcard certificate, via the same `qiniu_helper.py`/official Qiniu SDK
   path `renew.sh` uses — no second signing implementation.
   - Bind to `media.crowntime.cn` (CDN) — hard failure if it fails.
   - Bind to `media-origin.crowntime.cn` (origin) — non-fatal warning if it
     fails; the CDN domain is what end users hit.
4. **Record the Qiniu deployment before nginx handling.** After the required
   CDN bind succeeds and the origin bind has either succeeded or warned, the
   script writes `.deployed_fp` and clears its TLS markers.
   This ordering is production truth: a later nginx failure can therefore
   leave the fingerprint recorded.
5. **nginx deployment — only if all three already exist**: the nginx
   certificate destination directory, the local `fullchain.cer`, and the
   local private key. If any is missing, the script does **not** create
   the directory (no `mkdir -p` here). When all three are present it copies
   `fullchain.cer` → `fullchain.pem` and the private key → `privkey.pem`,
   attempts mode `640` (best effort), then runs `sudo -n
   /usr/bin/systemctl reload nginx`. The reload's exact command shape is
   absolute and non-interactive (`-n`). See the GH-123 failure policy below.

## GH-123 failure policy and alerting

`lib/common.sh` now provides the canonical `warn()` and `die()` functions
used by the wildcard flow. Both emit one structured, secret-filtered stderr
line containing the level, stage, domain, and message. `warn()` returns zero;
`die()` exits deterministically with `EXIT_GENERIC_FAILURE` (`1`). Neither
calls `notify.sh` directly.

The wildcard systemd unit uses the existing canonical
`OnFailure=wecom-job-failure-alert@%n.service` mechanism. Therefore a hard
failure sends one alert through the existing `notify.sh` transport, while a
warning-only path exits successfully and cannot trigger that systemd alert.
The alert payload contains only the failed unit name; detailed diagnostics
remain in the renewal journal and have already passed secret filtering.

| Failure point | Policy | Final exit / alert | Retry and operator response |
|---|---|---|---|
| `acme.sh --renew` exits other than 0 or 2 | Hard fail | `1`; one systemd failure alert | Retry on the next daily timer after inspecting the journal, acme.sh/DNS provider credentials, DNS reachability, and ACME state. Do not force issuance to test. |
| `fullchain.cer` is absent | Hard fail | `1`; one systemd failure alert | Retry after correcting acme certificate state, path, ownership, disk space, or the underlying ACME failure. |
| Qiniu upload fails | Hard fail | `1`; one systemd failure alert | No fingerprint is written, so the next timer retries after Qiniu credentials, permissions, SDK/runtime, or reachability is repaired. |
| CDN bind fails | Hard fail | `1`; one systemd failure alert | No fingerprint is written, so the next timer retries after Qiniu HTTPS-domain configuration or binding permissions are fixed. |
| Origin bind fails | Warn and continue | final `0`; no systemd failure alert | No automatic retry once the fingerprint is written. Correct the Qiniu origin configuration and bind/verify the certificate through an approved operator procedure. |
| nginx certificate copy fails, or its destination/source precondition is missing | Warn and continue | final `0`; no systemd failure alert | No automatic retry once the fingerprint is written. Correct destination existence/ownership/permissions/disk space, then copy the current certificate pair and reload nginx through the approved procedure. |
| nginx reload fails, including missing/non-authorized `sudo` | Warn and continue | final `0`; no systemd failure alert | No automatic retry once the fingerprint is written. Check the narrow sudoers grant and nginx configuration, run the approved reload, then verify the served certificate. |
| Mandatory bootstrap, secret-filtering, fingerprint, or Qiniu-helper runtime dependency fails | Hard fail | `1`; one systemd failure alert | Restore the tracked helper, executable, interpreter/package, or required utility and let the next timer retry. Dependencies reached only by the approved nginx copy/reload warning stages retain those stages' warning policy. |

The 41/41 captured production runs exercised only the successful path. The
policy above intentionally fixes the old undefined-function exit-127 defect
without changing ACME `0`/`2`, fingerprint-no-op, Qiniu/CDN hard-failure, or
origin/nginx warning semantics.

## Environment / configuration

`EnvironmentFile=/etc/qiniu-ssl-renew/media.crowntime.cn.env` (see
[`ssl-renew/examples/wildcard-domain.env.example`](../../ssl-renew/examples/wildcard-domain.env.example)
for the variable names this file documents — **never their real values**).
`QINIU_ACCESS_KEY`/`QINIU_SECRET_KEY` are read by `qiniu_helper.py` from
the environment. `DOMAIN`, `CDN_DOMAIN`, and `ORIGIN_DOMAIN` may be present
in the file for convention/record-keeping but are **not read by the
script** — see "Renewal mechanism" above. Optional, with code defaults:
`CERT_DIR`, `ACME_SH`, `DNS_PROVIDER`, `NGINX_CERT_DIR`,
`QINIU_HELPER_PYTHON`, `VERIFY_HTTPS_SKIP_CHAIN`, `ALERT_WEBHOOK_URL`.
`ALERT_WEBHOOK_URL` in this EnvironmentFile is not read for wildcard
failure delivery: the canonical `OnFailure` alert unit reads the existing
server-side `backend/.env` setting instead. The systemd unit also sets
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
explicitly **not** GH-123's scope. Do not add it as an incidental change to
failure-path or alerting work.

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
days, but failure-policy verification belongs to the fully mocked test suite,
not a production invocation.

## Historical server-only → tracked adoption (GH-126)

The one-time ownership transition was a completed GH-126 operation. It is
not part of GH-123 and must not be repeated as part of a failure-path rollout.
This hardening deploy updates the already tracked script and unit through the
normal reviewed CD path only.

## Rollback

If the repo-managed copy behaves unexpectedly after a deploy:

1. Revert the focused GH-123 commit through the normal reviewed rollback
   path, restoring the prior tracked script and unit files.
2. `sudo systemctl daemon-reload` and restart the timer only if necessary.
3. Do **not** revoke the current certificate, delete acme.sh's state
   directory (`~/.acme.sh/crowntime.cn/`), or force a new issuance —
   rollback restores the *renewal automation*, never the certificate
   material itself. The existing certificate remains valid regardless of
   which script version renews it next.
4. Do not delete `EnvironmentFile=/etc/qiniu-ssl-renew/media.crowntime.cn.env`
   or the nginx configuration — both are operator-managed and outside this
   repository's ownership.
