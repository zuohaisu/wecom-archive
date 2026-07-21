# RND-207 Infrastructure Runbook — Retire Qiniu CDN domain, move to origin domain + thumbnails

> **Do not execute any step in this runbook automatically.** Every action here
> is a manual operator step. Steps are explicitly assigned to **Haisu**
> (deploy/app) or **Miss Hermes** (Qiniu console / DNS / billing). Nothing in
> this runbook is run by CI/CD or by the application.

## 0. Scope & key facts

- The media access domain is **100% driven by the `QINIU_DOMAIN` environment
  variable** — there is **no hardcoded `media.crowntime.cn`** anywhere in the
  code. Switching from the CDN domain to an origin domain is therefore a
  **value change to one env var**, with no code deploy required for the domain
  switch itself.
- Media is served to the browser as a **client-direct short-lived signed URL**
  (Qiniu `private_download_url`) built from `QINIU_DOMAIN`. The bucket stays
  **private** — this migration never makes it public.
- Decision (approved): **Scheme B** — introduce a **new** origin domain
  `media-origin.crowntime.cn` **in parallel** with the existing CDN domain,
  verify, cut over, and only then retire the CDN domain. Lowest risk,
  parallel-verifiable, rollback = revert one env var.

### `REQUIRES EXTERNAL VERIFICATION` (confirm in the Qiniu console / billing — do not assume)

1. Whether a Qiniu **private bucket** signed URL (`private_download_url`) works
   identically on an **origin/source domain** as on the current CDN
   acceleration domain (some Qiniu access paths differ between 融合CDN domains
   and source domains).
2. Whether binding a **new** custom domain to the bucket as an origin domain
   has any cool-down, and the exact console fields/steps.
3. Billing differences between: **CDN traffic** vs **origin (源站) outbound
   traffic** vs **request-count** fees vs **image-processing** fees. This
   migration deliberately does **not** use Qiniu CDN image processing
   (`imageView2`) — thumbnails are separate stored objects — so image-
   processing fees should not increase, but confirm origin egress/request
   pricing before cutover.
4. Whether the origin domain returns cacheable response headers
   (`Cache-Control`, `ETag`) and honors conditional requests (304). The
   RND-207 fixed-window signed URL keeps the URL byte-identical within a
   window; browser byte-caching additionally depends on the origin returning
   cacheable headers.

---

## 1. Pre-change baseline capture — **Miss Hermes** (before any change)

Record, in a private ops note (never paste full signed URLs / tokens / AK/SK):

- [ ] Current CDN domain (`media.crowntime.cn`) status & bound bucket.
- [ ] Current DNS records for the media domain (type, value, TTL).
- [ ] Current TLS certificate (issuer, expiry) on the media domain.
- [ ] Bucket name (`365-wecom-media`) and region (`z2`).
- [ ] Current `QINIU_DOMAIN` value in production `.env` (redact if needed).
- [ ] Current **CDN traffic** baseline (last 7/30 days).
- [ ] Current **origin outbound traffic** baseline.
- [ ] Current **request count** baseline.
- [ ] Current **average image object size**.
- [ ] Backup of the production `.env` (at least the `QINIU_*` / `MEDIA_*`
      block) so a config rollback is a copy-back.

## 2. New origin domain — **Miss Hermes**

- [ ] In the Qiniu console, create/bind **`media-origin.crowntime.cn`** to the
      `365-wecom-media` bucket as an **origin/source** domain (NOT a new CDN
      acceleration domain). *(exact console location/field names —
      `REQUIRES EXTERNAL VERIFICATION`.)*
- [ ] Add the required DNS record (type/value per the console) for
      `media-origin.crowntime.cn`.
- [ ] Configure/verify an HTTPS certificate for the new domain.
- [ ] **Private signed-URL verification** without leaking secrets: generate a
      signed URL for one known private object via the app's own path (see
      §2.1) and confirm `HTTP 200` over HTTPS, correct `Host`, valid cert
      chain, and — via response headers / Qiniu logs — that the request did
      **not** traverse the CDN. Report only status codes and header names,
      never the full URL / token.

### 2.1 Safe signed-URL check (no secrets in output) — **Haisu**

From a staging/test shell with `QINIU_DOMAIN=https://media-origin.crowntime.cn`
set, use the app's provider to mint a URL and curl it, printing only the
status code and safe headers:

```bash
# prints HTTP status + cache/etag headers only; never the URL or token
curl -sS -o /dev/null -D - "$SIGNED_URL" | grep -iE '^HTTP/|^cache-control:|^etag:|^content-type:'
```
(where `$SIGNED_URL` is produced in-process and never echoed/logged.)

---

## 3. Application config change (Scheme B cutover) — **Haisu**

The new origin domain and the old CDN domain can be validated in parallel
because only `QINIU_DOMAIN` selects which one the app signs against.

1. [ ] **Test/staging first:** set `QINIU_DOMAIN=https://media-origin.crowntime.cn`
       in the staging `.env`, restart, and verify **every media type** loads:
       image (list thumbnail + viewer original), emotion/sticker, GIF, video,
       audio, file, mixed & chatrecord nested media, revoke original view, and
       local-storage media. Confirm no 403 storms and image load looks normal.
2. [ ] **Production cutover:** change **only** `QINIU_DOMAIN` in the production
       `.env` to the origin domain and restart the app service
       (`systemctl restart wecom-archive-365.service`). No code change is
       required for the domain switch. (Thumbnail/window env vars from §5 may
       be set in the same edit.)
3. [ ] Observe application logs and access metrics; confirm no regression.
4. [ ] **Only after** the new link is confirmed healthy, ask Miss Hermes to
       proceed to §7 (retire CDN).

> **Never** retire/delete the CDN domain before the origin link is verified in
> production. Keep the CDN domain bound and working as the rollback target.

---

## 4. Schema migration (thumbnails) — **Haisu** (production DB migration = gated)

RND-207 adds nullable columns to `media_files` (Alembic `0012`). This is
additive and backward-compatible (every existing row = "no thumbnail yet";
serving is unaffected until the backfill runs).

- [ ] Run `alembic upgrade head` against production **only with explicit
      approval** (production DB migration is a hard gate). Verify `0012` is the
      head and that up/down both work in staging first.

## 5. Thumbnail config — **Haisu**

Optional; sensible defaults apply if unset (see `.env.example`):

```
MEDIA_THUMBNAIL_ENABLED=true
MEDIA_THUMBNAIL_MAX_EDGE=360
MEDIA_THUMBNAIL_JPEG_QUALITY=82
# Signed-URL browser-cache stabilization (defaults to the TTL if unset)
MEDIA_SIGNED_URL_WINDOW_SECONDS=900
```

## 6. Historical thumbnail backfill — **Haisu** (batch backfill = gated)

New downloads generate their thumbnail automatically. Existing images are
filled by `scripts/backfill_thumbnails_once.py` — a manual, idempotent,
resumable tool. **Running the batch backfill against production is a hard gate**
(bounded CPU on the 2C2G box; run in controlled batches).

```bash
# from backend/, on the production host, in controlled batches:
python scripts/backfill_thumbnails_once.py --count-only          # how many remain
python scripts/backfill_thumbnails_once.py --dry-run --limit 20  # validate, no writes
python scripts/backfill_thumbnails_once.py --limit 50            # do a batch
python scripts/backfill_thumbnails_once.py --limit 50 --retry    # revisit failures
# repeat --limit batches until --count-only reports 0
```
A `wecom-thumbnail-backfill.service` template exists (manual `systemctl start`,
**no timer**) if the operator prefers running batches via systemd. It is never
enabled to run automatically.

---

## 7. Retire the CDN domain — **Miss Hermes** (only after §3 verified in prod)

1. [ ] Keep the CDN domain bound through an observation window (e.g. 24–72h)
       after the origin cutover.
2. [ ] Confirm no traffic is still flowing through the CDN domain (should be
       zero once `QINIU_DOMAIN` points at the origin).
3. [ ] **Disable** (not delete) the CDN acceleration domain; observe billing
       and error rates.
4. [ ] After a clean observation window, **delete** the CDN domain and its
       now-unused DNS/cert config.
5. [ ] Confirm origin traffic/billing matches expectations (see §0 external
       verification on pricing).

---

## 8. Rollback

Rollback is layered — apply only the layer that is actually wrong:

| Layer | Symptom | Rollback action | Owner |
|---|---|---|---|
| **Config** | 403 storm / images fail right after cutover | revert `QINIU_DOMAIN` to the CDN domain in `.env`, restart app | Haisu |
| **DNS** | new domain doesn't resolve / cert error | revert/adjust the `media-origin` DNS record; app keeps working on CDN domain if `QINIU_DOMAIN` reverted | Miss Hermes |
| **Qiniu domain** | origin domain unusable for private signed URLs | keep CDN domain bound (do not delete until verified); re-point `QINIU_DOMAIN` back | Miss Hermes |
| **Code** | app-level regression | redeploy the previous commit (domain switch itself needs no code) | Haisu |
| **DB migration** | need to remove thumbnail columns | `alembic downgrade -1` (0012 down is additive-reverse, safe) | Haisu |
| **Thumbnail objects** | want to discard generated thumbnails | thumbnails are separate objects under `tenants/{t}/thumbnails/…`; deleting them only reverts the list to serving originals (thumbnail_ref rows still point at them until re-backfilled) — optional cleanup, never required for correctness | Haisu / Miss Hermes |

Because the CDN domain is kept bound until the origin link is proven, the fast
rollback is always: **revert `QINIU_DOMAIN`, restart** — one env var.

---

## 9. Owner split summary

**Must complete BEFORE production cutover:**
- Miss Hermes: origin domain bound + cert + DNS + private signed-URL verified (§2)
- Haisu: staging verified (§3.1), Alembic `0012` applied (§4, gated)

**Production cutover (Haisu):** change `QINIU_DOMAIN`, restart, observe (§3.2–3.3).

**AFTER cutover verified:**
- Haisu: thumbnail backfill in batches (§6, gated); observe.
- Miss Hermes: retire CDN domain (§7); watch billing/traffic.
